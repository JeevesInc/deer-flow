"""Memory updater — profile sections only.

Facts are now handled by mem0 (see mem0_store.py).  This module only
manages the slim memory.json profile: workContext, personalContext,
topOfMind, and history summaries.
"""

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from deerflow.agents.memory.prompt import (
    PROFILE_UPDATE_PROMPT,
    format_conversation_for_update,
)
from deerflow.agents.memory.storage import get_memory_storage
from deerflow.config.memory_config import get_memory_config
from deerflow.models import create_chat_model
from deerflow.utils.text import extract_text as _extract_text


# ---------------------------------------------------------------------------
# Pydantic models for LLM profile update responses
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

class ProfileUpdateResponse(BaseModel):
    """Schema for the JSON the LLM returns when updating the profile."""
    user: UserUpdate = Field(default_factory=UserUpdate)
    history: HistoryUpdate = Field(default_factory=HistoryUpdate)


logger = logging.getLogger(__name__)


def get_memory_data(agent_name: str | None = None) -> dict[str, Any]:
    """Get the current memory data via storage provider."""
    return get_memory_storage().load(agent_name)


def reload_memory_data(agent_name: str | None = None) -> dict[str, Any]:
    """Reload memory data via storage provider."""
    return get_memory_storage().reload(agent_name)


# Matches sentences that describe a file-upload *event*.
_UPLOAD_SENTENCE_RE = re.compile(
    r"[^.!?]*\b(?:"
    r"upload(?:ed|ing)?(?:\s+\w+){0,3}\s+(?:file|files?|document|documents?|attachment|attachments?)"
    r"|file\s+upload"
    r"|/mnt/user-data/uploads/"
    r"|<uploaded_files>"
    r")[^.!?]*[.!?]?\s*",
    re.IGNORECASE,
)


def _strip_upload_mentions(memory_data: dict[str, Any]) -> dict[str, Any]:
    """Remove sentences about file uploads from all memory summaries and legacy facts."""
    for section in ("user", "history"):
        section_data = memory_data.get(section, {})
        for _key, val in section_data.items():
            if isinstance(val, dict) and "summary" in val:
                cleaned = _UPLOAD_SENTENCE_RE.sub("", val["summary"]).strip()
                cleaned = re.sub(r"  +", " ", cleaned)
                val["summary"] = cleaned

    # Also filter legacy facts if still present (migration period)
    facts = memory_data.get("facts", [])
    if facts:
        memory_data["facts"] = [f for f in facts if not _UPLOAD_SENTENCE_RE.search(f.get("content", ""))]

    return memory_data


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_JSON_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_profile_response(text: str) -> ProfileUpdateResponse:
    """Parse and validate the LLM's profile update JSON response."""
    m = _JSON_BLOCK_RE.search(text)
    raw = m.group(1).strip() if m else text.strip()

    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    for candidate in (raw, text):
        try:
            data = json.loads(candidate)
            return ProfileUpdateResponse.model_validate(data)
        except (json.JSONDecodeError, ValidationError):
            pass

    m2 = _JSON_BRACE_RE.search(text)
    if m2:
        try:
            data = json.loads(m2.group(0))
            return ProfileUpdateResponse.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning("Profile update JSON found but failed validation: %s", e)

    logger.error(
        "PROFILE PARSE FAILURE: Could not parse response. Length: %d. First 500 chars: %s",
        len(text), text[:500],
    )
    return ProfileUpdateResponse()


class ProfileUpdater:
    """Updates profile sections in memory.json using LLM.

    Facts are NOT managed here — they're in mem0.
    """

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name

    def _get_model(self):
        config = get_memory_config()
        model_name = self._model_name or config.model_name
        return create_chat_model(name=model_name, thinking_enabled=False)

    # Legacy alias
    def update_memory(self, messages: list[Any], thread_id: str | None = None, agent_name: str | None = None) -> bool:
        return self.update_profile(messages, thread_id, agent_name)

    def update_profile(
        self, messages: list[Any], thread_id: str | None = None, agent_name: str | None = None
    ) -> bool:
        """Update profile sections based on conversation messages.

        Returns True if update was successful.
        """
        config = get_memory_config()
        if not config.enabled:
            return False
        if not messages:
            return False

        try:
            current_memory = get_memory_data(agent_name)

            conversation_text = format_conversation_for_update(messages)
            if not conversation_text.strip():
                return False

            # Build a slim version of memory for the profile prompt (no facts)
            profile_data = {
                "user": current_memory.get("user", {}),
                "history": current_memory.get("history", {}),
            }

            prompt = PROFILE_UPDATE_PROMPT.format(
                current_profile=json.dumps(profile_data, indent=2),
                conversation=conversation_text,
            )

            model = self._get_model()
            response = model.invoke(prompt)
            response_text = _extract_text(response.content).strip()

            update_data = _parse_profile_response(response_text)
            updated_memory = self._apply_updates(current_memory, update_data)
            updated_memory = _strip_upload_mentions(updated_memory)

            # Ensure no facts array in the saved data
            updated_memory.pop("facts", None)

            return get_memory_storage().save(updated_memory, agent_name)

        except Exception as e:
            logger.exception("Profile update failed: %s", e)
            return False

    def _apply_updates(
        self,
        current_memory: dict[str, Any],
        update: ProfileUpdateResponse,
        thread_id: str | None = None,  # Accepted for backward compat, not used
    ) -> dict[str, Any]:
        """Apply validated LLM-generated updates to profile sections."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        _SECTION_CHAR_LIMITS = {
            "workContext": 500,
            "personalContext": 400,
            "topOfMind": 800,
            "recentMonths": 1200,
            "earlierContext": 800,
            "longTermBackground": 600,
        }

        def _enforce_limit(text, section_name):
            limit = _SECTION_CHAR_LIMITS.get(section_name, 1200)
            if len(text) > limit:
                logger.warning(
                    "Profile section '%s' exceeds %d char limit (%d chars). Truncating.",
                    section_name, limit, len(text),
                )
                truncated = text[:limit]
                last_period = truncated.rfind('.')
                if last_period > limit * 0.6:
                    return truncated[:last_period + 1]
                return truncated.rstrip() + "..."
            return text

        # Update user sections
        for section_name in ("workContext", "personalContext", "topOfMind"):
            section: SectionUpdate = getattr(update.user, section_name)
            if section.shouldUpdate and section.summary:
                current_memory.setdefault("user", {})[section_name] = {
                    "summary": _enforce_limit(section.summary, section_name),
                    "updatedAt": now,
                }

        # Update history sections
        for section_name in ("recentMonths", "earlierContext", "longTermBackground"):
            section = getattr(update.history, section_name)
            if section.shouldUpdate and section.summary:
                current_memory.setdefault("history", {})[section_name] = {
                    "summary": _enforce_limit(section.summary, section_name),
                    "updatedAt": now,
                }

        return current_memory


def update_profile_from_conversation(
    messages: list[Any], thread_id: str | None = None, agent_name: str | None = None
) -> bool:
    """Convenience function to update profile from a conversation."""
    updater = ProfileUpdater()
    return updater.update_profile(messages, thread_id, agent_name)


# ---------------------------------------------------------------------------
# Backward-compat aliases used by gateway router, tests, and other imports
# ---------------------------------------------------------------------------
MemoryUpdater = ProfileUpdater
update_memory_from_conversation = update_profile_from_conversation

# Legacy aliases for tests that import old names
MemoryUpdateResponse = ProfileUpdateResponse
_strip_upload_mentions_from_memory = _strip_upload_mentions


class NewFact(BaseModel):
    """Legacy model kept for backward compat with tests."""
    content: str = ""
    category: str = "context"
    confidence: float = 0.5


def _is_similar_to_existing(new_content: str, existing_contents: list[str], threshold: float = 0.55) -> bool:
    """Legacy function kept for backward compat with tests."""
    new_lower = new_content.lower().strip()
    new_tokens = _tokenize(new_content)
    if not new_tokens:
        return False
    for existing in existing_contents:
        existing_lower = existing.lower().strip()
        if new_lower in existing_lower or existing_lower in new_lower:
            return True
        existing_tokens = _tokenize(existing)
        if not existing_tokens:
            continue
        intersection = new_tokens & existing_tokens
        union = new_tokens | existing_tokens
        if len(intersection) / len(union) >= threshold:
            return True
    return False


def _normalize_token(word: str) -> str:
    """Legacy function kept for backward compat with tests."""
    w = word.lower().strip(".,;:!?\"'()")
    if w.endswith("ing") and len(w) > 4:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 3:
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 2:
        w = w[:-1]
    return w


def _tokenize(text: str) -> set[str]:
    """Legacy function kept for backward compat with tests."""
    return {_normalize_token(w) for w in text.split() if len(w) > 1}


def _parse_memory_response(text: str) -> ProfileUpdateResponse:
    """Legacy alias for _parse_profile_response."""
    return _parse_profile_response(text)
