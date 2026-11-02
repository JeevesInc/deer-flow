"""Shared TypeSafe Jev (System One) client — added 2026-09-23.

Jev returns typed decisions (noul = P(yes), choice, score) with calibrated
probabilities; it never generates text. Endpoint: POST /v1/systemone.
Pinned to jev-1.13.0 (NOT jev-latest) because thresholds are tuned
against that version — move deliberately, re-running jeeves-eval.

Key: TYPESAFE_API_KEY from env (backend/.env), falling back to the file
Brian dropped at C:\\Jeeves\\redshift-bot\\tmp\\typesafe.txt.txt.

Every call appends one line to .deer-flow/jev_usage.jsonl (caller, tokens,
latency, ok) so cost and failure rate are observable.

Callers MUST treat JevError as "fall back to the previous path" — Jev is
an accelerator, never a single point of failure.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")
URL = "https://api.typesafe.ai/v1/systemone"
_KEY_FILE = Path(r"C:\Jeeves\redshift-bot\tmp\typesafe.txt.txt")
_ENV_FILE = Path(r"C:\Jeeves\redshift-bot\deer-flow\backend\.env")
_USAGE = Path(r"C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow\jev_usage.jsonl")


class JevError(RuntimeError):
    pass


def _key() -> str:
    k = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not k and _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("TYPESAFE_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not k and _KEY_FILE.exists():
        k = _KEY_FILE.read_text(encoding="utf-8").strip()
    if not k:
        raise JevError("TYPESAFE_API_KEY not set")
    return k


def _log(rec: dict) -> None:
    try:
        rec["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(_USAGE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def ask(state, questions: dict, caller: str = "unknown", timeout: float = 15.0, retries: int = 2) -> dict:
    """Return the `answers` dict. Raises JevError on any failure."""
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    last = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            req = urllib.request.Request(URL, data=body, headers={
                "Authorization": "Bearer " + _key(), "Content-Type": "application/json"})
            data = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
            u = data.get("usage", {})
            _log({"caller": caller, "ok": True, "ms": int((time.time() - t0) * 1000),
                  "in": u.get("input_tokens"), "model": data.get("model"), "q": len(questions)})
            return data["answers"]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}"
            if e.code == 429 and attempt < retries:
                time.sleep(float(e.headers.get("retry-after") or 2 ** attempt))
                continue
            if e.code < 500:
                break
        except Exception as e:  # network / timeout / parse
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    _log({"caller": caller, "ok": False, "err": last, "q": len(questions)})
    raise JevError(last)


def noul(state, instructions: str, caller: str = "unknown", **kw) -> float:
    return float(ask(state, {"q": {"type": "noul", "instructions": instructions}}, caller, **kw)["q"]["noul"])


def choice(state, instructions: str, criteria: dict, caller: str = "unknown", **kw) -> dict:
    """Returns {'choice', 'confidence', 'probabilities'}."""
    return ask(state, {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}}, caller, **kw)["q"]
