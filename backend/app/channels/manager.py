"""ChannelManager — consumes inbound messages and dispatches them to the DeerFlow agent via LangGraph Server."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment
from app.channels.store import ChannelStore

logger = logging.getLogger(__name__)

DEFAULT_LANGGRAPH_URL = "http://localhost:2024"
DEFAULT_GATEWAY_URL = "http://localhost:8001"
DEFAULT_ASSISTANT_ID = "lead_agent"

DEFAULT_RUN_CONFIG: dict[str, Any] = {"recursion_limit": 50}
DEFAULT_RUN_CONTEXT: dict[str, Any] = {
    "thinking_enabled": True,
    "is_plan_mode": False,
    "subagent_enabled": False,
}
STREAM_UPDATE_MIN_INTERVAL_SECONDS = 0.35

def _stamp_message(text: str) -> str:
    """Prepend the current date/time to a user message so the agent always knows 'now'."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M, %A")
    return f"<current_date>{now}</current_date>\n{text}"


CHANNEL_CAPABILITIES = {
    "feishu": {"supports_streaming": True, "stream_update_interval": STREAM_UPDATE_MIN_INTERVAL_SECONDS},
    "slack": {"supports_streaming": True, "stream_update_interval": 10.0},
    "telegram": {"supports_streaming": False},
}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _merge_dicts(*layers: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        if isinstance(layer, Mapping):
            merged.update(layer)
    return merged


def _extract_response_text(result: dict | list) -> str:
    """Extract the last AI message text from a LangGraph runs.wait result.

    ``runs.wait`` returns the final state dict which contains a ``messages``
    list.  Each message is a dict with at least ``type`` and ``content``.

    Handles special cases:
    - Regular AI text responses
    - Clarification interrupts (``ask_clarification`` tool messages)
    - AI messages with tool_calls but no text content
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return ""

    # Walk backwards to find usable response text, but stop at the last
    # human message to avoid returning text from a previous turn.
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("type")

        # Stop at the last human message — anything before it is a previous turn
        if msg_type == "human":
            break

        # Check for tool messages from ask_clarification (interrupt case)
        if msg_type == "tool" and msg.get("name") == "ask_clarification":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content

        # Regular AI message with text content
        if msg_type == "ai":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content
            # content can be a list of content blocks
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "".join(parts)
                if text:
                    return text
    return ""


def _extract_text_content(content: Any) -> str:
    """Extract text from a streaming payload content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    nested = block.get("content")
                    if isinstance(nested, str):
                        parts.append(nested)
        return "".join(parts)
    if isinstance(content, Mapping):
        for key in ("text", "content"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    return ""


def _merge_stream_text(existing: str, chunk: str) -> str:
    """Merge either delta text or cumulative text into a single snapshot."""
    if not chunk:
        return existing
    if not existing or chunk == existing:
        return chunk or existing
    if chunk.startswith(existing):
        return chunk
    if existing.endswith(chunk):
        return existing
    return existing + chunk


def _extract_stream_message_id(payload: Any, metadata: Any) -> str | None:
    """Best-effort extraction of the streamed AI message identifier."""
    candidates = [payload, metadata]
    if isinstance(payload, Mapping):
        candidates.append(payload.get("kwargs"))

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for key in ("id", "message_id"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _accumulate_stream_text(
    buffers: dict[str, str],
    current_message_id: str | None,
    event_data: Any,
) -> tuple[str | None, str | None]:
    """Convert a ``messages-tuple`` event into the latest displayable AI text."""
    payload = event_data
    metadata: Any = None
    if isinstance(event_data, (list, tuple)):
        if event_data:
            payload = event_data[0]
        if len(event_data) > 1:
            metadata = event_data[1]

    if isinstance(payload, str):
        message_id = current_message_id or "__default__"
        buffers[message_id] = _merge_stream_text(buffers.get(message_id, ""), payload)
        return buffers[message_id], message_id

    if not isinstance(payload, Mapping):
        return None, current_message_id

    payload_type = str(payload.get("type", "")).lower()
    if "tool" in payload_type:
        return None, current_message_id

    text = _extract_text_content(payload.get("content"))
    if not text and isinstance(payload.get("kwargs"), Mapping):
        text = _extract_text_content(payload["kwargs"].get("content"))
    if not text:
        return None, current_message_id

    message_id = _extract_stream_message_id(payload, metadata) or current_message_id or "__default__"
    buffers[message_id] = _merge_stream_text(buffers.get(message_id, ""), text)
    return buffers[message_id], message_id


# -- Tool-call progress helpers --------------------------------------------

# Human-readable labels for common tool names
_TOOL_LABELS: dict[str, str] = {
    "bash": "Running command",
    "read_file": "Reading file",
    "write_file": "Writing file",
    "str_replace": "Editing file",
    "ls": "Listing directory",
    "present_files": "Preparing files",
    "web_search": "Searching the web",
    "web_fetch": "Fetching web page",
    "task": "Delegating to subagent",
    "ask_clarification": "Asking for clarification",
}


def _extract_tool_call_name(event_data: Any) -> str | None:
    """Extract the tool name from a messages-tuple event if it's an AI tool-call."""
    payload = event_data
    if isinstance(event_data, (list, tuple)) and event_data:
        payload = event_data[0]
    if not isinstance(payload, Mapping):
        return None
    if str(payload.get("type", "")).lower() != "ai":
        return None
    tool_calls = payload.get("tool_calls") or []
    if not tool_calls:
        # Also check kwargs.tool_calls
        kwargs = payload.get("kwargs")
        if isinstance(kwargs, Mapping):
            tool_calls = kwargs.get("tool_calls") or []
    if tool_calls and isinstance(tool_calls, list):
        first = tool_calls[0]
        if isinstance(first, Mapping):
            return first.get("name")
    return None


def _is_tool_result(event_data: Any) -> bool:
    """Check if a messages-tuple event is a tool result (tool finished running)."""
    payload = event_data
    if isinstance(event_data, (list, tuple)) and event_data:
        payload = event_data[0]
    if not isinstance(payload, Mapping):
        return False
    return "tool" in str(payload.get("type", "")).lower()


def _format_progress_text(tool_name: str, elapsed_seconds: int, tools_seen: list[str] | None = None) -> str:
    """Format a human-readable progress line for the currently running tool."""
    label = _TOOL_LABELS.get(tool_name, f"Using {tool_name}")
    minutes, seconds = divmod(elapsed_seconds, 60)
    if minutes > 0:
        time_str = f"{minutes}m {seconds}s"
    else:
        time_str = f"{seconds}s"
    line = f":hourglass_flowing_sand: {label}... ({time_str})"
    # Show breadcrumb of tools used so far (compact)
    if tools_seen and len(tools_seen) > 1:
        prev = [_TOOL_LABELS.get(t, t) for t in tools_seen if t != tool_name]
        if prev:
            line += f"\n:footprints: Done: {', '.join(prev[-5:])}"
    return line


def _extract_artifacts(result: dict | list) -> list[str]:
    """Extract artifact paths from the last AI response cycle only.

    Instead of reading the full accumulated ``artifacts`` state (which contains
    all artifacts ever produced in the thread), this inspects the messages after
    the last human message and collects file paths from ``present_files`` tool
    calls.  This ensures only newly-produced artifacts are returned.
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return []

    artifacts: list[str] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        # Stop at the last human message — anything before it is a previous turn
        if msg.get("type") == "human":
            break
        # Look for AI messages with present_files tool calls
        if msg.get("type") == "ai":
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict) and tc.get("name") == "present_files":
                    args = tc.get("args", {})
                    paths = args.get("filepaths", [])
                    if isinstance(paths, list):
                        artifacts.extend(p for p in paths if isinstance(p, str))
    return artifacts


def _format_artifact_text(artifacts: list[str]) -> str:
    """Format artifact paths into a human-readable text block listing filenames."""
    import posixpath

    filenames = [posixpath.basename(p) for p in artifacts]
    if len(filenames) == 1:
        return f"Created File: 📎 {filenames[0]}"
    return "Created Files: 📎 " + "、".join(filenames)


_OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs/"


def _resolve_attachments(thread_id: str, artifacts: list[str]) -> list[ResolvedAttachment]:
    """Resolve virtual artifact paths to host filesystem paths with metadata.

    Only paths under ``/mnt/user-data/outputs/`` are accepted; any other
    virtual path is rejected with a warning to prevent exfiltrating uploads
    or workspace files via IM channels.

    Skips artifacts that cannot be resolved (missing files, invalid paths)
    and logs warnings for them.
    """
    from deerflow.config.paths import get_paths

    attachments: list[ResolvedAttachment] = []
    paths = get_paths()
    outputs_dir = paths.sandbox_outputs_dir(thread_id).resolve()
    for virtual_path in artifacts:
        # Security: only allow files from the agent outputs directory
        if not virtual_path.startswith(_OUTPUTS_VIRTUAL_PREFIX):
            logger.warning("[Manager] rejected non-outputs artifact path: %s", virtual_path)
            continue
        try:
            actual = paths.resolve_virtual_path(thread_id, virtual_path)
            # Verify the resolved path is actually under the outputs directory
            # (guards against path-traversal even after prefix check)
            try:
                actual.resolve().relative_to(outputs_dir)
            except ValueError:
                logger.warning("[Manager] artifact path escapes outputs dir: %s -> %s", virtual_path, actual)
                continue
            if not actual.is_file():
                logger.warning("[Manager] artifact not found on disk: %s -> %s", virtual_path, actual)
                continue
            mime, _ = mimetypes.guess_type(str(actual))
            mime = mime or "application/octet-stream"
            attachments.append(
                ResolvedAttachment(
                    virtual_path=virtual_path,
                    actual_path=actual,
                    filename=actual.name,
                    mime_type=mime,
                    size=actual.stat().st_size,
                    is_image=mime.startswith("image/"),
                )
            )
        except (ValueError, OSError) as exc:
            logger.warning("[Manager] failed to resolve artifact %s: %s", virtual_path, exc)
    return attachments


def _prepare_artifact_delivery(
    thread_id: str,
    response_text: str,
    artifacts: list[str],
) -> tuple[str, list[ResolvedAttachment]]:
    """Resolve attachments and append filename fallbacks to the text response."""
    attachments: list[ResolvedAttachment] = []
    if not artifacts:
        return response_text, attachments

    attachments = _resolve_attachments(thread_id, artifacts)
    resolved_virtuals = {attachment.virtual_path for attachment in attachments}
    unresolved = [path for path in artifacts if path not in resolved_virtuals]

    if unresolved:
        artifact_text = _format_artifact_text(unresolved)
        response_text = (response_text + "\n\n" + artifact_text) if response_text else artifact_text

    # Always include resolved attachment filenames as a text fallback so files
    # remain discoverable even when the upload is skipped or fails.
    if attachments:
        resolved_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
        response_text = (response_text + "\n\n" + resolved_text) if response_text else resolved_text

    return response_text, attachments


class ChannelManager:
    """Core dispatcher that bridges IM channels to the DeerFlow agent.

    It reads from the MessageBus inbound queue, creates/reuses threads on
    the LangGraph Server, sends messages via ``runs.wait``, and publishes
    outbound responses back through the bus.
    """

    def __init__(
        self,
        bus: MessageBus,
        store: ChannelStore,
        *,
        max_concurrency: int = 5,
        langgraph_url: str = DEFAULT_LANGGRAPH_URL,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        assistant_id: str = DEFAULT_ASSISTANT_ID,
        default_session: dict[str, Any] | None = None,
        channel_sessions: dict[str, Any] | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self._max_concurrency = max_concurrency
        self._langgraph_url = langgraph_url
        self._gateway_url = gateway_url
        self._assistant_id = assistant_id
        self._default_session = _as_dict(default_session)
        self._channel_sessions = dict(channel_sessions or {})
        self._client = None  # lazy init — langgraph_sdk async client
        self._semaphore: asyncio.Semaphore | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    @staticmethod
    def _channel_supports_streaming(channel_name: str) -> bool:
        return CHANNEL_CAPABILITIES.get(channel_name, {}).get("supports_streaming", False)

    @staticmethod
    def _channel_update_interval(channel_name: str) -> float:
        return CHANNEL_CAPABILITIES.get(channel_name, {}).get("stream_update_interval", STREAM_UPDATE_MIN_INTERVAL_SECONDS)

    def _resolve_session_layer(self, msg: InboundMessage) -> tuple[dict[str, Any], dict[str, Any]]:
        channel_layer = _as_dict(self._channel_sessions.get(msg.channel_name))
        users_layer = _as_dict(channel_layer.get("users"))
        user_layer = _as_dict(users_layer.get(msg.user_id))
        return channel_layer, user_layer

    def _resolve_run_params(self, msg: InboundMessage, thread_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        channel_layer, user_layer = self._resolve_session_layer(msg)

        assistant_id = user_layer.get("assistant_id") or channel_layer.get("assistant_id") or self._default_session.get("assistant_id") or self._assistant_id
        if not isinstance(assistant_id, str) or not assistant_id.strip():
            assistant_id = self._assistant_id

        run_config = _merge_dicts(
            DEFAULT_RUN_CONFIG,
            self._default_session.get("config"),
            channel_layer.get("config"),
            user_layer.get("config"),
        )

        run_context = _merge_dicts(
            DEFAULT_RUN_CONTEXT,
            self._default_session.get("context"),
            channel_layer.get("context"),
            user_layer.get("context"),
            {"thread_id": thread_id},
        )

        return assistant_id, run_config, run_context

    # -- LangGraph SDK client (lazy) ----------------------------------------

    def _get_client(self):
        """Return the ``langgraph_sdk`` async client, creating it on first use."""
        if self._client is None:
            import httpx
            from langgraph_sdk import get_client

            # Agent runs can take 10-20+ minutes for complex analysis tasks.
            # Default read timeout (300s) is too short — use 1 hour.
            self._client = get_client(
                url=self._langgraph_url,
                timeout=httpx.Timeout(connect=5, read=3600, write=300, pool=5),
            )
        return self._client

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the dispatch loop."""
        if self._running:
            return
        self._running = True
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._task = asyncio.create_task(self._dispatch_loop())
        logger.info("ChannelManager started (max_concurrency=%d)", self._max_concurrency)

    async def stop(self) -> None:
        """Stop the dispatch loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("ChannelManager stopped")

    # -- dispatch loop -----------------------------------------------------

    async def _dispatch_loop(self) -> None:
        logger.info("[Manager] dispatch loop started, waiting for inbound messages")
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.get_inbound(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            logger.info(
                "[Manager] received inbound: channel=%s, chat_id=%s, type=%s, text=%r",
                msg.channel_name,
                msg.chat_id,
                msg.msg_type.value,
                msg.text[:100] if msg.text else "",
            )
            task = asyncio.create_task(self._handle_message(msg))
            task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        """Surface unhandled exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("[Manager] unhandled error in message task: %s", exc, exc_info=exc)

    async def _handle_message(self, msg: InboundMessage) -> None:
        async with self._semaphore:
            try:
                if msg.msg_type == InboundMessageType.COMMAND:
                    await self._handle_command(msg)
                else:
                    await self._handle_chat(msg)
            except Exception as exc:
                logger.exception(
                    "Error handling message from %s (chat=%s)",
                    msg.channel_name,
                    msg.chat_id,
                )
                import httpx
                if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, TimeoutError)):
                    error_text = "The agent is still working but the connection timed out. The task may complete in the background — check back shortly."
                else:
                    error_text = "An internal error occurred. Please try again."
                await self._send_error(msg, error_text)

    # -- chat handling -----------------------------------------------------

    async def _create_thread(self, client, msg: InboundMessage) -> str:
        """Create a new thread on the LangGraph Server and store the mapping."""
        thread = await client.threads.create()
        thread_id = thread["thread_id"]
        self.store.set_thread_id(
            msg.channel_name,
            msg.chat_id,
            thread_id,
            topic_id=msg.topic_id,
            user_id=msg.user_id,
        )
        logger.info("[Manager] new thread created on LangGraph Server: thread_id=%s for chat_id=%s topic_id=%s", thread_id, msg.chat_id, msg.topic_id)
        return thread_id

    async def _handle_chat(self, msg: InboundMessage, extra_context: dict[str, Any] | None = None) -> None:
        client = self._get_client()

        # Look up existing DeerFlow thread.
        # topic_id may be None (e.g. Telegram private chats) — the store
        # handles this by using the "channel:chat_id" key without a topic suffix.
        thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
        if thread_id:
            logger.info("[Manager] reusing thread: thread_id=%s for topic_id=%s", thread_id, msg.topic_id)

        # No existing thread found — create a new one
        if thread_id is None:
            thread_id = await self._create_thread(client, msg)

        assistant_id, run_config, run_context = self._resolve_run_params(msg, thread_id)
        if extra_context:
            run_context.update(extra_context)
        if self._channel_supports_streaming(msg.channel_name):
            await self._handle_streaming_chat(
                client,
                msg,
                thread_id,
                assistant_id,
                run_config,
                run_context,
            )
            return

        stamped_text = _stamp_message(msg.text)
        logger.info("[Manager] invoking runs.wait(thread_id=%s, text=%r)", thread_id, msg.text[:100])
        result = await client.runs.wait(
            thread_id,
            assistant_id,
            input={"messages": [{"role": "human", "content": stamped_text}]},
            config=run_config,
            context=run_context,
        )

        response_text = _extract_response_text(result)
        artifacts = _extract_artifacts(result)

        logger.info(
            "[Manager] agent response received: thread_id=%s, response_len=%d, artifacts=%d",
            thread_id,
            len(response_text) if response_text else 0,
            len(artifacts),
        )

        response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts)

        if not response_text:
            if attachments:
                response_text = _format_artifact_text([a.virtual_path for a in attachments])
            else:
                response_text = "(No response from agent)"

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=thread_id,
            text=response_text,
            artifacts=artifacts,
            attachments=attachments,
            thread_ts=msg.thread_ts,
        )
        logger.info("[Manager] publishing outbound message to bus: channel=%s, chat_id=%s", msg.channel_name, msg.chat_id)
        await self.bus.publish_outbound(outbound)

    async def _handle_streaming_chat(
        self,
        client,
        msg: InboundMessage,
        thread_id: str,
        assistant_id: str,
        run_config: dict[str, Any],
        run_context: dict[str, Any],
    ) -> None:
        stamped_text = _stamp_message(msg.text)
        logger.info("[Manager] invoking runs.stream(thread_id=%s, text=%r)", thread_id, msg.text[:100])

        last_values: dict[str, Any] | list | None = None
        streamed_buffers: dict[str, str] = {}
        current_message_id: str | None = None
        latest_text = ""
        last_published_text = ""
        last_publish_at = 0.0
        stream_error: BaseException | None = None

        # Tool-call progress tracking: show what the agent is doing
        active_tool: str | None = None
        tool_start_at = 0.0
        tools_seen: list[str] = []  # History of tools invoked this turn
        update_interval = self._channel_update_interval(msg.channel_name)
        stream_done = False

        async def _publish_progress(text: str) -> None:
            nonlocal last_published_text, last_publish_at
            if not text or text == last_published_text:
                return
            now = time.monotonic()
            if last_published_text and now - last_publish_at < update_interval:
                return
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel_name=msg.channel_name,
                    chat_id=msg.chat_id,
                    thread_id=thread_id,
                    text=text,
                    is_final=False,
                    thread_ts=msg.thread_ts,
                )
            )
            last_published_text = text
            last_publish_at = now

        async def _heartbeat_loop() -> None:
            """Periodically publish progress while a tool is running."""
            while not stream_done:
                await asyncio.sleep(update_interval)
                if stream_done:
                    break
                if active_tool:
                    elapsed = int(time.monotonic() - tool_start_at)
                    await _publish_progress(_format_progress_text(active_tool, elapsed, tools_seen))

        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        try:
            async for chunk in client.runs.stream(
                thread_id,
                assistant_id,
                input={"messages": [{"role": "human", "content": stamped_text}]},
                config=run_config,
                context=run_context,
                stream_mode=["messages-tuple", "values"],
            ):
                event = getattr(chunk, "event", "")
                data = getattr(chunk, "data", None)

                if event == "messages-tuple":
                    # Check for tool-call events (AI requesting a tool)
                    tool_name = _extract_tool_call_name(data)
                    if tool_name:
                        active_tool = tool_name
                        tool_start_at = time.monotonic()
                        if tool_name not in tools_seen:
                            tools_seen.append(tool_name)
                        # Immediately publish on tool transition
                        await _publish_progress(_format_progress_text(active_tool, 0, tools_seen))

                    # Check for tool-result events (tool finished)
                    if _is_tool_result(data):
                        active_tool = None

                    accumulated_text, current_message_id = _accumulate_stream_text(streamed_buffers, current_message_id, data)
                    if accumulated_text:
                        latest_text = accumulated_text
                elif event == "values" and isinstance(data, (dict, list)):
                    last_values = data
                    snapshot_text = _extract_response_text(data)
                    if snapshot_text:
                        latest_text = snapshot_text

                # Publish real AI text when available
                if latest_text:
                    await _publish_progress(latest_text)

        except Exception as exc:
            stream_error = exc
            logger.exception("[Manager] streaming error: thread_id=%s", thread_id)
        finally:
            stream_done = True
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            result = last_values if last_values is not None else {"messages": [{"type": "ai", "content": latest_text}]}
            response_text = _extract_response_text(result)
            artifacts = _extract_artifacts(result)
            response_text, attachments = _prepare_artifact_delivery(thread_id, response_text, artifacts)

            if not response_text:
                if attachments:
                    response_text = _format_artifact_text([attachment.virtual_path for attachment in attachments])
                elif stream_error:
                    response_text = "An error occurred while processing your request. Please try again."
                else:
                    response_text = latest_text or "(No response from agent)"

            logger.info(
                "[Manager] streaming response completed: thread_id=%s, response_len=%d, artifacts=%d, error=%s",
                thread_id,
                len(response_text),
                len(artifacts),
                stream_error,
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel_name=msg.channel_name,
                    chat_id=msg.chat_id,
                    thread_id=thread_id,
                    text=response_text,
                    artifacts=artifacts,
                    attachments=attachments,
                    is_final=True,
                    thread_ts=msg.thread_ts,
                )
            )

    # -- command handling --------------------------------------------------

    async def _handle_command(self, msg: InboundMessage) -> None:
        text = msg.text.strip()
        parts = text.split(maxsplit=1)
        command = parts[0].lower().lstrip("/")

        if command == "bootstrap":
            from dataclasses import replace as _dc_replace

            chat_text = parts[1] if len(parts) > 1 else "Initialize workspace"
            chat_msg = _dc_replace(msg, text=chat_text, msg_type=InboundMessageType.CHAT)
            await self._handle_chat(chat_msg, extra_context={"is_bootstrap": True})
            return

        if command == "new":
            # Create a new thread on the LangGraph Server
            client = self._get_client()
            thread = await client.threads.create()
            new_thread_id = thread["thread_id"]
            self.store.set_thread_id(
                msg.channel_name,
                msg.chat_id,
                new_thread_id,
                topic_id=msg.topic_id,
                user_id=msg.user_id,
            )
            reply = "New conversation started."
        elif command == "status":
            thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
            reply = f"Active thread: {thread_id}" if thread_id else "No active conversation."
        elif command == "models":
            reply = await self._fetch_gateway("/api/models", "models")
        elif command == "memory":
            reply = await self._fetch_gateway("/api/memory", "memory")
        elif command == "help":
            reply = (
                "Available commands:\n"
                "/bootstrap — Start a bootstrap session (enables agent setup)\n"
                "/new — Start a new conversation\n"
                "/status — Show current thread info\n"
                "/models — List available models\n"
                "/memory — Show memory status\n"
                "/help — Show this help"
            )
        else:
            reply = f"Unknown command: /{command}. Type /help for available commands."

        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
            text=reply,
            thread_ts=msg.thread_ts,
        )
        await self.bus.publish_outbound(outbound)

    async def _fetch_gateway(self, path: str, kind: str) -> str:
        """Fetch data from the Gateway API for command responses."""
        import httpx

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(f"{self._gateway_url}{path}", timeout=10)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Failed to fetch %s from gateway", kind)
            return f"Failed to fetch {kind} information."

        if kind == "models":
            names = [m["name"] for m in data.get("models", [])]
            return ("Available models:\n" + "\n".join(f"• {n}" for n in names)) if names else "No models configured."
        elif kind == "memory":
            facts = data.get("facts", [])
            return f"Memory contains {len(facts)} fact(s)."
        return str(data)

    # -- error helper ------------------------------------------------------

    async def _send_error(self, msg: InboundMessage, error_text: str) -> None:
        outbound = OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
            text=error_text,
            thread_ts=msg.thread_ts,
        )
        await self.bus.publish_outbound(outbound)
