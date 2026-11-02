"""One-off: walk a thread's checkpoints, find image_url content blocks
whose base64 data exceeds 2000px on any side, resize in place to <=2000px,
and write the checkpoint back.

The Anthropic API rejects many-image requests where any image dimension
exceeds 2000px. Once an oversized image lands in conversation history, every
subsequent turn fails with the same 400. This script repairs the history
so the thread can keep going.

Usage:
    python scripts/_fix_oversize_images_in_thread.py <thread_id>
"""

import base64
import io
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from PIL import Image
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from deerflow.utils.images import downscale_for_anthropic, MAX_DIMENSION

DB = Path(".deer-flow/checkpoints.db")


def _fix_image_url_block(block: dict) -> bool:
    """Resize in place if oversized. Return True if changed."""
    url = block.get("image_url", {}).get("url", "") if isinstance(block.get("image_url"), dict) else ""
    if not url.startswith("data:"):
        return False
    try:
        header, b64 = url.split(",", 1)
        mime = header.split(";")[0].split(":", 1)[1]
    except (ValueError, IndexError):
        return False

    try:
        raw = base64.b64decode(b64)
    except Exception:
        return False

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return False

    if max(img.size) <= MAX_DIMENSION:
        return False

    new_bytes, new_mime = downscale_for_anthropic(raw, mime)
    new_b64 = base64.b64encode(new_bytes).decode("ascii")
    block["image_url"]["url"] = f"data:{new_mime};base64,{new_b64}"
    return True


def _walk_and_fix(messages: list) -> int:
    """Walk a list of LangChain messages, fix oversized images. Returns #fixed."""
    fixed = 0
    for m in messages:
        content = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "image_url":
                continue
            if _fix_image_url_block(block):
                fixed += 1
    return fixed


def _walk_viewed_images(viewed: dict | None) -> int:
    fixed = 0
    if not isinstance(viewed, dict):
        return 0
    for path, data in viewed.items():
        if not isinstance(data, dict):
            continue
        b64 = data.get("base64", "")
        mime = data.get("mime_type", "")
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64)
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception:
            continue
        if max(img.size) <= MAX_DIMENSION:
            continue
        new_bytes, new_mime = downscale_for_anthropic(raw, mime)
        data["base64"] = base64.b64encode(new_bytes).decode("ascii")
        data["mime_type"] = new_mime
        fixed += 1
    return fixed


def main():
    if len(sys.argv) < 2:
        print("usage: python scripts/_fix_oversize_images_in_thread.py <thread_id>")
        sys.exit(2)
    thread_id = sys.argv[1]

    backup = DB.with_suffix(f".db.bak_{int(time.time())}")
    shutil.copy(DB, backup)
    print(f"Backup -> {backup}")

    serde = JsonPlusSerializer()
    con = sqlite3.connect(str(DB), timeout=30.0)
    con.execute("PRAGMA busy_timeout = 30000")
    rows = con.execute(
        "SELECT thread_id, checkpoint_ns, checkpoint_id, type, checkpoint FROM checkpoints WHERE thread_id = ?",
        (thread_id,),
    ).fetchall()
    print(f"Found {len(rows)} checkpoint rows for thread {thread_id}")

    total_fixed_msgs = 0
    total_fixed_viewed = 0
    rows_updated = 0
    for tid, ns, cid, ctype, blob in rows:
        try:
            ckpt = serde.loads_typed((ctype, blob))
        except Exception as e:
            print(f"  skip cid={cid}: decode failed: {e}")
            continue
        ch = ckpt.get("channel_values", {}) or {}
        msgs = ch.get("messages", []) or []
        viewed = ch.get("viewed_images", {}) or {}
        nm = _walk_and_fix(msgs)
        nv = _walk_viewed_images(viewed)
        if nm or nv:
            new_type, new_blob = serde.dumps_typed(ckpt)
            con.execute(
                "UPDATE checkpoints SET type = ?, checkpoint = ? "
                "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                (new_type, new_blob, tid, ns, cid),
            )
            total_fixed_msgs += nm
            total_fixed_viewed += nv
            rows_updated += 1

    con.commit()
    con.close()
    print(f"Updated {rows_updated} checkpoints; fixed {total_fixed_msgs} message-image blocks, {total_fixed_viewed} viewed_images entries")


if __name__ == "__main__":
    main()
