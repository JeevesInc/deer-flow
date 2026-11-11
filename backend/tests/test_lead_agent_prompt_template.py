"""The lead-agent prompt must not tell the agent to stop and ask when a detail is missing.

The upstream template said "If anything is unclear, missing ... you MUST ask for
clarification FIRST - do NOT proceed with work". Opus 5.5 took that literally and
answered build/report requests with a list of what was missing instead of the work.
"""

from deerflow.agents.lead_agent.prompt import SYSTEM_PROMPT_TEMPLATE


def test_template_does_not_block_on_missing_details() -> None:
    assert "do NOT proceed with work" not in SYSTEM_PROMPT_TEMPLATE
    assert "Clarification First" not in SYSTEM_PROMPT_TEMPLATE
    assert "What is missing?" not in SYSTEM_PROMPT_TEMPLATE


def test_template_tells_agent_to_proceed_and_close_gaps() -> None:
    assert "make the reasonable call, do the work" in SYSTEM_PROMPT_TEMPLATE
    assert "yours to close in the same turn" in SYSTEM_PROMPT_TEMPLATE
