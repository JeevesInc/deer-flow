"""Shared Anthropic model constants + response helpers (P2-4).

Nine scripts used to hardcode model ids — the Fable-5→Opus and
Sonnet-5 migrations each meant 13 separate edits. New code imports the
tier constants; the next migration is one env var (or one line here).

Tiers (see MEMORY: model standardization 2026-06-30):
- SONNET (quality-sensitive: drafters, synthesizers, dossiers, SQL gen)
- HAIKU  (high-frequency/cost-sensitive: classifiers, labelers, SLL)
- OPUS   (resilience fallback only)

Both `claude-sonnet-5` and `claude-opus-4-8` are adaptive-thinking-only:
no budget_tokens, no temperature-with-thinking, and responses may lead
with a ThinkingBlock — ALWAYS use text_from_response(), never
resp.content[0].text (that crashed the proposal synthesizer and the CICO
vision extractor).
"""

import os

SONNET = os.environ.get("DEERFLOW_SONNET_MODEL", "claude-sonnet-5")
HAIKU = os.environ.get("DEERFLOW_HAIKU_MODEL", "claude-haiku-4-5-20251001")
OPUS = os.environ.get("DEERFLOW_OPUS_MODEL", "claude-opus-4-8")


def text_from_response(resp) -> str:
    """Concatenated text blocks from a Messages API response.

    Safe against adaptive-thinking models returning a ThinkingBlock as
    content[0] (where .text raises AttributeError).
    """
    parts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return "".join(parts)


def client(**kwargs):
    """An anthropic.Anthropic() using ANTHROPIC_API_KEY from env."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key, **kwargs)
