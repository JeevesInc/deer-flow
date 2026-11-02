"""Bridge custom "soul" agents into the subagent system.

Custom agents live under ``{base_dir}/agents/{name}/`` with a ``config.yaml``
(parsed into :class:`AgentConfig`) and an optional ``SOUL.md`` that defines the
agent's personality and guardrails. Normally these run as the *lead* agent, one
per thread.

This module exposes each custom agent as a :class:`SubagentConfig` so the lead
agent can delegate to named specialists via the ``task`` tool — i.e. a swarm of
collaborating named agents. The lead acts as orchestrator; each specialist runs
in its own isolated context with its own SOUL as the system prompt.
"""

import logging

from deerflow.config.agents_config import AgentConfig, list_custom_agents, load_agent_config, load_agent_soul
from deerflow.subagents.config import SubagentConfig

logger = logging.getLogger(__name__)

# Appended to every specialist's SOUL so delegated work returns a clean,
# self-contained result rather than a conversational reply.
_SUBAGENT_GUIDELINES = """

<delegated_task>
You are being delegated a focused task by a coordinator agent — you are NOT
talking to the end user. Work autonomously and return a clear, self-contained
result.

- Do NOT ask for clarification; work with the information provided.
- Stay within your area of expertise as defined above.
- When done, return: (1) a concise summary of what you accomplished, (2) key
  findings or results, (3) any file paths or artifacts you created, (4) issues
  encountered, if any.
- Use `[citation:Title](URL)` format for external sources.
</delegated_task>
"""


def custom_agent_to_subagent_config(name: str) -> SubagentConfig | None:
    """Build a :class:`SubagentConfig` from a custom agent, or ``None`` if absent.

    Args:
        name: The custom agent's name (directory name).

    Returns:
        A SubagentConfig wrapping the agent's SOUL and metadata, or ``None`` if
        no such agent exists.
    """
    try:
        agent_config = load_agent_config(name)
    except (FileNotFoundError, ValueError) as exc:
        logger.debug("Custom agent '%s' not available as subagent: %s", name, exc)
        return None

    if agent_config is None:
        return None

    return _build_config(agent_config)


def _build_config(agent_config: AgentConfig) -> SubagentConfig:
    soul = load_agent_soul(agent_config.name) or ""
    system_prompt = (soul + _SUBAGENT_GUIDELINES).strip()

    description = agent_config.description or (
        f"The '{agent_config.name}' specialist agent. Delegate domain-specific "
        "work matching this agent's expertise."
    )

    return SubagentConfig(
        name=agent_config.name,
        description=description,
        system_prompt=system_prompt,
        # Inherit the full tool set for now. Honoring per-agent `tool_groups`
        # requires resolving groups -> tool names at the task-tool layer; the
        # SOUL still scopes the specialist's behavior. (TODO: respect tool_groups.)
        tools=None,
        # Prevent recursive nesting and end-user-only interactions.
        disallowed_tools=["task", "ask_clarification", "present_files"],
        model=agent_config.model or "inherit",
    )


def list_custom_agent_subagent_configs() -> list[SubagentConfig]:
    """Return a :class:`SubagentConfig` for every valid custom agent on disk."""
    configs: list[SubagentConfig] = []
    for agent_config in list_custom_agents():
        try:
            configs.append(_build_config(agent_config))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Skipping custom agent '%s' as subagent: %s", agent_config.name, exc)
    return configs


def list_custom_agent_subagent_names() -> list[str]:
    """Return the names of all custom agents available as subagents."""
    return [cfg.name for cfg in list_custom_agent_subagent_configs()]
