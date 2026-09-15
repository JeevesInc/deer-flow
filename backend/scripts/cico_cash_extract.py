#!/usr/bin/env python3
"""cico_cash_extract.py
======================
Pull the most recent CICO daily balances email from Gmail (sender: Axel
Orendain), OCR the inlined bank-balance screenshots via Claude Vision,
and write structured cash data to .deer-flow/_cico_state.json.

Categories mirror the HTML wireframe:
  - Operating Accounts
  - Credit (Card & Jeeves Pay)
  - Self-funded Prepaid
  - AP Accounts
  - Restricted Deposits
  - DACA + Pledged Countries (feeds US BB)

Plus a currency rollup for CIM Reporting Cash.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from PIL import Image
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

BACKEND_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BACKEND_DIR / ".deer-flow" / "_cico_state.json"
load_dotenv(BACKEND_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cico")


def _gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _payload_has_image(payload) -> bool:
    if payload.get("mimeType", "").startswith("image/") and payload.get("body", {}).get("attachmentId"):
        return True
    return any(_payload_has_image(p) for p in (payload.get("parts") or []))


def _fetch_latest_cico(svc):
    """Newest CICO email that actually carries balance screenshots.

    Broken until 2026-08-11: the query pinned from:axel@tryjeeves.com and took
    exactly 1 result. The daily balances email moved to Mariana Herrera Cruz
    ("YYMMDD CICO- Daily balances"), so the pinned query started matching an
    unrelated Aug-4 diligence thread ("Schedule 5.22 ... vs CICO") that has no
    images -> "No images to OCR; aborting" -> state frozen on 2026-07-27 cash
    while the digest kept printing it as current.

    Sender-agnostic now: scan newest-first, take the first message with an
    image attachment (checked from the payload, no attachment download).
    """
    res = svc.users().messages().list(
        userId="me", q="subject:CICO has:attachment", maxResults=25
    ).execute()
    msgs = res.get("messages", [])
    if not msgs:
        raise RuntimeError("No CICO emails found")
    skipped = []
    for m in msgs:
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        hdrs = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
        if _payload_has_image(full["payload"]):
            if skipped:
                log.info("Skipped %d CICO hit(s) with no images: %s",
                         len(skipped), "; ".join(skipped[:5]))
            return full
        skipped.append(hdrs.get("Subject", "")[:60])
    raise RuntimeError(
        "No CICO email with image attachments in the last %d hits (checked: %s)"
        % (len(msgs), "; ".join(skipped[:5]))
    )


def _collect_image_attachments(payload, out: list, svc, msg_id: str):
    if payload.get("mimeType", "").startswith("image/"):
        body = payload.get("body", {})
        att_id = body.get("attachmentId")
        if att_id:
            att = svc.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=att_id
            ).execute()
            data = att.get("data", "")
            if data:
                out.append(base64.urlsafe_b64decode(data))
    for part in payload.get("parts", []) or []:
        _collect_image_attachments(part, out, svc, msg_id)



# ── Image conditioning ───────────────────────────────────────────────────────
# The CICO screenshots arrive tiny (~456x950) with ~7px digit height. At that
# size Claude reliably misreads single digits in the 8-figure totals — on
# 2026-08-17 it read Total cash as 91,804,860 and 91,604,860 on two runs when
# the true value was 91,604,850, and the reconciliation gate (correctly) threw
# both away. Magnification is the fix: upscaling the same crop to ~2500px on
# the long edge reads every figure exactly right, repeatably.
_OCR_TARGET_LONG_EDGE = 2500
# Band geometry for the totals pass. A band must be tall enough to hold
# "Total cash" through "Total cash + restricted deposits" together, otherwise
# no single band can satisfy the reconciliation check.
_BAND_HEIGHT = 260
_BAND_STRIDE = 120


def _upscale(im, target: int = _OCR_TARGET_LONG_EDGE):
    """Enlarge so the long edge is ~target px. Never downscales."""
    factor = target / max(im.size)
    if factor <= 1.0:
        return im
    return im.resize((round(im.width * factor), round(im.height * factor)), Image.LANCZOS)


def _to_png_b64(im) -> str:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _is_blank(im, threshold: float = 0.01) -> bool:
    """True when a band is essentially white — skips empty spacer rows so we
    don't spend an API call on them."""
    mono = im.convert("L").point(lambda p: 255 if p > 240 else 0)
    dark = mono.histogram()[0]
    return (dark / float(im.width * im.height)) <= threshold


def _summary_bands(img_bytes: bytes):
    """Yield overlapping, upscaled horizontal bands of one screenshot.

    Banding (rather than upscaling the whole page) is what buys the
    magnification: a full 456x950 page can only be enlarged ~2.6x before it
    hits the long-edge budget, which still misreads digits. A 456x260 band
    gets ~5.5x, which reads clean.
    """
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    if im.height <= _BAND_HEIGHT:
        yield _upscale(im)
        return
    y = 0
    while True:
        band = im.crop((0, y, im.width, min(y + _BAND_HEIGHT, im.height)))
        if not _is_blank(band):
            yield _upscale(band)
        if y + _BAND_HEIGHT >= im.height:
            break
        y += _BAND_STRIDE


# ── Second-pass prompt: extract named summary totals ─────────────────────────
_TOTALS_PROMPT = (
    "This is a screenshot from a Jeeves daily CICO cash report. Some images show "
    "named summary lines like DACA + pledged countries, Total cash, "
    "Total restricted deposits accounts balance, etc.\n\n"
    "Extract ONLY these named summary totals. Return a single JSON object:\n"
    "{\n"
    "  daca_pledged_usd: <number or null>,\n"
    "  total_cash_usd: <number or null>,\n"
    "  total_restricted_usd: <number or null>,\n"
    "  total_cash_plus_restricted_usd: <number or null>\n"
    "}\n\n"
    "Rules:\n"
    "- Match the label EXACTLY. If the exact label is not in this image, return null.\n"
    "- 'DACA + pledged countries' -> daca_pledged_usd.\n"
    "- 'Total cash' (the standalone grand total line, immediately above the\n"
    "  'DACA + pledged countries' line) -> total_cash_usd.\n"
    "- 'Total restricted deposits accounts balance' -> total_restricted_usd.\n"
    "- 'Total cash + restricted deposits' -> total_cash_plus_restricted_usd.\n"
    "- NEVER use a section subtotal for these fields. In particular these are\n"
    "  NOT the grand totals and must be ignored: 'Total operating accounts\n"
    "  balance', 'Total credit - Card and Jeeves Pay accounts balance', 'Total\n"
    "  prepaid - Card, Jeeves Pay, and stablecoin accounts balance', 'Total AP\n"
    "  accounts balance', 'Demo/Testing accounts balance', 'Total not used bank\n"
    "  accounts', 'Total self funded balances', 'Total client collateral\n"
    "  balances', 'Total client balances'. Reading one of those as total_cash\n"
    "  is the exact failure this pass exists to prevent.\n"
    "- Output valid JSON with double-quoted keys, plain numbers, no commas in numbers.\n"
    "- If a field is not visible in this image, return null for that field.\n"
    "- No prose, no markdown fences, just the raw JSON object."
)


def _extract_summary_totals(client, images: list) -> dict:
    """Second pass: scan all images for named summary lines.

    The CICO email shows 'DACA + pledged countries' as a pre-computed subtotal
    (e.g. $21.78M on 2026-06-08) that cannot be reconstructed from individual
    account rows — Claude OCR classifies them as 'operating', not 'pledged'.
    This pass reads the summary line directly from the image.
    """
    import re as _re
    merged: dict = {}
    candidates: list = []
    for i, img_bytes in enumerate(images):
        for band_idx, band in enumerate(_summary_bands(img_bytes)):
            resp = client.messages.create(
                # Sonnet, not Haiku: the totals block is a dense multi-column table and
                # Haiku misread "98,264,115" as "96,264,115" and mapped the
                # cash+restricted line onto total_restricted (2026-08-11). These four
                # numbers are the ones that reach the digest, so pay for the better read.
                model="claude-sonnet-4-5-20250929",
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": _to_png_b64(band)},
                        },
                        {"type": "text", "text": _TOTALS_PROMPT},
                    ],
                }],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            text = _re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=_re.MULTILINE).strip()
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                continue
            cand = {k: float(v) for k, v in data.items()
                    if k in ("daca_pledged_usd", "total_cash_usd", "total_restricted_usd",
                             "total_cash_plus_restricted_usd")
                    and isinstance(v, (int, float))}
            if cand:
                log.info("  Summary pass img %d band %d: %s", i + 1, band_idx,
                         {k: round(v) for k, v in cand.items()})
                candidates.append(cand)

    # Prefer a single image that carries the grand totals AND reconciles:
    #   Total cash + Total restricted == Total cash + restricted deposits.
    # 2026-08-11: without this, image 1's section subtotals ('Total operating'
    # 53,041,173 and 'Total credit' 38,920,674) were merged in as total cash and
    # restricted, while the real grand totals (98,264,115 / 11,076,642 /
    # 109,340,757, DACA 75,470,863) sat on image 3.
    for cand in candidates:
        c, r, cr = (cand.get("total_cash_usd"), cand.get("total_restricted_usd"),
                    cand.get("total_cash_plus_restricted_usd"))
        if None in (c, r, cr):
            continue
        # Tolerance, not exact equality: the printed grand totals are rounded
        # dollars and OCR can land $1-2 off (2026-09-09: 64,558,514 + 11,177,635
        # vs a printed 75,736,147 -> $2 delta vetoed the write for 9 days).
        # $100 / 5bps still catches the real failure mode this guard exists for
        # (section subtotals merged as grand totals, off by tens of millions).
        if abs((c + r) - cr) <= max(100.0, abs(cr) * 0.0005):
            cand["_verified"] = True
            log.info("Summary totals reconcile on one image: cash %.0f + restricted "
                     "%.0f = %.0f", c, r, cr)
            return cand

    # No single self-consistent image. Merge field-wise and re-test: the grand
    # totals legitimately straddle two images (2026-09-09: total cash on image 2,
    # restricted + cash-and-restricted on image 3), so the single-image rule
    # rejected a perfectly good read and froze the dashboard for 9 days. The
    # merged set still has to reconcile, which is what actually guards against
    # section subtotals being mistaken for grand totals.
    for cand in candidates:
        for k, v in cand.items():
            merged.setdefault(k, v)
    c, r, cr = (merged.get("total_cash_usd"), merged.get("total_restricted_usd"),
                merged.get("total_cash_plus_restricted_usd"))
    if None not in (c, r, cr) and abs((c + r) - cr) <= max(100.0, abs(cr) * 0.0005):
        merged["_verified"] = True
        log.info("Summary totals reconcile across images: cash %.0f + restricted "
                 "%.0f = %.0f (printed %.0f)", c, r, c + r, cr)
        return merged
    # Last resort: components corroborated, combined line misread.
    # 2026-09-14: cash 78,497,060 came back identically on 4 separate bands and
    # restricted 15,174,020 on one, but the printed "Total cash + restricted"
    # line 93,671,081 was OCR'd as 83,671,081 (one digit), so the merged test
    # failed and the digest served 5-day-old cash. The combined line is a
    # derived figure; when the cash grand total is independently corroborated on
    # >=2 bands (the CIM-reporting block and the summary-by-country block both
    # print it), the components are the trustworthy read — accept and recompute.
    if None not in (c, r):
        cash_votes = sum(1 for cand in candidates if cand.get("total_cash_usd") == c)
        if cash_votes >= 2:
            merged["total_cash_plus_restricted_usd"] = c + r
            merged["_verified"] = True
            merged["_repaired"] = True
            log.warning(
                "Combined-line mismatch (printed %s vs computed %.0f); cash %.0f "
                "corroborated on %d bands - accepting components, recomputing total.",
                "n/a" if cr is None else format(cr, ".0f"), c + r, c, cash_votes)
            return merged

    merged["_verified"] = False
    log.error("Summary totals did NOT reconcile on any image or merged: %s",
              {k: round(v) for k, v in merged.items() if isinstance(v, float)})
    return merged


_OCR_PROMPT = """\
This is a screenshot of Jeeves bank-account balances from a daily CICO cash \
report. Extract each row as JSON and classify into categories. Return STRICTLY \
a single JSON object with this schema:

{
  "rows": [
    {"bank": "<name>", "account": "<last 4 or label>", "currency": "USD|MXN|BRL|COP|...", "balance_native": <number>, "balance_usd": <number or null>, "category": "operating|credit|prepaid|ap|restricted|pledged|cim_reporting"}
  ]
}

Category rules:
- "operating": BofA, Citibank, JPM, BBVA, BTG, Monex, STP, general operating accounts
- "credit": Card & Jeeves Pay funding (Routefusion, Banco de Bogotá, Cobre, Davivienda)
- "prepaid": Self-funded prepaid (Bridge, Airwallex, Marqeta, Stripe, Dlocal)
- "ap": AP accounts (Mercury, Jeeves internal)
- "restricted": PNC restricted, BTG restricted, Airwallex restricted (anything labelled "restricted")
- "pledged": Accounts marked DACA, pledged, or that feed US BB
- "cim_reporting": Currency breakdowns specifically labelled CIM Reporting

If you can't classify confidently, default to "operating". Only include rows \
with numeric balances. No prose, no markdown, just the JSON object.\
"""


_CATEGORIES = ["operating", "credit", "prepaid", "ap", "restricted", "pledged", "cim_reporting"]

# Structured-output schema for the row pass. Guarantees the response is a
# single valid JSON object, which removes two whole failure classes seen on
# 2026-08-17: unescaped quotes inside bank names ("Expecting ',' delimiter")
# and stray markdown fences.
_ROWS_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "bank": {"type": "string"},
                    "account": {"type": "string"},
                    "currency": {"type": "string"},
                    "balance_native": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "balance_usd": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "category": {"type": "string", "enum": _CATEGORIES},
                },
                "required": ["bank", "account", "currency", "balance_native", "balance_usd", "category"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rows"],
    "additionalProperties": False,
}


def _ocr_image(client: Anthropic, img_bytes: bytes) -> list[dict]:
    # Same magnification rationale as the totals pass — at native size the
    # per-account balances misread too.
    b64 = _to_png_b64(_upscale(Image.open(io.BytesIO(img_bytes))))
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        # 2026-08-17: this was 2000, which truncated the row JSON mid-string on
        # every single image (~5.7-5.9k chars of output) — the parse then failed
        # and the pass silently returned zero rows. A full CICO page of accounts
        # needs roughly 4x that.
        max_tokens=8000,
        output_config={"format": {"type": "json_schema", "schema": _ROWS_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": _OCR_PROMPT},
                ],
            }
        ],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Strip code fences if present
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    if resp.stop_reason == "max_tokens":
        # Never silently drop a page: a truncated read is a data-loss bug, and
        # downstream reconciliation can't tell "no accounts" from "cut off".
        log.error("OCR hit max_tokens (%d chars returned) — rows for this image are incomplete", len(text))
        return []
    try:
        data = json.loads(text)
        return data.get("rows", [])
    except json.JSONDecodeError as e:
        log.warning("OCR JSON parse failed: %s — first 200 chars: %r", e, text[:200])
        return []


def _alert_extract_failure(subject: str, date: str, summary: dict) -> None:
    """A refused write must be loud. Silent refusal let CICO cash go 9 days
    stale (2026-08-31 -> 2026-09-09) with nothing but a log line."""
    try:
        nums = {k: round(v) for k, v in summary.items() if isinstance(v, float)}
        msg = (
            ":rotating_light: CICO extract blocked - dashboard cash will be stale.\n"
            f"Email: {subject} ({date})\n"
            f"OCR read: {nums}\n"
            "Totals failed the cash + restricted reconciliation; previous state kept."
        )
        tool = Path(__file__).resolve().parents[1] / "skills" / "custom" / "slack-search" / "slack_tool.py"
        if not tool.exists():
            tool = Path(__file__).resolve().parents[2] / "skills" / "custom" / "slack-search" / "slack_tool.py"
        subprocess.run([sys.executable, str(tool), "send", "U05B5HGNCN9", msg],
                       timeout=60, capture_output=True)
    except Exception as e:  # never let the alerter mask the original failure
        log.warning("Could not send CICO failure alert: %s", e)


def main() -> int:
    svc = _gmail_service()
    msg = _fetch_latest_cico(svc)
    hdrs = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    subject = hdrs.get("Subject", "")
    date = hdrs.get("Date", "")
    log.info("Latest CICO email: %s (%s)", subject, date)

    images: list[bytes] = []
    _collect_image_attachments(msg["payload"], images, svc, msg["id"])
    log.info("Found %d image attachments", len(images))
    if not images:
        log.warning("No images to OCR; aborting")
        return 1

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    all_rows: list[dict] = []
    for i, img in enumerate(images):
        log.info("OCR image %d/%d (%d bytes)", i + 1, len(images), len(img))
        rows = _ocr_image(client, img)
        log.info("  → %d rows extracted", len(rows))
        all_rows.extend(rows)

    # Approximate FX (USD per unit) for balances without an explicit USD
    # equivalent — overridden from cap_markets_config.json (local-per-USD,
    # hot-reloads); baked table below is the fallback. Acceptable for an
    # at-a-glance dashboard.
    _FX = {"USD": 1.0, "MXN": 1.0 / 17.35, "BRL": 1.0 / 5.5, "COP": 1.0 / 4100.0,
           "EUR": 1.08, "GBP": 1.27, "CAD": 1.0 / 1.37, "ARS": 1.0 / 1000.0,
           "CLP": 1.0 / 950.0, "PEN": 1.0 / 3.75}
    try:
        _shared = Path(__file__).resolve().parents[2] / "skills" / "custom" / "_shared"
        if str(_shared) not in sys.path:
            sys.path.insert(0, str(_shared))
        from cap_markets_config import get_cm_config
        for _ccy, _local_per_usd in (get_cm_config().get("fx_fallback") or {}).items():
            if isinstance(_local_per_usd, (int, float)) and _local_per_usd > 0:
                _FX[_ccy.upper()] = 1.0 / float(_local_per_usd)
    except Exception as _e:
        log.warning("cap_markets_config unavailable — using baked FX table: %s", _e)

    # Aggregate by category
    by_cat: dict[str, dict] = {}
    for row in all_rows:
        cat = row.get("category", "operating")
        bal_usd = row.get("balance_usd")
        if not isinstance(bal_usd, (int, float)):
            # Convert from native if possible
            native = row.get("balance_native")
            ccy = row.get("currency", "USD")
            if isinstance(native, (int, float)) and ccy in _FX:
                bal_usd = round(native * _FX[ccy], 2)
                row["balance_usd"] = bal_usd
                row["balance_usd_estimated"] = True
            else:
                continue
        entry = by_cat.setdefault(cat, {"balance_usd": 0.0, "rows": []})
        entry["balance_usd"] += bal_usd
        entry["rows"].append(row)
    # Round
    for c, e in by_cat.items():
        e["balance_usd"] = round(e["balance_usd"], 2)

    total_cash = sum(by_cat[c]["balance_usd"] for c in by_cat if c != "restricted")
    restricted = by_cat.get("restricted", {}).get("balance_usd", 0.0)
    pledged_daca = by_cat.get("pledged", {}).get("balance_usd", 0.0)

    # ── Summary-totals override ────────────────────────────────────────────────
    # "DACA + pledged countries" is a pre-computed subtotal in the email image.
    # Individual rows don't carry a "pledged" tag so the row-sum is always 0.
    # A second Claude pass reads the summary line directly from the images.
    log.info("Running summary-totals extraction pass...")
    summary = _extract_summary_totals(client, images)
    if summary.get("_verified") is not True:
        log.error("Refusing to write _cico_state.json: summary totals failed the "
                  "cash + restricted = cash+restricted check. Keeping previous state "
                  "(email: %s / %s).", subject, date)
        _alert_extract_failure(subject, date, summary)
        return 2
    if summary.get("daca_pledged_usd") is not None:
        pledged_daca = summary["daca_pledged_usd"]
        log.info("DACA/pledged from summary pass: $%.0f", pledged_daca)
    if summary.get("total_cash_usd") is not None:
        total_cash = summary["total_cash_usd"]
        log.info("Total cash from summary pass: $%.0f", total_cash)
    if summary.get("total_restricted_usd") is not None:
        restricted = summary["total_restricted_usd"]
        log.info("Restricted from summary pass: $%.0f", restricted)

    state = {
        "source_subject": subject,
        "source_date": date,
        "extracted_at": datetime.now().isoformat(),
        "by_category": {c: by_cat[c]["balance_usd"] for c in by_cat},
        "rows": all_rows,
        "total_cash_usd": round(total_cash, 2),
        "restricted_deposits_usd": round(restricted, 2),
        "daca_pledged_usd": round(pledged_daca, 2),
        "total_cash_plus_restricted_usd": round(total_cash + restricted, 2),
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s", STATE_FILE)
    log.info("Total cash: $%.2f, Restricted: $%.2f, DACA/Pledged: $%.2f",
             total_cash, restricted, pledged_daca)
    return 0


def _state_age_seconds(path, ts_field):
    """Seconds since path's ts_field timestamp, or None if missing/unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (datetime.now() - datetime.fromisoformat(data[ts_field])).total_seconds()
    except Exception:
        return None


def run_loop():
    """Cron entry — refresh daily after Axel typically sends (22:30 local).
    Brian's gateway cron polls every 30 min."""
    import time
    interval = int(os.environ.get("CICO_REFRESH_SECONDS", "1800"))
    log.info("cico-cash cron starting (interval=%ds)", interval)
    while True:
        # Skip-if-fresh: a gateway restart re-instantiates this cron, which would
        # otherwise immediately re-OCR the CICO email (~14 Anthropic Vision
        # calls). That burst starves the gateway's async /health probe →
        # supervisor false-kills the gateway → restart → fresh burst (the
        # 2026-06-16 flap). Reuse still-fresh state across restarts.
        age = _state_age_seconds(STATE_FILE, "extracted_at")
        if age is not None and age < interval:
            wait = interval - age
            log.info("CICO state fresh (%.0fs old < %ds) — skipping run, sleeping %.0fs", age, interval, wait)
            time.sleep(wait)
            continue
        try:
            main()
        except Exception as e:
            log.exception("cico iteration failed: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
