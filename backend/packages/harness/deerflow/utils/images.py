"""Image utilities — currently just downscaling to Anthropic's pixel limit.

Anthropic rejects images where any dimension exceeds 2000px in many-image
requests with a 400 invalid_request_error. Both `view_image_tool` (file reads
inside the agent) and the Slack channel inline-attachment path call
`downscale_for_anthropic` so the image is safe to ship over the API and to
persist in conversation history.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

MAX_DIMENSION = 2000


def downscale_for_anthropic(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """Resize image bytes so the longest side is <= MAX_DIMENSION.

    Returns (possibly-new bytes, possibly-new mime type). If PIL can't open
    the bytes, or both dimensions are already in range, returns the original
    bytes unchanged. Always preserves aspect ratio.

    JPEG output for previously-RGB images; PNG output preserved only when the
    source is PNG with an alpha channel; WEBP preserved when source is WEBP.
    """
    try:
        from PIL import Image
    except ImportError:
        return image_bytes, mime_type

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception:
        return image_bytes, mime_type

    w, h = img.size
    if max(w, h) <= MAX_DIMENSION:
        return image_bytes, mime_type

    scale = MAX_DIMENSION / max(w, h)
    new_size = (int(w * scale), int(h * scale))
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    img = img.resize(new_size, Image.LANCZOS)

    out_fmt = "JPEG"
    out_mime = "image/jpeg"
    if mime_type == "image/png" and img.mode == "RGBA":
        out_fmt = "PNG"
        out_mime = "image/png"
    elif mime_type == "image/webp":
        out_fmt = "WEBP"
        out_mime = "image/webp"

    if out_fmt == "JPEG" and img.mode == "RGBA":
        img = img.convert("RGB")

    buf = io.BytesIO()
    save_kwargs = {"quality": 85} if out_fmt == "JPEG" else {}
    img.save(buf, format=out_fmt, **save_kwargs)
    logger.info(
        "downscale_for_anthropic: %sx%s %s -> %sx%s %s (%d -> %d bytes)",
        w, h, mime_type, new_size[0], new_size[1], out_mime, len(image_bytes), buf.tell(),
    )
    return buf.getvalue(), out_mime
