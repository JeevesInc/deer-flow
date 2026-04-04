"""Memory updater for reading, writing, and updating memory data."""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from deerflow.agents.memory.prompt import (
    MEMORY_UPDATE_PROMPT,
    format_conversation_for_update,
)
from deerflow.agents.memory.storage import get_memory_storage
from deerflow.config.memory_config import get_memory_config
from deerflow.models import create_chat_model
from deerflow.utils.text import extract_text as _extract_text


# ---------------------------------------------------------------------------
# Pydantic models for LLM memory update responses
# ---------------------------------------------------------------------------

class SectionUpdate(BaseModel):
    shouldUpdate: bool = False
    summary: str = ""

class UserUpdate(BaseModel):
    workContext: SectionUpdate = Field(default_factory=SectionUpdate)
    personalContext: SectionUpdate = Field(default_factory=SectionUpdate)
    topOfMind: SectionUpdate = Field(default_factory=SectionUpdate)

class HistoryUpdate(BaseModel):
    recentMonths: SectionUpdate = Field(default_factory=SectionUpdate)
    earlierContext: SectionUpdate = Field(default_factory=SectionUpdate)
    longTermBackground: SectionUpdate = Field(default_factory=SectionUpdate)

class NewFact(BaseModel):
    content: str = ""
    category: str = "context"
    confidence: float = 0.5

class MemoryUpdateResponse(BaseModel):
    """Schema for the JSON the LLM returns when updating memory."""
    user: UserUpdate = Field(default_factory=UserUpdate)
    history: HistoryUpdate = Field(default_factory=HistoryUpdate)
    newFacts: list[NewFact] = Field(default_factory=list)
    factsToRemove: list[str] = Field(default_factory=list)

logger = logging.getLogger(__name__)

def get_memory_data(agent_name: str | None = None) -> dict[str, Any]:
    """Get the current memory data via storage provider."""
    return get_memory_storage().load(agent_name)

def reload_memory_data(agent_name: str | None = None) -> dict[str, Any]:
    """Reload memory data via storage provider."""
    return get_memory_storage().reload(agent_name)



# Matches sentences that describe a file-upload *event* rather than general
# file-related work.  Deliberately narrow to avoid removing legitimate facts
# such as "User works with CSV files" or "prefers PDF export".
_UPLOAD_SENTENCE_RE = re.compile(
    r"[^.!?]*\b(?:"
    r"upload(?:ed|ing)?(?:\s+\w+){0,3}\s+(?:file|files?|document|documents?|attachment|attachments?)"
    r"|file\s+upload"
    r"|/mnt/user-data/uploads/"
    r"|<uploaded_files>"
    r")[^.!?]*[.!?]?\s*",
    re.IGNORECASE,
)


def _strip_upload_mentions_from_memory(memory_data: dict[str, Any]) -> dict[str, Any]:
    """Remove sentences about file uploads from all memory summaries and facts.

    Uploaded files are session-scoped; persisting upload events in long-term
    memory causes the agent to search for non-existent files in future sessions.
    """
    # Scrub summaries in user/history sections
    for section in ("user", "history"):
        section_data = memory_data.get(section, {})
        for _key, val in section_data.items():
            if isinstance(val, dict) and "summary" in val:
                cleaned = _UPLOAD_SENTENCE_RE.sub("", val["summary"]).strip()
                cleaned = re.sub(r"  +", " ", cleaned)
                val["summary"] = cleaned

    # Also remove any facts that describe upload events
    facts = memory_data.get("facts", [])
    if facts:
        memory_data["facts"] = [f for f in facts if not _UPLOAD_SENTENCE_RE.search(f.get("content", ""))]

    return memory_data


def _fact_content_key(content: Any) -> str | None:
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped:
        return None
    return stripped


def _normalize_token(word: str) -> str:
    """Basic normalization: lowercase, strip trailing 's'/'ing'/'ed' for fuzzy matching."""
    w = word.lower().strip(".,;:!?\"'()")
    if w.endswith("ing") and len(w) > 4:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 3:
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 2:
        w = w[:-1]
    return w


def _tokenize(text: str) -> set[str]:
    """Normalized word tokens for similarity comparison."""
    return {_normalize_token(w) for w in text.split() if len(w) > 1}


def _is_similar_to_existing(new_content: str, existing_contents: list[str], threshold: float = 0.55) -> bool:
    """Check if new_content is semantically similar to any existing fact.

    Uses Jaccard similarity on normalized word tokens — lightweight, no deps.
    Also checks if one fact is a substring of another (catches condensed rephrases).
    """
    new_lower = new_content.lower().strip()
    new_tokens = _tokenize(new_content)
    if not new_tokens:
        return False
    for existing in existing_contents:
        existing_lower = existing.lower().strip()
        # Substring check — one contains the other
        if new_lower in existing_lower or existing_lower in new_lower:
            return True
        # Jaccard on normalized tokens
        existing_tokens = _tokenize(existing)
        if not existing_tokens:
            continue
        intersection = new_tokens & existing_tokens
        union = new_tokens | existing_tokens
        if len(intersection) / len(union) >= threshold:
            return True
    return False


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_JSON_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_memory_response(text: str) -> MemoryUpdateResponse:
    """Parse and validate the LLM's memory update JSON response.

    Tries multiple extraction strategies:
      1. Strip markdown code fences and parse
      2. Find JSON object via regex
      3. On validation failure, return an empty (no-op) update
    """
    # Strategy 1: strip markdown code block
    m = _JSON_BLOCK_RE.search(text)
    raw = m.group(1).strip() if m else text.strip()

    # If still wrapped in backticks (single-line), strip them
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    for candidate in (raw, text):
        try:
            data = json.loads(candidate)
            return MemoryUpdateResponse.model_validate(data)
        except (json.JSONDecodeError, ValidationError):
            pass

    # Strategy 2: find first { ... } in text
    m2 = _JSON_BRACE_RE.search(text)
    if m2:
        try:
            data = json.loads(m2.group(0))
            return MemoryUpdateResponse.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning("Memory update JSON found but failed validation: %s", e)

    # Nothing parseable — return no-op
    logger.warning("Could not parse memory update response, returning no-op. First 200 chars: %s", text[:200])
    return MemoryUpdateResponse()


class MemoryUpdater:
    """Updates memory using LLM based on conversation context."""

    def __init__(self, model_name: str | None = None):
        """Initialize the memory updater.

        Args:
            model_name: Optional model name to use. If None, uses config or default.
        """
        self._model_name = model_name

    def _get_model(self):
        """Get the model for memory updates."""
        config = get_memory_config()
        model_name = self._model_name or config.model_name
        return create_chat_model(name=model_name, thinking_enabled=False)

    def update_memory(self, messages: list[Any], thread_id: str | None = None, agent_name: str | None = None) -> bool:
        """Update memory based on conversation messages.

        Args:
            messages: List of conversation messages.
            thread_id: Optional thread ID for tracking source.
            agent_name: If provided, updates per-agent memory. If None, updates global memory.

        Returns:
            True if update was successful, False otherwise.
        """
        config = get_memory_config()
        if not config.enabled:
            return False

        if not messages:
            return False

        try:
            # Get current memory
            current_memory = get_memory_data(agent_name)

            # Format conversation for prompt
            conversation_text = format_conversation_for_update(messages)

            if not conversation_text.strip():
                return False

            # Build prompt
            prompt = MEMORY_UPDATE_PROMPT.format(
                current_memory=json.dumps(current_memory, indent=2),
                conversation=conversation_text,
            )

            # Call LLM
            model = self._get_model()
            response = model.invoke(prompt)
            response_text = _extract_text(response.content).strip()

            # Parse and validate the LLM response
            update_data = _parse_memory_response(response_text)

            # Apply updates
            updated_memory = self._apply_updates(current_memory, update_data, thread_id)

            # Strip file-upload mentions from all summaries before saving.
            # Uploaded files are session-scoped and won't exist in future sessions,
            # so recording upload events in long-term memory causes the agent to
            # try (and fail) to locate those files in subsequent conversations.
            updated_memory = _strip_upload_mentions_from_memory(updated_memory)

            # Save
            return get_memory_storage().save(updated_memory, agent_name)

        except Exception as e:
            logger.exception("Memory update failed: %s", e)
            return False

    def _apply_updates(
        self,
        current_memory: dict[str, Any],
        update: MemoryUpdateResponse,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply validated LLM-generated updates to memory.

        Args:
            current_memory: Current memory data.
            update: Validated update from LLM.
            thread_id: Optional thread ID for tracking.

        Returns:
            Updated memory data.
        """
        config = get_memory_config()
        now = datetime.utcnow().isoformat() + "Z"

        # Update user sections
        for section_name in ("workContext", "personalContext", "topOfMind"):
            section: SectionUpdate = getattr(update.user, section_name)
            if section.shouldUpdate and section.summary:
                current_memory["user"][section_name] = {
                    "summary": section.summary,
                    "updatedAt": now,
                }

        # Update history sections
        for section_name in ("recentMonths", "earlierContext", "longTermBackground"):
            section = getattr(update.history, section_name)
            if section.shouldUpdate and section.summary:
                current_memory["history"][section_name] = {
                    "summary": section.summary,
                    "updatedAt": now,
                }

        # Remove facts
        if update.factsToRemove:
            facts_to_remove = set(update.factsToRemove)
            current_memory["facts"] = [f for f in current_memory.get("facts", []) if f.get("id") not in facts_to_remove]

        # Add new facts (dedup via exact match + similarity check)
        existing_fact_contents = [
            f.get("content", "").strip()
            for f in current_memory.get("facts", [])
            if f.get("content")
        ]
        for fact in update.newFacts:
            if fact.confidence >= config.fact_confidence_threshold:
                normalized_content = fact.content.strip()
                if not normalized_content:
                    continue

                # Exact match
                if normalized_content in existing_fact_contents:
                    continue

                # Similarity match (catches near-duplicates)
                if _is_similar_to_existing(normalized_content, existing_fact_contents):
                    logger.debug("Skipping near-duplicate fact: %s", normalized_content[:80])
                    continue

                fact_entry = {
                    "id": f"fact_{uuid.uuid4().hex[:8]}",
                    "content": normalized_content,
                    "category": fact.category,
                    "confidence": fact.confidence,
                    "createdAt": now,
                    "source": thread_id or "unknown",
                }
                current_memory["facts"].append(fact_entry)
                existing_fact_contents.append(normalized_content)

        # Enforce max facts limit
        if len(current_memory["facts"]) > config.max_facts:
            current_memory["facts"] = sorted(
                current_memory["facts"],
                key=lambda f: f.get("confidence", 0),
                reverse=True,
            )[: config.max_facts]

        return current_memory


def update_memory_from_conversation(messages: list[Any], thread_id: str | None = None, agent_name: str | None = None) -> bool:
    """Convenience function to update memory from a conversation.

    Args:
        messages: List of conversation messages.
        thread_id: Optional thread ID.
        agent_name: If provided, updates per-agent memory. If None, updates global memory.

    Returns:
        True if successful, False otherwise.
    """
    updater = MemoryUpdater()
    return updater.update_memory(messages, thread_id, agent_name)
