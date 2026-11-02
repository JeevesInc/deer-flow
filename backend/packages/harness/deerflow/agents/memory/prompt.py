"""Prompt templates for memory update and injection.

Profile updates use PROFILE_UPDATE_PROMPT (no facts — those are in mem0).
Injection combines the slim profile from memory.json with semantically
retrieved mem0 memories.
"""

import logging
import math
import re
from typing import Any

try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Profile-only update prompt (facts removed — mem0 handles them)
# ---------------------------------------------------------------------------

PROFILE_UPDATE_PROMPT = """You are a memory management system. Analyze the conversation and update the user's profile sections.

Current Profile State:
<current_profile>
{current_profile}
</current_profile>

New Conversation to Process:
<conversation>
{conversation}
</conversation>

Instructions:
1. Analyze the conversation for important information about the user
2. Update only the profile sections — do NOT extract individual facts (those are handled separately)

Memory Section Guidelines:

**User Context** (Current state - concise summaries):
- workContext: Professional role, company, key projects. HARD LIMIT: 2-3 sentences, max 300 chars.
- personalContext: Languages, communication preferences, key interests. HARD LIMIT: 1-2 sentences, max 200 chars.
- topOfMind: Current active priorities only. HARD LIMIT: 3-5 bullet points, max 500 chars total.
  CRITICAL: This is a ROLLING WINDOW. Replace completed/stale items with current ones.

**History** (Temporal context — SYNTHESIZED summaries, not event logs):
- recentMonths: High-level themes from last 1-3 months. HARD LIMIT: 4-6 sentences, max 800 chars.
  Synthesize patterns and outcomes, NOT a chronological event log.
- earlierContext: Important patterns from 3-12 months ago. HARD LIMIT: 3-5 sentences, max 500 chars.
- longTermBackground: Persistent foundational context. HARD LIMIT: 2-4 sentences, max 400 chars.

Output Format (JSON):
{{
  "user": {{
    "workContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "personalContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "topOfMind": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "history": {{
    "recentMonths": {{ "summary": "...", "shouldUpdate": true/false }},
    "earlierContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "longTermBackground": {{ "summary": "...", "shouldUpdate": true/false }}
  }}
}}

Important Rules:
- Only set shouldUpdate=true if there's meaningful new information
- RESPECT CHARACTER LIMITS
- topOfMind is a PRIORITY LIST, not an activity log
- recentMonths is a SYNTHESIS, not a diary
- Do NOT record file upload events or session-specific details
- Do NOT record specific times (e.g., "at 08:10")

Return ONLY valid JSON, no explanation or markdown."""


# Keep old name as alias for backward compat
MEMORY_UPDATE_PROMPT = PROFILE_UPDATE_PROMPT

# Kept for backward compat but no longer used in the main flow
FACT_EXTRACTION_PROMPT = """Extract factual information about the user from this message.

Message:
{message}

Extract facts in this JSON format:
{{
  "facts": [
    {{ "content": "...", "category": "preference|knowledge|context|behavior|goal", "confidence": 0.0-1.0 }}
  ]
}}

Return ONLY valid JSON."""


def _count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    if not TIKTOKEN_AVAILABLE:
        return len(text) // 4
    try:
        encoding = tiktoken.get_encoding(encoding_name)
        return len(encoding.encode(text))
    except Exception:
        return len(text) // 4


def _coerce_confidence(value: Any, default: float = 0.0) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return max(0.0, min(1.0, default))
    if not math.isfinite(confidence):
        return max(0.0, min(1.0, default))
    return max(0.0, min(1.0, confidence))


def format_memory_for_injection(
    memory_data: dict[str, Any],
    max_tokens: int = 2000,
    mem0_memories: list[dict] | None = None,
) -> str:
    """Format memory data for injection into system prompt.

    Combines the slim profile from memory.json with semantically relevant
    mem0 memories.

    Args:
        memory_data: The profile data (user context + history, no facts).
        max_tokens: Maximum tokens budget.
        mem0_memories: Optional list of mem0 search results to include.

    Returns:
        Formatted memory string for system prompt injection.
    """
    if not memory_data and not mem0_memories:
        return ""

    sections = []

    # Format user context (always included — small and critical)
    user_data = memory_data.get("user", {})
    if user_data:
        user_sections = []
        work_ctx = user_data.get("workContext", {})
        if work_ctx.get("summary"):
            user_sections.append(f"Work: {work_ctx['summary']}")
        personal_ctx = user_data.get("personalContext", {})
        if personal_ctx.get("summary"):
            user_sections.append(f"Personal: {personal_ctx['summary']}")
        top_of_mind = user_data.get("topOfMind", {})
        if top_of_mind.get("summary"):
            user_sections.append(f"Current Focus: {top_of_mind['summary']}")
        if user_sections:
            sections.append("User Context:\n" + "\n".join(f"- {s}" for s in user_sections))

    # Format history
    history_data = memory_data.get("history", {})
    if history_data:
        history_sections = []
        recent = history_data.get("recentMonths", {})
        if recent.get("summary"):
            history_sections.append(f"Recent: {recent['summary']}")
        earlier = history_data.get("earlierContext", {})
        if earlier.get("summary"):
            history_sections.append(f"Earlier: {earlier['summary']}")
        if history_sections:
            sections.append("History:\n" + "\n".join(f"- {s}" for s in history_sections))

    # Format mem0 memories (semantically retrieved, relevant to current query)
    if mem0_memories:
        base_text = "\n\n".join(sections)
        base_tokens = _count_tokens(base_text) if base_text else 0
        mem_header = "Relevant Memories:\n"
        separator_tokens = _count_tokens("\n\n" + mem_header) if base_text else _count_tokens(mem_header)
        running_tokens = base_tokens + separator_tokens

        memory_lines: list[str] = []
        for mem in mem0_memories:
            text = mem.get("memory", "") or mem.get("text", "") or str(mem)
            text = text.strip()
            if not text:
                continue

            score = mem.get("score", "")
            score_str = f" [{score:.2f}]" if isinstance(score, (int, float)) else ""
            line = f"- {text}{score_str}"

            line_text = ("\n" + line) if memory_lines else line
            line_tokens = _count_tokens(line_text)

            if running_tokens + line_tokens <= max_tokens:
                memory_lines.append(line)
                running_tokens += line_tokens
            else:
                break

        if memory_lines:
            sections.append("Relevant Memories:\n" + "\n".join(memory_lines))

    # Legacy: if facts still exist in memory_data (migration period), include them
    facts_data = memory_data.get("facts", [])
    if isinstance(facts_data, list) and facts_data:
        base_text = "\n\n".join(sections)
        base_tokens = _count_tokens(base_text) if base_text else 0
        facts_header = "Legacy Facts:\n"
        separator_tokens = _count_tokens("\n\n" + facts_header) if base_text else _count_tokens(facts_header)
        running_tokens = base_tokens + separator_tokens

        ranked_facts = sorted(
            (f for f in facts_data if isinstance(f, dict) and isinstance(f.get("content"), str) and f.get("content").strip()),
            key=lambda fact: _coerce_confidence(fact.get("confidence"), default=0.0),
            reverse=True,
        )

        fact_lines: list[str] = []
        for fact in ranked_facts:
            content = fact.get("content", "").strip()
            if not content:
                continue
            category = str(fact.get("category", "context")).strip() or "context"
            confidence = _coerce_confidence(fact.get("confidence"), default=0.0)
            line = f"- [{category} | {confidence:.2f}] {content}"
            line_text = ("\n" + line) if fact_lines else line
            line_tokens = _count_tokens(line_text)
            if running_tokens + line_tokens <= max_tokens:
                fact_lines.append(line)
                running_tokens += line_tokens
            else:
                break

        if fact_lines:
            sections.append("Legacy Facts:\n" + "\n".join(fact_lines))

    if not sections:
        return ""

    result = "\n\n".join(sections)

    token_count = _count_tokens(result)
    if token_count > max_tokens:
        char_per_token = len(result) / token_count
        target_chars = int(max_tokens * char_per_token * 0.95)
        result = result[:target_chars] + "\n..."

    return result


def format_conversation_for_update(messages: list[Any]) -> str:
    """Format conversation messages for memory update prompt."""
    lines = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", str(msg))

        if isinstance(content, list):
            text_parts = []
            for p in content:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict):
                    text_val = p.get("text")
                    if isinstance(text_val, str):
                        text_parts.append(text_val)
            content = " ".join(text_parts) if text_parts else str(content)

        if role == "human":
            content = re.sub(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", "", str(content)).strip()
            if not content:
                continue

        if len(str(content)) > 1000:
            content = str(content)[:1000] + "..."

        if role == "human":
            lines.append(f"User: {content}")
        elif role == "ai":
            lines.append(f"Assistant: {content}")

    return "\n\n".join(lines)
