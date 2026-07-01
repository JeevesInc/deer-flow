"""ChannelManager — consumes inbound messages and dispatches them to the DeerFlow agent via LangGraph Server."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from app.channels.manager_helpers import (
    accumulate_stream_text,
    checkpoint_age_seconds,
    extract_artifacts,
    extract_response_text,
    extract_tool_call_name,
    format_artifact_text,
    format_progress_text,
    is_tool_result,
    prepare_artifact_delivery,
    stamp_message,
)
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage
from app.channels.store import ChannelStore

logger = logging.getLogger(__name__)

DEFAULT_LANGGRAPH_URL = "http://localhost:2024"
DEFAULT_GATEWAY_URL = "http://localhost:8001"
DEFAULT_ASSISTANT_ID = "lead_agent"


def _build_human_content(stamped_text: str, msg: InboundMessage) -> str | list[dict[str, Any]]:
    """Build the HumanMessage content payload.

    Returns a plain string for text-only messages, or a list of multimodal
    content blocks (text + image_url) when the channel attached images via
    ``metadata["image_blocks"]``.
    """
    image_blocks = msg.metadata.get("image_blocks") if msg.metadata else None
    if not image_blocks:
        return stamped_text
    return [{"type": "text", "text": stamped_text}, *image_blocks]

# Maximum wall-clock time (seconds) a single runs.wait() call may block
# before the gateway cancels the run and returns an error to the user.
RUN_TIMEOUT_SECONDS = 20 * 60  # 20 minutes


def classify_error_text(exc: BaseException) -> str:
    """Map an exception raised during a run into a short, user-facing message.

    Shared by the non-streaming (``runs.wait``) and streaming error paths so
    both surface the same friendly text instead of a raw stack trace.
    """
    import httpx

    err_str = str(exc).lower()
    if isinstance(exc, TimeoutError) and "timed out" in err_str:
        return (
            "This task took too long and was cancelled after "
            f"{RUN_TIMEOUT_SECONDS // 60} minutes. "
            "Try breaking it into smaller steps, or reply **continue** "
            "to pick up where I left off."
        )
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "The agent is still working but the connection timed out. The task may complete in the background — check back shortly."
    if isinstance(exc, httpx.ConnectError):
        return "I can't reach the agent engine right now — it may be restarting. Try again in a moment."
    if "usage limit" in err_str or "specified api usage" in err_str:
        return (
            "I've hit the Anthropic API usage limit for this billing period, so "
            "I can't respond right now. Access resets at the start of next month — "
            "or raise the cap in the Anthropic Console to restore it sooner."
        )
    if "recursion" in err_str or "recursion_limit" in err_str:
        return (
            "I hit my step limit before finishing. "
            "Reply **continue** in this thread and I'll pick up where I left off."
        )
    return "An internal error occurred. Please try again."

DEFAULT_RUN_CONFIG: dict[str, Any] = {"recursion_limit": 50}
DEFAULT_RUN_CONTEXT: dict[str, Any] = {
    "thinking_enabled": True,
    "is_plan_mode": False,
    "subagent_enabled": False,
}
STREAM_UPDATE_MIN_INTERVAL_SECONDS = 0.35

CHANNEL_CAPABILITIES = {
    "feishu": {"supports_streaming": True, "stream_update_interval": STREAM_UPDATE_MIN_INTERVAL_SECONDS},
    "slack": {"supports_streaming": False},
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
            # Read timeout must exceed RUN_TIMEOUT_SECONDS so that our
            # asyncio.wait_for() fires before the HTTP timeout does.
            self._client = get_client(
                url=self._langgraph_url,
                timeout=httpx.Timeout(connect=5, read=RUN_TIMEOUT_SECONDS + 120, write=300, pool=5),
            )
        return self._client

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the dispatch loop."""
        if self._running:
            return
        await self._cancel_zombie_runs()
        self._running = True
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._task = asyncio.create_task(self._dispatch_loop())
        self._start_stuck_run_monitor()
        logger.info("ChannelManager started (max_concurrency=%d)", self._max_concurrency)

    async def _cancel_zombie_runs(self) -> None:
        """Cancel any runs left in 'running' state from a previous process.

        On restart, the in-memory LangGraph queue may resume orphaned runs
        that will never complete, blocking the per-thread enqueue strategy.
        """
        try:
            client = self._get_client()
            busy_threads = await client.threads.search(status="busy", limit=100)
            if not busy_threads:
                logger.info("[Manager] no zombie runs found on startup")
                return
            cancelled = 0
            for thread in busy_threads:
                tid = thread["thread_id"]
                try:
                    runs = await client.runs.list(tid)
                    for run in runs:
                        if run["status"] in ("running", "pending"):
                            try:
                                await client.runs.cancel(tid, run["run_id"])
                                cancelled += 1
                                logger.info(
                                    "[Manager] cancelled zombie run %s on thread %s (was %s)",
                                    run["run_id"], tid, run["status"],
                                )
                            except Exception:
                                logger.warning("[Manager] failed to cancel run %s on thread %s", run["run_id"], tid, exc_info=True)
                except Exception:
                    logger.warning("[Manager] failed to list runs for thread %s", tid, exc_info=True)
            logger.info("[Manager] zombie cleanup complete: cancelled %d run(s) across %d busy thread(s)", cancelled, len(busy_threads))
        except Exception:
            logger.warning("[Manager] zombie cleanup failed (non-fatal, continuing startup)", exc_info=True)

    def _start_stuck_run_monitor(self) -> None:
        """Launch the stuck-run monitor in a dedicated daemon thread.

        Runs its own asyncio event loop and HTTP client so it cannot be
        blocked by the gateway's main event loop (which may be saturated
        by long-running ``runs.stream`` calls).
        """
        import threading

        def _monitor_loop():
            import asyncio as _aio

            POLL_INTERVAL = 30   # seconds between checks
            STUCK_THRESHOLD = 600  # 10 min with no state update → stuck

            async def _run():
                import httpx
                from langgraph_sdk import get_client

                client = get_client(
                    url=self._langgraph_url,
                    timeout=httpx.Timeout(connect=5, read=30, write=30, pool=5),
                )
                logger.info("[Monitor] stuck-run monitor started (poll=%ds, threshold=%ds)", POLL_INTERVAL, STUCK_THRESHOLD)

                while self._running:
                    await _aio.sleep(POLL_INTERVAL)
                    try:
                        busy_threads = await client.threads.search(status="busy", limit=100)
                        if not busy_threads:
                            continue
                        logger.info("[Monitor] found %d busy thread(s), checking ages", len(busy_threads))

                        now = datetime.now(timezone.utc)
                        cancelled = 0
                        for thread in busy_threads:
                            tid = thread["thread_id"]
                            updated_raw = thread.get("state_updated_at") or thread.get("updated_at")
                            if not updated_raw:
                                continue
                            if isinstance(updated_raw, str):
                                updated_at = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                            else:
                                updated_at = updated_raw
                            thread_age = (now - updated_at).total_seconds()
                            if thread_age < STUCK_THRESHOLD:
                                continue

                            try:
                                runs = await client.runs.list(tid)
                                active_runs = [r for r in runs if r["status"] in ("running", "pending")]
                                if active_runs:
                                    # Before cancelling, check if any active run was
                                    # updated recently.  The thread-level timestamp can
                                    # lag behind actual run progress (e.g. tool calls
                                    # that update the run but not the thread state).
                                    any_run_active = False
                                    for run in active_runs:
                                        run_updated_raw = run.get("updated_at") or run.get("created_at")
                                        if run_updated_raw:
                                            if isinstance(run_updated_raw, str):
                                                run_updated = datetime.fromisoformat(run_updated_raw.replace("Z", "+00:00"))
                                            else:
                                                run_updated = run_updated_raw
                                            run_age = (now - run_updated).total_seconds()
                                            if run_age < STUCK_THRESHOLD:
                                                any_run_active = True
                                                logger.info(
                                                    "[Monitor] thread %s looks stale (%.0fs) but run %s "
                                                    "was updated %.0fs ago — skipping",
                                                    tid, thread_age, run["run_id"], run_age,
                                                )
                                                break
                                    if not any_run_active:
                                        # Run rows freeze their updated_at at run start, so a
                                        # long healthy run looks idle. Consult the real progress
                                        # signal before killing: the latest checkpoint write time
                                        # (a checkpoint lands on every graph superstep).
                                        try:
                                            state = await client.threads.get_state(tid)
                                            ckpt_age = checkpoint_age_seconds(state, now)
                                            if ckpt_age is not None and ckpt_age < STUCK_THRESHOLD:
                                                any_run_active = True
                                                logger.info(
                                                    "[Monitor] thread %s run-row stale (%.0fs) but "
                                                    "checkpoint written %.0fs ago — skipping",
                                                    tid, thread_age, ckpt_age,
                                                )
                                        except Exception:
                                            logger.warning(
                                                "[Monitor] checkpoint freshness check failed for thread %s — falling back to run age",
                                                tid, exc_info=True,
                                            )
                                    if any_run_active:
                                        continue

                                    for run in active_runs:
                                        try:
                                            await client.runs.cancel(tid, run["run_id"])
                                            cancelled += 1
                                            logger.warning(
                                                "[Monitor] cancelled stuck run %s on thread %s "
                                                "(status=%s, idle %.0fs)",
                                                run["run_id"], tid, run["status"], thread_age,
                                            )
                                        except Exception:
                                            logger.warning("[Monitor] failed to cancel run %s on thread %s", run["run_id"], tid, exc_info=True)
                                else:
                                    # Thread is busy but has no active runs — zombie state.
                                    # Unstick by writing empty state update.
                                    try:
                                        await client.threads.update_state(tid, values={"messages": []})
                                        cancelled += 1
                                        logger.warning(
                                            "[Monitor] unstuck zombie busy thread %s (no active runs, idle %.0fs)",
                                            tid, thread_age,
                                        )
                                    except Exception:
                                        logger.warning("[Monitor] failed to unstick thread %s", tid, exc_info=True)
                            except Exception:
                                logger.warning("[Monitor] failed to list runs for thread %s", tid, exc_info=True)

                        if cancelled:
                            logger.info("[Monitor] stuck-run sweep: cancelled/unstuck %d thread(s)", cancelled)
                    except Exception:
                        logger.warning("[Monitor] stuck-run check failed (will retry)", exc_info=True)

                logger.info("[Monitor] stuck-run monitor stopped")

            _aio.run(_run())

        t = threading.Thread(target=_monitor_loop, name="stuck-run-monitor", daemon=True)
        t.start()
        logger.info("[Monitor] monitor thread launched")

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
        # Monitor thread is a daemon — dies automatically with the process
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
        if self._semaphore is None:
            logger.error("[Manager] semaphore not initialized — dropping message from %s", msg.channel_name)
            return
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
                await self._send_error(msg, classify_error_text(exc))

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
            # Verify the thread still exists on LangGraph (it uses an in-memory
            # thread registry that is lost on restart).
            try:
                await client.threads.get(thread_id)
                logger.info("[Manager] reusing thread: thread_id=%s for topic_id=%s", thread_id, msg.topic_id)
            except Exception:
                logger.warning("[Manager] stored thread %s no longer exists on LangGraph, creating new thread", thread_id)
                thread_id = None

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

        stamped_text = stamp_message(msg.text)
        human_content = _build_human_content(stamped_text, msg)
        n_images = len(msg.metadata.get("image_blocks", [])) if msg.metadata else 0
        logger.info("[Manager] invoking runs.wait(thread_id=%s, text=%r, images=%d)", thread_id, msg.text[:100], n_images)
        try:
            result = await asyncio.wait_for(
                client.runs.wait(
                    thread_id,
                    assistant_id,
                    input={"messages": [{"role": "human", "content": human_content}]},
                    config=run_config,
                    context=run_context,
                ),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.error("[Manager] run timed out after %ds on thread %s — cancelling", RUN_TIMEOUT_SECONDS, thread_id)
            await self._cancel_active_runs(client, thread_id)
            raise TimeoutError(f"Run timed out after {RUN_TIMEOUT_SECONDS // 60} minutes")

        response_text = extract_response_text(result)
        artifacts = extract_artifacts(result)

        logger.info(
            "[Manager] agent response received: thread_id=%s, response_len=%d, artifacts=%d",
            thread_id,
            len(response_text) if response_text else 0,
            len(artifacts),
        )

        response_text, attachments = prepare_artifact_delivery(thread_id, response_text, artifacts)

        if not response_text:
            if attachments:
                response_text = format_artifact_text([a.virtual_path for a in attachments])
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
        stamped_text = stamp_message(msg.text)
        human_content = _build_human_content(stamped_text, msg)
        n_images = len(msg.metadata.get("image_blocks", [])) if msg.metadata else 0
        logger.info("[Manager] invoking runs.stream(thread_id=%s, text=%r, images=%d)", thread_id, msg.text[:100], n_images)

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
                    await _publish_progress(format_progress_text(active_tool, elapsed, tools_seen))

        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        async def _stream_iteration() -> None:
            nonlocal last_values, current_message_id, latest_text, active_tool, tool_start_at
            async for chunk in client.runs.stream(
                thread_id,
                assistant_id,
                input={"messages": [{"role": "human", "content": human_content}]},
                config=run_config,
                context=run_context,
                stream_mode=["messages-tuple", "values"],
            ):
                event = getattr(chunk, "event", "")
                data = getattr(chunk, "data", None)

                if event == "messages-tuple":
                    tool_name = extract_tool_call_name(data)
                    if tool_name:
                        active_tool = tool_name
                        tool_start_at = time.monotonic()
                        if tool_name not in tools_seen:
                            tools_seen.append(tool_name)
                        await _publish_progress(format_progress_text(active_tool, 0, tools_seen))

                    if is_tool_result(data):
                        active_tool = None

                    accumulated_text, current_message_id = accumulate_stream_text(streamed_buffers, current_message_id, data)
                    if accumulated_text:
                        latest_text = accumulated_text
                elif event == "values" and isinstance(data, (dict, list)):
                    last_values = data
                    snapshot_text = extract_response_text(data)
                    if snapshot_text:
                        latest_text = snapshot_text

                if latest_text:
                    await _publish_progress(latest_text)

        try:
            await asyncio.wait_for(_stream_iteration(), timeout=RUN_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):
            stream_error = TimeoutError(f"Streaming run timed out after {RUN_TIMEOUT_SECONDS // 60} minutes")
            logger.error("[Manager] streaming run timed out after %ds on thread %s — cancelling", RUN_TIMEOUT_SECONDS, thread_id)
            await self._cancel_active_runs(client, thread_id)
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
            response_text = extract_response_text(result)
            artifacts = extract_artifacts(result)
            response_text, attachments = prepare_artifact_delivery(thread_id, response_text, artifacts)

            if not response_text:
                if attachments:
                    response_text = format_artifact_text([attachment.virtual_path for attachment in attachments])
                elif stream_error:
                    response_text = classify_error_text(stream_error)
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
        command = parts[0].lower().lstrip("/!")

        if command == "bootstrap":
            from dataclasses import replace as _dc_replace

            chat_text = parts[1] if len(parts) > 1 else "Initialize workspace"
            chat_msg = _dc_replace(msg, text=chat_text, msg_type=InboundMessageType.CHAT)
            await self._handle_chat(chat_msg, extra_context={"is_bootstrap": True})
            return

        if command == "learn":
            from dataclasses import replace as _dc_replace

            chat_msg = _dc_replace(
                msg,
                text=(
                    "Run the latent-learning skill now. Analyse memory facts and completed work to identify "
                    "recurring task domains, draft specialist sub-agent specs, write them to the agents-draft "
                    "directory, and report back a summary of what was proposed."
                ),
                msg_type=InboundMessageType.CHAT,
            )
            await self._handle_chat(chat_msg)
            return

        if command == "promote":
            reply = await self._promote_agent(parts[1].strip() if len(parts) > 1 else "")
            outbound = OutboundMessage(
                channel_name=msg.channel_name,
                chat_id=msg.chat_id,
                thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
                text=reply,
                thread_ts=msg.thread_ts,
            )
            await self.bus.publish_outbound(outbound)
            return

        if command == "reject":
            reply = await self._reject_agent(parts[1].strip() if len(parts) > 1 else "")
            outbound = OutboundMessage(
                channel_name=msg.channel_name,
                chat_id=msg.chat_id,
                thread_id=self.store.get_thread_id(msg.channel_name, msg.chat_id) or "",
                text=reply,
                thread_ts=msg.thread_ts,
            )
            await self.bus.publish_outbound(outbound)
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
        elif command == "btw":
            reply = await self._btw_status(msg)
        elif command == "help":
            reply = (
                "Available commands:\n"
                "/bootstrap — Start a bootstrap session (enables agent setup)\n"
                "/new — Start a new conversation\n"
                "/status — Show current thread info\n"
                "/btw — Quick system health check\n"
                "/models — List available models\n"
                "/memory — Show memory status\n"
                "!learn — Analyse task history and propose specialist agents\n"
                "!promote <name> — Activate a drafted specialist agent\n"
                "!reject <name> — Delete a drafted specialist agent\n"
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

    async def _promote_agent(self, name: str) -> str:
        """Move an agent draft into the live agents directory."""
        import re
        import shutil

        from deerflow.config.paths import get_paths

        if not name or not re.match(r"^[A-Za-z0-9-]+$", name):
            return "Usage: /promote <agent-name>  (letters, digits, hyphens only)"

        paths = get_paths()
        draft_dir = paths.base_dir / "agents-draft" / name.lower()
        live_dir = paths.agent_dir(name)

        if not draft_dir.exists():
            return f"No draft found for `{name}`. Run `/learn` to generate drafts."

        if live_dir.exists():
            return f"Agent `{name}` already exists. Delete it first if you want to replace it."

        try:
            shutil.copytree(str(draft_dir), str(live_dir))
            shutil.rmtree(str(draft_dir))
        except Exception as exc:
            logger.exception("Failed to promote agent %s", name)
            return f"Error promoting `{name}`: {exc}"

        return f"Agent `{name}` is now live. The lead agent will delegate to it on matching tasks."

    async def _reject_agent(self, name: str) -> str:
        """Delete a draft agent without promoting it."""
        import re
        import shutil

        from deerflow.config.paths import get_paths

        if not name or not re.match(r"^[A-Za-z0-9-]+$", name):
            return "Usage: /reject <agent-name>"

        paths = get_paths()
        draft_dir = paths.base_dir / "agents-draft" / name.lower()

        if not draft_dir.exists():
            return f"No draft found for `{name}`."

        try:
            shutil.rmtree(str(draft_dir))
        except Exception as exc:
            logger.exception("Failed to reject agent %s", name)
            return f"Error deleting draft `{name}`: {exc}"

        return f"Draft `{name}` deleted."

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

    async def _btw_status(self, msg: InboundMessage) -> str:
        """Quick system health check: this thread, busy threads, recent errors."""
        lines: list[str] = []
        try:
            client = self._get_client()

            # This thread
            thread_id = self.store.get_thread_id(msg.channel_name, msg.chat_id, topic_id=msg.topic_id)
            if thread_id:
                try:
                    t = await client.threads.get(thread_id)
                    status = t.get("status", "?")
                    msg_count = len(t.get("values", {}).get("messages", []))
                    lines.append(f"*This thread:* `{status}` ({msg_count} msgs)")
                except Exception:
                    lines.append("*This thread:* not found on server (stale)")
            else:
                lines.append("*This thread:* no active conversation")

            # Busy threads (system-wide)
            busy = await client.threads.search(status="busy", limit=10)
            if busy:
                lines.append(f"\n*Busy threads:* {len(busy)}")
                now = datetime.now(timezone.utc)
                for bt in busy[:5]:
                    updated_raw = bt.get("state_updated_at") or bt.get("updated_at", "")
                    if isinstance(updated_raw, str) and updated_raw:
                        updated_at = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                        age = int((now - updated_at).total_seconds())
                        age_str = f"{age // 60}m{age % 60}s" if age >= 60 else f"{age}s"
                    else:
                        age_str = "?"
                    bt_msgs = len(bt.get("values", {}).get("messages", []))
                    lines.append(f"  • `{bt['thread_id'][:8]}…` — {bt_msgs} msgs, idle {age_str}")
            else:
                lines.append("\n*Busy threads:* none")

            # Recent errors
            errored = await client.threads.search(status="error", limit=5)
            if errored:
                lines.append(f"*Errored threads:* {len(errored)}")
            else:
                lines.append("*Errored threads:* none")

        except Exception:
            logger.exception("[Manager] /btw status check failed")
            return "Failed to fetch status."

        return "\n".join(lines)

    # -- run lifecycle helpers ------------------------------------------------

    async def _cancel_active_runs(self, client, thread_id: str) -> None:
        """Cancel all active (running/pending) runs on a thread."""
        try:
            runs = await client.runs.list(thread_id)
            for run in runs:
                if run["status"] in ("running", "pending"):
                    try:
                        await client.runs.cancel(thread_id, run["run_id"])
                        logger.info("[Manager] cancelled run %s on thread %s", run["run_id"], thread_id)
                    except Exception:
                        logger.warning("[Manager] failed to cancel run %s on thread %s", run["run_id"], thread_id, exc_info=True)
        except Exception:
            logger.warning("[Manager] failed to list runs for thread %s during cancel", thread_id, exc_info=True)

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
