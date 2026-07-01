"""Memory update queue with debounce mechanism.

After debounce, feeds conversations to mem0 for long-term fact storage
and to the profile updater for the slim memory.json sections.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from deerflow.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """Context for a conversation to be processed for memory update."""

    thread_id: str
    messages: list[Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    agent_name: str | None = None


class MemoryUpdateQueue:
    """Queue for memory updates with debounce mechanism.

    Collects conversation contexts and processes them after a configurable
    debounce period.  Processing now does two things:
    1. Feeds conversations to mem0 for semantic long-term memory
    2. Updates profile sections in memory.json via the profile updater
    """

    def __init__(self):
        self._queue: dict[str, ConversationContext] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._processing = False

    def add(self, thread_id: str, messages: list[Any], agent_name: str | None = None) -> None:
        config = get_memory_config()
        if not config.enabled:
            return

        context = ConversationContext(
            thread_id=thread_id,
            messages=messages,
            agent_name=agent_name,
        )

        with self._lock:
            self._queue[thread_id] = context
            self._reset_timer()

        logger.info("Memory update queued for thread %s, queue size: %d", thread_id, len(self._queue))

    def _reset_timer(self) -> None:
        config = get_memory_config()
        if self._timer is not None:
            self._timer.cancel()

        self._timer = threading.Timer(
            config.debounce_seconds,
            self._process_queue,
        )
        self._timer.daemon = True
        self._timer.start()
        logger.debug("Memory update timer set for %ss", config.debounce_seconds)

    def _process_queue(self) -> None:
        """Process all queued conversations via mem0 + profile updater."""
        with self._lock:
            if self._processing:
                self._reset_timer()
                return
            if not self._queue:
                return
            self._processing = True
            contexts_to_process = list(self._queue.values())
            self._queue.clear()
            self._timer = None

        logger.info("Processing %d queued memory updates", len(contexts_to_process))

        try:
            for context in contexts_to_process:
                try:
                    self._process_single(context)
                except Exception as e:
                    logger.error("Error updating memory for thread %s: %s", context.thread_id, e)

                if len(contexts_to_process) > 1:
                    time.sleep(0.5)
        finally:
            with self._lock:
                self._processing = False

    def _process_single(self, context: ConversationContext) -> None:
        """Process a single conversation context.

        1. Feed to mem0 for long-term fact storage
        2. Update profile sections in memory.json
        """
        from deerflow.agents.middlewares.memory_middleware import _messages_to_mem0_format

        thread_id = context.thread_id

        # --- mem0: long-term semantic memory ---
        mem0_messages = _messages_to_mem0_format(context.messages)
        if mem0_messages:
            try:
                from deerflow.agents.memory.mem0_store import add_memories

                result = add_memories(mem0_messages, thread_id=thread_id)
                logger.info("mem0 updated for thread %s: %s", thread_id, result)
            except Exception as e:
                logger.error("mem0 add failed for thread %s: %s", thread_id, e)

        # --- Profile updater: slim memory.json (no facts) ---
        try:
            from deerflow.agents.memory.updater import update_profile_from_conversation

            update_profile_from_conversation(
                messages=context.messages,
                thread_id=thread_id,
                agent_name=context.agent_name,
            )
        except Exception as e:
            logger.error("Profile update failed for thread %s: %s", thread_id, e)

    def flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._process_queue()

    def clear(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._queue.clear()
            self._processing = False

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def is_processing(self) -> bool:
        with self._lock:
            return self._processing


# Global singleton instance
_memory_queue: MemoryUpdateQueue | None = None
_queue_lock = threading.Lock()


def get_memory_queue() -> MemoryUpdateQueue:
    global _memory_queue
    with _queue_lock:
        if _memory_queue is None:
            _memory_queue = MemoryUpdateQueue()
        return _memory_queue


def reset_memory_queue() -> None:
    global _memory_queue
    with _queue_lock:
        if _memory_queue is not None:
            _memory_queue.clear()
        _memory_queue = None
