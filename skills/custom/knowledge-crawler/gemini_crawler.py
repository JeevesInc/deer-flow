"""Gemini meeting-notes source for the knowledge crawler (added 2026-10-07).

Why: the crawler read only email/Drive/Slack, and Honcho only sees <=5 excerpts
from the 20 newest notes, so "how do people see Brian" reviews had no meeting
signal. This module:
  1. pulls every "Notes by Gemini" doc (incremental, paginated, all drives),
  2. extracts per-note evidence rows about how colleagues work with Brian
     (praise / pushback / delegation / reliance / style_feedback / slip / behavior),
  3. writes wiki/people/<first_last>.md (deterministic, dated quotes, no LLM),
  4. rebuilds wiki/people/brian_mauck_360.md weekly via an LLM reduce, then
     DROPS any quoted string that cannot be found verbatim in a source note.

CLI:  python gemini_crawler.py [--days 120] [--force-review]
"""
import json, logging, os, re, sys, concurrent.futures as cf
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "_shared"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

log = logging.getLogger("knowledge-crawler.gemini")
DEER_FLOW_DIR = Path(os.environ.get("DEER_FLOW_DATA_DIR",
                     r"C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow"))
GEM_DIR = DEER_FLOW_DIR / "knowledge" / "gemini"
RAW_DIR, MAP_DIR = GEM_DIR / "raw", GEM_DIR / "map"
PEOPLE_DIR = DEER_FLOW_DIR / "wiki" / "people"
STATE = GEM_DIR / "_state.json"
MAP_MODEL = os.environ.get("GEMINI_MAP_MODEL", "claude-sonnet-5-5")
REDUCE_MODEL = os.environ.get("GEMINI_REDUCE_MODEL", "claude-opus-5-5")
REVIEW_EVERY_DAYS = 7
TYPES = ["praise", "pushback", "delegation", "reliance", "style_feedback", "slip", "behavior"]

MAP_PROMPT = """You are analyzing a Google Meet "Notes by Gemini" document (summary and possibly transcript) from Jeeves, a fintech. The subject is Brian Mauck, Head of Capital Markets.
Extract ONLY evidence about how OTHER Jeeves people perceive, rely on, react to, or work with Brian. Include:
- praise, thanks, credit given to Brian
- pushback, disagreement, frustration, criticism, corrections aimed at Brian or his work
- things others delegate to him / defer to him on / ask him to own (trust or bottleneck)
- feedback (explicit or implied) about his style: communication, follow-through, speed, rigor, tone
- moments where Brian's commitments slipped or were chased
- how Brian behaves when it reveals how he's seen
Quotes must be EXACT substrings of the document. Attribute each to the person who actually said it.
Return strict JSON: {"meeting": str, "date": "YYYY-MM-DD", "internal_attendees": [names], "signals": [{"person": str, "type": "praise|pushback|delegation|reliance|style_feedback|slip|behavior|other", "quote": str, "note": str}]}. Empty signals list if none. No inference beyond the text."""

REDUCE_PROMPT = """Below are evidence rows extracted from Gemini meeting notes about how Jeeves colleagues perceive and work with Brian Mauck (Head of Capital Markets). Columns: date | meeting | person | type | quote | note.
Write a candid, holistic 360-style review addressed to Brian as "you". Every claim must cite at least one dated quote copied EXACTLY from the quote column, format: "quote" (Person, Mon DD). Attribute quotes only to the person in that row. No invented facts, no flattery; weight criticism and slips equally with praise. Distinguish what people SAY from what their behavior implies. Spell the lender "Neuberger Berman" (transcripts say "Newberger").
Structure (markdown, bold headers, bullets, max ~550 words): Bottom line; How key people see you (~7 internal people, Alex first); What you're known for (3); Where the friction is (3-4); How it's trended; One thing to change.
Rows:
"""


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower())).strip()


def _load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(s):
    STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")


def _drive():
    from google_auth import get_credentials
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=get_credentials(), cache_discovery=False)


def _client():
    import anthropic
    return anthropic.Anthropic()


def pull_notes(days: int) -> list:
    svc = _drive()
    after = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    q = (f"name contains 'Notes by Gemini' and modifiedTime > '{after}' and "
         "mimeType = 'application/vnd.google-apps.document' and trashed = false")
    files, tok = [], None
    while True:
        r = svc.files().list(q=q, fields="nextPageToken,files(id,name,createdTime,modifiedTime)",
                             orderBy="createdTime desc", pageSize=200, pageToken=tok,
                             supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files += r.get("files", [])
        tok = r.get("nextPageToken")
        if not tok:
            break
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    idx = {}
    for f in files:
        p = RAW_DIR / f"{f['id']}.txt"
        meta = RAW_DIR / f"{f['id']}.meta"
        stale = (not p.exists()) or (meta.exists() and meta.read_text() != f["modifiedTime"])
        if stale:
            try:
                c = svc.files().export(fileId=f["id"], mimeType="text/plain").execute()
                p.write_text(c.decode("utf-8", "ignore") if isinstance(c, bytes) else c, encoding="utf-8")
                meta.write_text(f["modifiedTime"])
                (MAP_DIR / f"{f['id']}.json").unlink(missing_ok=True)
            except Exception as e:
                log.warning(f"gemini export failed {f['name'][:60]}: {e}")
                continue
        idx[f["id"]] = f
    (GEM_DIR / "_index.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return list(idx.values())


def _map_one(client, f):
    op = MAP_DIR / f"{f['id']}.json"
    if op.exists():
        return 0
    text = (RAW_DIR / f"{f['id']}.txt").read_text(encoding="utf-8")[:150000]
    err = ""
    for _ in range(3):
        try:
            r = client.messages.create(model=MAP_MODEL, max_tokens=12000, messages=[
                {"role": "user", "content": MAP_PROMPT + f"\n\nTITLE: {f['name']}\n\n" + text}])
            txt = "".join(getattr(b, "text", "") for b in r.content)
            j = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
            src = _norm(text)
            # keep only quotes that are really in the note
            j["signals"] = [s for s in j.get("signals", []) if _norm(s.get("quote", ""))[:120] in src]
            j.update(title=f["name"], id=f["id"], created=f["createdTime"][:10])
            op.write_text(json.dumps(j, indent=1, ensure_ascii=False), encoding="utf-8")
            return len(j["signals"])
        except Exception as e:
            err = str(e)[:150]
    log.warning(f"gemini map failed {f['name'][:60]}: {err}")
    return 0


def map_notes(files: list) -> int:
    MAP_DIR.mkdir(parents=True, exist_ok=True)
    todo = [f for f in files if not (MAP_DIR / f"{f['id']}.json").exists()]
    if not todo:
        return 0
    client = _client()
    with cf.ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda f: _map_one(client, f), todo))
    return len(todo)


def load_rows() -> list:
    rows = []
    for p in MAP_DIR.glob("*.json"):
        j = json.loads(p.read_text(encoding="utf-8"))
        d = (j.get("created") or j.get("date") or "")[:10]
        mtg = re.sub(r" - 20\d\d/.*", "", j.get("title", ""))
        raw = RAW_DIR / f"{p.stem}.txt"
        src = _norm(raw.read_text(encoding="utf-8")) if raw.exists() else ""
        for s in j.get("signals", []):
            if src and _norm(s.get("quote", ""))[:120] not in src:
                continue  # quote not verbatim in the note -> drop
            rows.append({"date": d, "mtg": mtg, **s})
    return sorted(rows, key=lambda r: r["date"])


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unknown"


def write_people_pages(rows: list) -> int:
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    by = defaultdict(list)
    for r in rows:
        nm = (r.get("person") or "").split("(")[0].strip()
        if nm and "brian" not in nm.lower():
            by[nm].append(r)
    n = 0
    for nm, rs in by.items():
        if len(rs) < 3:
            continue
        c = Counter(r["type"] for r in rs)
        lines = [f"# {nm} - how they work with Brian (from Gemini notes)",
                 f"_Auto-generated {datetime.now():%Y-%m-%d} by gemini_crawler. {len(rs)} signals, "
                 f"{rs[0]['date']} to {rs[-1]['date']}. "
                 + ", ".join(f"{t}={c[t]}" for t in TYPES if c[t]) + "_", ""]
        for t in TYPES + ["other"]:
            sub = [r for r in rs if r["type"] == t]
            if not sub:
                continue
            lines.append(f"## {t}")
            for r in sub[-15:]:
                lines.append(f"- {r['date']} | {r['mtg'][:60]} | \"{r.get('quote','')[:300]}\"")
            lines.append("")
        (PEOPLE_DIR / f"{_slug(nm)}.md").write_text("\n".join(lines), encoding="utf-8")
        n += 1
    return n


def write_review(rows: list) -> Path:
    body = "\n".join(f"{r['date']} | {r['mtg'][:50]} | {r.get('person')} | {r['type']} | "
                     f"{r.get('quote','')[:300]} | {r.get('note','')[:150]}" for r in rows)
    r = _client().messages.create(model=REDUCE_MODEL, max_tokens=16000,
                                  messages=[{"role": "user", "content": REDUCE_PROMPT + body}])
    out = "".join(getattr(b, "text", "") for b in r.content)
    # verification: strike any quoted string not found verbatim in a source note
    corpus = [_norm(p.read_text(encoding="utf-8")) for p in RAW_DIR.glob("*.txt")]
    dropped = []
    for q in set(re.findall(r'"([^"]{8,})"', out)):
        parts = [p for p in (_norm(x) for x in q.split("...")) if len(p) > 6]
        if parts and not any(all(p in t for p in parts) for t in corpus):
            dropped.append(q)
            out = out.replace(f'"{q}"', "[quote removed - not found in source]")
    hdr = (f"# Brian Mauck - 360 from meeting notes\n_Auto-generated {datetime.now():%Y-%m-%d %H:%M} "
           f"from {len({x['mtg'] + x['date'] for x in rows})} meetings, {len(rows)} signals "
           f"({rows[0]['date']} to {rows[-1]['date']}). Quotes verified against source; "
           f"{len(dropped)} unverifiable quote(s) removed._\n\n")
    p = PEOPLE_DIR / "brian_mauck_360.md"
    p.write_text(hdr + out, encoding="utf-8")
    return p


def crawl_gemini(state: dict = None, force: bool = False, days: int = 120, force_review: bool = False) -> int:
    GEM_DIR.mkdir(parents=True, exist_ok=True)
    files = pull_notes(days)
    mapped = map_notes(files)
    rows = load_rows()
    pages = write_people_pages(rows)
    st = _load_state()
    last = st.get("last_review")
    due = force_review or not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days >= REVIEW_EVERY_DAYS
    if rows and (due or mapped >= 10):
        write_review(rows)
        st["last_review"] = datetime.now(timezone.utc).isoformat()
    st.update(last_crawl=datetime.now(timezone.utc).isoformat(), notes=len(files), signals=len(rows), people_pages=pages)
    _save_state(st)
    log.info(f"gemini: {len(files)} notes, {mapped} newly mapped, {len(rows)} signals, {pages} people pages")
    return mapped


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    try:
        from env_loader import load_env
        load_env()
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--force-review", action="store_true")
    a = ap.parse_args()
    print(crawl_gemini(days=a.days, force_review=a.force_review))
