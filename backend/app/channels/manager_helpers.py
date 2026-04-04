"""Helper functions for ChannelManager — stream processing, artifact resolution, progress formatting.

Extracted from manager.py to keep the main module focused on dispatch logic.
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.channels.message_bus import ResolvedAttachment

logger = logging.getLogger(__name__)


def stamp_message(text: str) -> str:
    """Prepend the current date/time to a user message so the agent always knows 'now'."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M, %A")
    return f"<current_date>{now}</current_date>\n{text}"


def extract_response_text(result: dict | list) -> str:
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

    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type")
        if msg_type == "human":
            break
        if msg_type == "tool" and msg.get("name") == "ask_clarification":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content
        if msg_type == "ai":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content
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


def extract_text_content(content: Any) -> str:
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


def merge_stream_text(existing: str, chunk: str) -> str:
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


def extract_stream_message_id(payload: Any, metadata: Any) -> str | None:
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


def accumulate_stream_text(
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
        buffers[message_id] = merge_stream_text(buffers.get(message_id, ""), payload)
        return buffers[message_id], message_id

    if not isinstance(payload, Mapping):
        return None, current_message_id

    payload_type = str(payload.get("type", "")).lower()
    if "tool" in payload_type:
        return None, current_message_id

    text = extract_text_content(payload.get("content"))
    if not text and isinstance(payload.get("kwargs"), Mapping):
        text = extract_text_content(payload["kwargs"].get("content"))
    if not text:
        return None, current_message_id

    message_id = extract_stream_message_id(payload, metadata) or current_message_id or "__default__"
    buffers[message_id] = merge_stream_text(buffers.get(message_id, ""), text)
    return buffers[message_id], message_id


# -- Tool-call progress helpers ----------------------------------------------

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


def extract_tool_call_name(event_data: Any) -> str | None:
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
        kwargs = payload.get("kwargs")
        if isinstance(kwargs, Mapping):
            tool_calls = kwargs.get("tool_calls") or []
    if tool_calls and isinstance(tool_calls, list):
        first = tool_calls[0]
        if isinstance(first, Mapping):
            return first.get("name")
    return None


def is_tool_result(event_data: Any) -> bool:
    """Check if a messages-tuple event is a tool result (tool finished running)."""
    payload = event_data
    if isinstance(event_data, (list, tuple)) and event_data:
        payload = event_data[0]
    if not isinstance(payload, Mapping):
        return False
    return "tool" in str(payload.get("type", "")).lower()


def format_progress_text(tool_name: str, elapsed_seconds: int, tools_seen: list[str] | None = None) -> str:
    """Format a human-readable progress line for the currently running tool."""
    label = _TOOL_LABELS.get(tool_name, f"Using {tool_name}")
    minutes, seconds = divmod(elapsed_seconds, 60)
    time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
    line = f":hourglass_flowing_sand: {label}... ({time_str})"
    if tools_seen and len(tools_seen) > 1:
        prev = [_TOOL_LABELS.get(t, t) for t in tools_seen if t != tool_name]
        if prev:
            line += f"\n:footprints: Done: {', '.join(prev[-5:])}"
    return line


def extract_artifacts(result: dict | list) -> list[str]:
    """Extract artifact paths from the last AI response cycle only."""
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
        if msg.get("type") == "human":
            break
        if msg.get("type") == "ai":
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict) and tc.get("name") == "present_files":
                    args = tc.get("args", {})
                    paths = args.get("filepaths", [])
                    if isinstance(paths, list):
                        artifacts.extend(p for p in paths if isinstance(p, str))
    return artifacts


def format_artifact_text(artifacts: list[str]) -> str:
    """Format artifact paths into a human-readable text block listing filenames."""
    import posixpath
    filenames = [posixpath.basename(p) for p in artifacts]
    if len(filenames) == 1:
        return f"Created File: 📎 {filenames[0]}"
    return "Created Files: 📎 " + "、".join(filenames)


_OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs/"


def resolve_attachments(thread_id: str, artifacts: list[str]) -> list[ResolvedAttachment]:
    """Resolve virtual artifact paths to host filesystem paths with metadata."""
    from deerflow.config.paths import get_paths

    attachments: list[ResolvedAttachment] = []
    paths = get_paths()
    outputs_dir = paths.sandbox_outputs_dir(thread_id).resolve()
    for virtual_path in artifacts:
        if not virtual_path.startswith(_OUTPUTS_VIRTUAL_PREFIX):
            logger.warning("[Manager] rejected non-outputs artifact path: %s", virtual_path)
            continue
        try:
            actual = paths.resolve_virtual_path(thread_id, virtual_path)
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


def prepare_artifact_delivery(
    thread_id: str,
    response_text: str,
    artifacts: list[str],
) -> tuple[str, list[ResolvedAttachment]]:
    """Resolve attachments and append filename fallbacks to the text response."""
    attachments: list[ResolvedAttachment] = []
    if not artifacts:
        return response_text, attachments

    attachments = resolve_attachments(thread_id, artifacts)
    resolved_virtuals = {attachment.virtual_path for attachment in attachments}
    unresolved = [path for path in artifacts if path not in resolved_virtuals]

    if unresolved:
        artifact_text = format_artifact_text(unresolved)
        response_text = (response_text + "\n\n" + artifact_text) if response_text else artifact_text

    if attachments:
        resolved_text = format_artifact_text([attachment.virtual_path for attachment in attachments])
        response_text = (response_text + "\n\n" + resolved_text) if response_text else resolved_text

    return response_text, attachments
