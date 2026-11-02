"""Semantic retrieval layer for STRATEGIC_CONTEXT.md.

The strategic context file is a living document (rewritten by the dreams and
knowledge-crawler crons) that is too large to inject wholesale on every turn
(~22KB / ~5-6K tokens). Instead this module indexes it into a dedicated mem0
namespace so each request can pull only the *semantically relevant* sections.

Design:
- The file is split into coherent chunks along ``##``/``###`` headers, with
  large sections sub-split at line boundaries (table rows / bullets) so no
  single chunk is huge. Each chunk keeps its section header for context.
- Chunks are stored with ``infer=False`` so mem0 keeps them verbatim instead
  of atomizing them into extracted "facts".
- Re-indexing is driven by a content hash: ``sync_if_changed()`` is a cheap
  no-op unless the file changed since the last index, so normal turns pay
  essentially nothing.
- ``search_strategic_context()`` gates on a similarity threshold, so turns that
  aren't strategy-adjacent retrieve nothing.

Tuning knobs are environment variables (hot-adjustable, no restart to retune):
    STRATEGIC_CONTEXT_ENABLED         (default "true")
    STRATEGIC_CONTEXT_MIN_SCORE       (default "0.22")
    STRATEGIC_CONTEXT_TOP_K           (default "3")
    STRATEGIC_CONTEXT_MAX_CHUNK_CHARS (default "1200")

The 0.22 default was tuned empirically against this file's embeddings
(sentence-transformers/all-MiniLM-L6-v2): off-topic queries score <=0.15 while
on-topic queries floor around 0.25, so 0.22 sits in the gap. Retune via the env
var if the file's content mix shifts materially.
"""

import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

FILENAME = "STRATEGIC_CONTEXT.md"
_STATE_FILENAME = "_strategic_context_index.json"

_sync_lock = threading.Lock()

# Markdown table separator row, e.g. "|----|----|" — used to detect the header
# block of a table so it can be repeated at the top of each table sub-chunk.
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}")


def is_enabled() -> bool:
    return os.environ.get("STRATEGIC_CONTEXT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def get_min_score() -> float:
    try:
        return float(os.environ.get("STRATEGIC_CONTEXT_MIN_SCORE", "0.22"))
    except ValueError:
        return 0.22


def get_top_k() -> int:
    try:
        return int(os.environ.get("STRATEGIC_CONTEXT_TOP_K", "3"))
    except ValueError:
        return 3


def get_max_chunk_chars() -> int:
    try:
        return int(os.environ.get("STRATEGIC_CONTEXT_MAX_CHUNK_CHARS", "1200"))
    except ValueError:
        return 1200


def get_file_path() -> Path:
    from deerflow.config.paths import get_paths

    return get_paths().base_dir / FILENAME


def _state_path() -> Path:
    from deerflow.config.paths import get_paths

    return get_paths().base_dir / _STATE_FILENAME


def file_hash(text: str) -> str:
    """Stable content hash used to detect when a re-index is needed."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _flush_chunk(section: str, header_lines: list[str], body_lines: list[str], out: list[dict]) -> None:
    """Emit one chunk = section title + optional table header + body lines."""
    body = "\n".join(body_lines).strip()
    if not body:
        return
    parts = [f"## {section}"] if section else []
    parts.extend(header_lines)
    parts.append(body)
    out.append({"section": section, "text": "\n".join(parts).strip()})


def split_sections(text: str, max_chunk_chars: int | None = None) -> list[dict]:
    """Split the strategic context markdown into retrieval chunks.

    Returns a list of ``{"section": <title>, "text": <chunk>}`` dicts. Small
    sections become a single chunk; large sections are sub-split at line
    boundaries (never mid-line), with the section header — and the table header
    rows, for tables — repeated at the top of each sub-chunk so every chunk is
    self-describing.
    """
    if max_chunk_chars is None:
        max_chunk_chars = get_max_chunk_chars()

    lines = text.splitlines()

    # Partition into top-level (## ) sections, ignoring the H1 title / preamble.
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_body: list[str] = []
    for line in lines:
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            if current_title or current_body:
                sections.append((current_title, current_body))
            current_title = m.group(1).strip()
            current_body = []
        else:
            current_body.append(line)
    if current_title or current_body:
        sections.append((current_title, current_body))

    chunks: list[dict] = []
    for title, body_lines in sections:
        # Drop leading horizontal rules / blank lines that carry no content.
        cleaned = [ln for ln in body_lines if ln.strip() not in ("", "---")]
        if not cleaned:
            continue

        if not title:
            # Preamble / no header: drop the H1 title line (noise) but keep any
            # real content (e.g. the "last update / corrections" note), which is
            # genuinely useful strategic context.
            preamble = [ln for ln in cleaned if not re.match(r"^#\s+", ln)]
            preamble_text = "\n".join(preamble).strip()
            if preamble_text:
                chunks.append({"section": "", "text": preamble_text})
            continue

        section_text = "\n".join(cleaned).strip()

        if len(section_text) <= max_chunk_chars:
            chunks.append({"section": title, "text": f"## {title}\n{section_text}".strip()})
            continue

        # Large section: sub-split at line boundaries. Detect a leading markdown
        # table header (header row + separator row) and repeat it per sub-chunk.
        table_header: list[str] = []
        content_lines = cleaned
        if len(cleaned) >= 2 and cleaned[0].lstrip().startswith("|") and _TABLE_SEP_RE.match(cleaned[1]):
            table_header = [cleaned[0], cleaned[1]]
            content_lines = cleaned[2:]

        buf: list[str] = []
        buf_len = 0
        base_len = len(title) + len("\n".join(table_header)) + 8
        for ln in content_lines:
            add_len = len(ln) + 1
            if buf and (buf_len + add_len + base_len) > max_chunk_chars:
                _flush_chunk(title, table_header, buf, chunks)
                buf, buf_len = [], 0
            buf.append(ln)
            buf_len += add_len
        _flush_chunk(title, table_header, buf, chunks)

    return chunks


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def sync_if_changed(force: bool = False) -> bool:
    """Re-index the strategic context into mem0 iff the file changed.

    Cheap no-op on the common path: reads the file, hashes it, and returns
    immediately if the hash matches the last indexed hash. On change (or
    ``force``), clears the namespace and re-adds every chunk verbatim.

    Returns True if a re-index was performed, False otherwise.
    """
    if not is_enabled():
        return False

    path = get_file_path()
    if not path.exists():
        return False

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("strategic_context: could not read %s: %s", path, e)
        return False

    h = file_hash(text)
    if not force and _read_state().get("hash") == h:
        return False

    with _sync_lock:
        # Re-check under the lock in case another thread just indexed it.
        if not force and _read_state().get("hash") == h:
            return False

        chunks = split_sections(text)
        if not chunks:
            return False

        try:
            from deerflow.agents.memory.mem0_store import (
                STRATEGIC_CONTEXT_USER_ID,
                add_memories,
                delete_all_memories,
            )

            delete_all_memories(user_id=STRATEGIC_CONTEXT_USER_ID)
            for i, chunk in enumerate(chunks):
                add_memories(
                    [{"role": "user", "content": chunk["text"]}],
                    user_id=STRATEGIC_CONTEXT_USER_ID,
                    infer=False,
                    metadata={"section": chunk["section"], "chunk_index": i, "source": FILENAME},
                )
        except Exception as e:
            logger.warning("strategic_context: re-index failed: %s", e)
            return False

        _write_state({"hash": h, "chunks": len(chunks)})
        logger.info("strategic_context: re-indexed %d chunks", len(chunks))
        return True


def search_strategic_context(query: str, top_k: int | None = None, min_score: float | None = None) -> list[dict]:
    """Return strategic-context chunks relevant to *query* above the threshold.

    Each result is the raw mem0 result dict (has 'memory', 'score', 'metadata').
    Results below ``min_score`` are filtered out, so an off-topic query returns
    an empty list.
    """
    if not is_enabled() or not query:
        return []

    if top_k is None:
        top_k = get_top_k()
    if min_score is None:
        min_score = get_min_score()

    try:
        from deerflow.agents.memory.mem0_store import STRATEGIC_CONTEXT_USER_ID, search_memories

        results = search_memories(query, top_k=top_k, user_id=STRATEGIC_CONTEXT_USER_ID)
    except Exception as e:
        logger.warning("strategic_context: search failed: %s", e)
        return []

    gated = [r for r in results if float(r.get("score", 0.0) or 0.0) >= min_score]
    if results:
        logger.info(
            "strategic_context: %d/%d chunks cleared threshold %.2f (top score %.3f)",
            len(gated),
            len(results),
            min_score,
            max((float(r.get("score", 0.0) or 0.0) for r in results), default=0.0),
        )
    return gated
