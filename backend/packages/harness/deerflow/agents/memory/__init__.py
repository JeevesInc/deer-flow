"""Memory module for DeerFlow.

Long-term facts are stored and retrieved via mem0 (semantic vector search).
Profile sections (workContext, personalContext, topOfMind, history) are
managed in a slim memory.json via the profile updater.
"""

from deerflow.agents.memory.prompt import (
    FACT_EXTRACTION_PROMPT,
    MEMORY_UPDATE_PROMPT,
    PROFILE_UPDATE_PROMPT,
    format_conversation_for_update,
    format_memory_for_injection,
)
from deerflow.agents.memory.queue import (
    ConversationContext,
    MemoryUpdateQueue,
    get_memory_queue,
    reset_memory_queue,
)
from deerflow.agents.memory.storage import (
    FileMemoryStorage,
    MemoryStorage,
    get_memory_storage,
)
from deerflow.agents.memory.updater import (
    MemoryUpdater,
    ProfileUpdater,
    get_memory_data,
    reload_memory_data,
    update_memory_from_conversation,
    update_profile_from_conversation,
)

__all__ = [
    # Prompt utilities
    "MEMORY_UPDATE_PROMPT",
    "PROFILE_UPDATE_PROMPT",
    "FACT_EXTRACTION_PROMPT",
    "format_memory_for_injection",
    "format_conversation_for_update",
    # Queue
    "ConversationContext",
    "MemoryUpdateQueue",
    "get_memory_queue",
    "reset_memory_queue",
    # Storage
    "MemoryStorage",
    "FileMemoryStorage",
    "get_memory_storage",
    # Updater
    "MemoryUpdater",
    "ProfileUpdater",
    "get_memory_data",
    "reload_memory_data",
    "update_memory_from_conversation",
    "update_profile_from_conversation",
]
