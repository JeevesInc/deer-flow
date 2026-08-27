"""Local run tracing for the DeerFlow agent.

Persists one record per model call so agent behavior can be sampled, graded and
replayed offline. See trace_sink for the rationale and file layout.
"""

from deerflow.agents.tracing.trace_sink import enabled, prune, read_traces, stats, write

__all__ = ["enabled", "prune", "read_traces", "stats", "write"]
