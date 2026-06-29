#!/usr/bin/env python3
"""Honcho peer cards tool.

Honcho is the backing store for peer (contact) interaction history. Every
email thread, Slack message, calendar meeting, and Gemini meeting note with a
person is ingested as messages in that peer's Honcho session. The dialectic
`chat()` API then synthesises rich peer cards on demand.

Ingestion ALSO maintains a local co-occurrence graph (`ona_graph.json`) that
the companion `ona_tool.py` reads for org-network-analysis queries and the
Grafana node-graph export. That half works even with no Honcho key configured,
so the ONA/Grafana pipeline is usable before signing up at app.honcho.dev.

Commands:
  read <email>                     Dialectic synthesis: full peer card
  query <email> "<question>"       Ask the dialectic a specific question
  ingest <email> [--days 30]       Pull recent interactions for one peer
  list                             List peers tracked in Honcho
  seed-dossiers                    Seed Honcho from existing dossier JSONs
  seed-all [--days 365]            Full historical ingest for every dossier peer

Env vars:
  HONCHO_API_KEY      Key from app.honcho.dev (omit for local self-host)
  HONCHO_BASE_URL     https://api.honcho.dev (cloud) or http://localhost:8000
  HONCHO_APP_NAME     Honcho workspace id (default: jeeves)
  DOSSIER_PATH        Override for the dossier / ona_graph.json directory
  GOOGLE_CALENDAR_EMAIL  Brian's own address (excluded from peer cards)
  + the Google/Slack creds the jeeves-dossier gather functions need.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _SKILL_DIR.parent / '_shared'
_DOSSIER_SKILL = _SKILL_DIR.parent / 'jeeves-dossier'

sys.path.insert(0, str(_SHARED_DIR))
from env_loader import load_env  # noqa: E402
load_env()

# Reuse the battle-tested gather functions from jeeves-dossier rather than
# duplicating ~200 lines of Google/Slack API plumbing.
sys.path.insert(0, str(_DOSSIER_SKILL))
from dossier_tool import (  # noqa: E402
    gather_calendar,
    gather_gmail,
    gather_slack,
    gather_gemini_notes,
)

MY_EMAIL = os.environ.get('GOOGLE_CALENDAR_EMAIL', 'brian.mauck@tryjeeves.com').strip().lower()
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
_MAX_MSG_LEN = 4000
_HONCHO_BATCH = 100


def _peer_id(email: str) -> str:
    """Encode an email into a valid Honcho id (must match ^[a-zA-Z0-9_-]+$).

    Matches the convention the historical seed already used (single underscore:
    `@`→`_at_`, `.`→`_`), so ongoing ingest appends to the SAME peer rather than
    forking a duplicate. The real email is always stored in peer metadata.
    e.g. adam.chrysostomou@kroll.com -> adam_chrysostomou_at_kroll_com
    """
    return (email.strip().lower()
            .replace('@', '_at_')
            .replace('.', '_')
            .replace('+', '_'))


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

def _dossier_dir() -> Path:
    base = os.environ.get('DOSSIER_PATH', '')
    if not base:
        candidate = _SKILL_DIR.parent.parent.parent / 'backend' / '.deer-flow' / 'dossiers'
        base = str(candidate.resolve())
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ona_graph_path() -> Path:
    return _dossier_dir() / 'ona_graph.json'


def _ingest_state_path() -> Path:
    return _dossier_dir() / '_honcho_ingest_state.json'


def _atomic_json_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tracked_peer_emails() -> list[str]:
    """Emails of every tracked dossier peer (lowercased, deduped, sorted)."""
    out: set[str] = set()
    for f in _dossier_dir().glob('*.json'):
        if f.name.startswith('_') or f.name == 'ona_graph.json':
            continue
        try:
            email = (json.loads(f.read_text(encoding='utf-8')).get('email') or '').strip().lower()
            if '@' in email and email != MY_EMAIL:
                out.add(email)
        except Exception:
            pass
    return sorted(out)


# ---------------------------------------------------------------------------
# Honcho client
# ---------------------------------------------------------------------------

def _honcho_enabled() -> bool:
    """Honcho usable if a cloud key is set or a local/self-host base_url given."""
    if os.environ.get('HONCHO_API_KEY'):
        return True
    base = os.environ.get('HONCHO_BASE_URL', '')
    return 'localhost' in base or '127.0.0.1' in base


def _get_honcho_client():
    """Return a configured Honcho 2.x client (installs honcho-ai if missing)."""
    try:
        from honcho import Honcho
    except ImportError:
        print('[honcho] installing honcho-ai...', file=sys.stderr)
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'honcho-ai'])
        from honcho import Honcho

    workspace = (os.environ.get('HONCHO_APP_NAME')
                 or os.environ.get('HONCHO_WORKSPACE_ID')
                 or 'jeeves')
    kwargs = {'workspace_id': workspace}
    api_key = os.environ.get('HONCHO_API_KEY')
    if api_key:
        kwargs['api_key'] = api_key
    base_url = os.environ.get('HONCHO_BASE_URL')
    if base_url:
        kwargs['base_url'] = base_url
    return Honcho(**kwargs)


def _iter_pages(page):
    """Yield every item across a Honcho paginated response.

    The 2.x Page object is directly iterable; fall back to the manual
    .data()/.nextPage() protocol if a future version stops being iterable.
    """
    if page is None:
        return
    try:
        for item in page:
            yield item
        return
    except TypeError:
        pass
    while page is not None:
        data_attr = getattr(page, 'data', None)
        data = data_attr() if callable(data_attr) else (data_attr or [])
        for item in (data or []):
            yield item
        nxt = getattr(page, 'nextPage', None) or getattr(page, 'next_page', None)
        page = nxt() if callable(nxt) else None


def _session_id(email: str) -> str:
    """Ongoing raw-interaction history session for a peer. Distinct from the
    `dossier_seed_v1_*` baseline-seed sessions; both feed the same peer's
    dialectic representation since Honcho aggregates per-peer across sessions."""
    return f'peer_history_v1_{_peer_id(email)}'


def _peer_email(p) -> str:
    """Recover the real email from a Honcho peer object. Metadata['email'] is
    always set on our peers, so the id fallback is a last resort only (the
    single-underscore id encoding is not reliably reversible)."""
    md = getattr(p, 'metadata', None) or {}
    email = (md.get('email') if isinstance(md, dict) else '') or ''
    if email:
        return email.strip().lower()
    return (getattr(p, 'id', '') or '').replace('_at_', '@')


# ---------------------------------------------------------------------------
# Ingest state (dedup)
# ---------------------------------------------------------------------------

def _load_ingest_state() -> dict:
    p = _ingest_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def _save_ingest_state(state: dict) -> None:
    _atomic_json_write(_ingest_state_path(), state)


# ---------------------------------------------------------------------------
# Interaction normalisation — turn gather_* output into a uniform shape:
#   {key, date(YYYY-MM-DD), source, context, participants[emails], speaker, text}
# ---------------------------------------------------------------------------

def _extract_emails(header: str) -> list[str]:
    return [m.lower() for m in _EMAIL_RE.findall(header or '')]


def _norm_date(raw: str) -> str:
    """Best-effort normalise assorted date strings to YYYY-MM-DD."""
    if not raw:
        return ''
    raw = raw.strip()
    # Already a plain YYYY-MM-DD or 'YYYY-MM-DD HH:MM'
    m = re.match(r'(\d{4}-\d{2}-\d{2})', raw)
    if m:
        return m.group(1)
    # RFC2822 email Date header
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        if dt:
            return dt.strftime('%Y-%m-%d')
    except Exception:
        pass
    return raw[:10]


def _gather_interactions(email: str, days: int) -> list[dict]:
    """Pull all sources for `email` and normalise to interaction records."""
    email = email.strip().lower()
    out: list[dict] = []

    for ev in gather_calendar(email, days):
        if 'error' in ev:
            continue
        attendees = [a.lower() for a in ev.get('attendees', []) if '@' in a]
        participants = sorted(set(attendees) | {email, MY_EMAIL})
        date = _norm_date(ev.get('date', ''))
        title = ev.get('title', '(meeting)')
        eid = ev.get('event_id', '') or f"{date}:{title}"
        others = [p for p in participants if p != MY_EMAIL]
        out.append({
            'key': f'cal:{eid}',
            'date': date,
            'source': 'calendar',
            'context': title,
            'participants': participants,
            'speaker': None,  # narrated
            'text': f"Meeting \"{title}\" with {', '.join(others)}.",
        })

    for msg in gather_gmail(email, days):
        if 'error' in msg:
            continue
        froms = _extract_emails(msg.get('from', ''))
        tos = _extract_emails(msg.get('to', ''))
        participants = sorted(set(froms) | set(tos) | {email, MY_EMAIL})
        date = _norm_date(msg.get('date', ''))
        subject = msg.get('subject', '(no subject)')
        tid = msg.get('thread_id', '')
        speaker = froms[0] if froms else None
        out.append({
            'key': f'gmail:{tid}:{date}',
            'date': date,
            'source': 'gmail',
            'context': subject,
            'participants': participants,
            'speaker': speaker,
            'text': f"Email \"{subject}\": {msg.get('snippet', '')}",
        })

    for sm in gather_slack(email, days):
        if 'error' in sm:
            continue
        date = _norm_date(sm.get('date', ''))
        text = sm.get('text', '')
        channel = sm.get('channel', '')
        # The slack gather queries `from:<peer>`, so the sender is the peer.
        out.append({
            'key': f"slack:{date}:{abs(hash(text)) % (10 ** 12)}",
            'date': date,
            'source': 'slack',
            'context': channel,
            'participants': sorted({email, MY_EMAIL}),
            'speaker': email,
            'text': f"Slack ({channel}): {text}",
        })

    for note in gather_gemini_notes(email, days):
        if 'error' in note:
            continue
        date = _norm_date(note.get('date', ''))
        title = note.get('title', '(meeting notes)')
        excerpts = ' / '.join(note.get('relevant_excerpts', [])[:5])
        out.append({
            'key': f'gemini:{title}:{date}',
            'date': date,
            'source': 'gemini_notes',
            'context': title,
            'participants': sorted({email, MY_EMAIL}),
            'speaker': None,
            'text': f"Meeting notes \"{title}\": {excerpts}",
        })

    return out


# ---------------------------------------------------------------------------
# ONA co-occurrence graph
# ---------------------------------------------------------------------------

def _load_ona_graph() -> dict:
    p = _ona_graph_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {'updated_at': '', 'edges': []}


def _update_ona_graph(interactions: list[dict]) -> int:
    """Merge new interactions into ona_graph.json. Returns edges touched."""
    graph = _load_ona_graph()
    # Index existing edges by (source, target) sorted key.
    index: dict[tuple, dict] = {}
    for e in graph.get('edges', []):
        index[tuple(sorted((e['source'], e['target'])))] = e

    touched = 0
    for it in interactions:
        people = [p for p in it.get('participants', []) if '@' in p]
        date = it.get('date', '')
        src = it.get('source', '')
        ctx = (it.get('context', '') or '')[:80]
        # Every unordered pair of participants co-occurred in this interaction.
        for i in range(len(people)):
            for j in range(i + 1, len(people)):
                a, b = sorted((people[i], people[j]))
                k = (a, b)
                edge = index.get(k)
                if edge is None:
                    edge = {
                        'source': a, 'target': b, 'weight': 0,
                        'first_interaction': date, 'last_interaction': date,
                        'sources': [], 'sample_contexts': [],
                    }
                    index[k] = edge
                edge['weight'] += 1
                if date:
                    if not edge['first_interaction'] or date < edge['first_interaction']:
                        edge['first_interaction'] = date
                    if date > edge['last_interaction']:
                        edge['last_interaction'] = date
                if src and src not in edge['sources']:
                    edge['sources'].append(src)
                if ctx and ctx not in edge['sample_contexts'] and len(edge['sample_contexts']) < 5:
                    edge['sample_contexts'].append(ctx)
                touched += 1

    graph['edges'] = sorted(index.values(), key=lambda e: -e['weight'])
    graph['updated_at'] = datetime.now().isoformat()
    _atomic_json_write(_ona_graph_path(), graph)
    return touched


# ---------------------------------------------------------------------------
# Honcho push
# ---------------------------------------------------------------------------

def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _push_to_honcho(email: str, interactions: list[dict]) -> int:
    """Add new interaction messages to the peer's Honcho session."""
    if not interactions:
        return 0
    client = _get_honcho_client()
    peer_obj = client.peer(_peer_id(email), metadata={'email': email})
    me_obj = client.peer(_peer_id(MY_EMAIL), metadata={'email': MY_EMAIL})
    session = client.session(_session_id(email))
    # Ensure both parties are attached so narrated (Brian-authored) calendar /
    # notes messages still count toward this peer's representation.
    try:
        session.add_peers([peer_obj, me_obj])
    except Exception:
        pass

    messages = []
    for it in interactions:
        speaker = it.get('speaker')
        obj = peer_obj if speaker == email else me_obj
        content = f"[{it.get('date', '')} | {it.get('source', '')}] {it.get('text', '')}"
        date = it.get('date') or None
        messages.append(obj.message(content[:_MAX_MSG_LEN], created_at=date))

    for chunk in _chunks(messages, _HONCHO_BATCH):
        session.add_messages(chunk)
    return len(messages)


# ---------------------------------------------------------------------------
# Core ingest (imported by honcho_sync_cron.py)
# ---------------------------------------------------------------------------

def ingest_peer(email: str, days: int = 30, verbose: bool = True) -> int:
    """Gather recent interactions for one peer, push new ones to Honcho and
    merge them into the ONA graph. Returns the count of new interactions."""
    email = email.strip().lower()
    if email == MY_EMAIL:
        if verbose:
            print(f"Skipping {email} (that's you).")
        return 0

    interactions = _gather_interactions(email, days)
    state = _load_ingest_state()
    seen = set(state.get(email, {}).get('keys', []))
    new = [it for it in interactions if it['key'] not in seen]

    if verbose:
        print(f"{email}: {len(interactions)} interactions found, {len(new)} new.")

    if not new:
        return 0

    if _honcho_enabled():
        try:
            n = _push_to_honcho(email, new)
            if verbose:
                print(f"  → pushed {n} messages to Honcho.")
        except Exception as e:
            if verbose:
                print(f"  ! Honcho push failed: {e}", file=sys.stderr)
    elif verbose:
        print("  (Honcho not configured — ONA graph updated only.)")

    _update_ona_graph(new)

    seen.update(it['key'] for it in new)
    state[email] = {'keys': sorted(seen), 'last_ingest': datetime.now().isoformat()}
    _save_ingest_state(state)
    return len(new)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_CARD_PROMPT = (
    "Provide a comprehensive relationship briefing on this person for Brian Mauck: "
    "who they are and their role/organization; their communication style and "
    "preferences; the key topics, deals, and threads discussed; the current state "
    "and health of the relationship; and any open items or next steps. Be specific "
    "and ground every claim in concrete interactions."
)


def _require_honcho():
    if not _honcho_enabled():
        print("ERROR: Honcho is not configured. Set HONCHO_API_KEY (cloud) or "
              "HONCHO_BASE_URL=http://localhost:8000 (self-host). See SKILL.md.",
              file=sys.stderr)
        sys.exit(1)


def cmd_read(email: str):
    _require_honcho()
    email = email.strip().lower()
    client = _get_honcho_client()
    peer = client.peer(_peer_id(email), metadata={'email': email})
    print(f"Peer card for {email}:\n")
    resp = peer.chat(_CARD_PROMPT)
    print(resp if resp else "(No representation yet — run `ingest` or `seed-all` for this peer first.)")


def cmd_query(email: str, question: str):
    _require_honcho()
    email = email.strip().lower()
    client = _get_honcho_client()
    peer = client.peer(_peer_id(email), metadata={'email': email})
    resp = peer.chat(question)
    print(resp if resp else "(No representation yet — run `ingest` or `seed-all` for this peer first.)")


def cmd_ingest(email: str, days: int = 30):
    ingest_peer(email, days, verbose=True)


def cmd_list():
    if not _honcho_enabled():
        # Fall back to the local tracked peers so `list` still works offline.
        peers = _tracked_peer_emails()
        print(f"Honcho not configured — showing {len(peers)} locally-tracked peers:\n")
        for p in peers:
            print(f"  {p}")
        return
    client = _get_honcho_client()
    emails = sorted(e for e in {_peer_email(p) for p in _iter_pages(client.peers())}
                    if '@' in e and e != MY_EMAIL)
    print(f"{len(emails)} peers tracked in Honcho:\n")
    for e in emails:
        print(f"  {e}")


def cmd_seed_dossiers():
    """Seed Honcho with the synthesised content already in dossier JSONs so the
    dialectic has a baseline before raw-history ingest completes."""
    _require_honcho()
    client = _get_honcho_client()
    state = _load_ingest_state()
    seeded = 0
    for f in _dossier_dir().glob('*.json'):
        if f.name.startswith('_') or f.name == 'ona_graph.json':
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            continue
        email = (data.get('email') or '').strip().lower()
        if '@' not in email or email == MY_EMAIL:
            continue

        key = f"dossier-seed:{data.get('last_updated', '')}"
        if key in set(state.get(email, {}).get('keys', [])):
            continue

        parts = []
        if data.get('name'):
            parts.append(f"Name: {data['name']}")
        for fld in ('role', 'organization', 'summary', 'communication_style'):
            if data.get(fld):
                parts.append(f"{fld.replace('_', ' ').title()}: {data[fld]}")
        rel = data.get('relationship', {})
        if rel:
            parts.append(f"Relationship: health {rel.get('health_score', '?')}/10, "
                         f"trend {rel.get('trend', '?')}. {rel.get('notes', '')}")
        for note in data.get('coaching_notes', [])[:10]:
            parts.append(f"Coaching note: {note}")
        if not parts:
            continue

        me_obj = client.peer(_peer_id(MY_EMAIL), metadata={'email': MY_EMAIL})
        peer_obj = client.peer(_peer_id(email), metadata={'email': email})
        session = client.session(_session_id(email))
        content = f"[dossier seed] {' | '.join(parts)}"[:_MAX_MSG_LEN]
        try:
            session.add_peers([peer_obj, me_obj])
            session.add_messages([me_obj.message(content)])
        except Exception as e:
            print(f"  ! {email}: {e}", file=sys.stderr)
            continue

        seen = set(state.get(email, {}).get('keys', []))
        seen.add(key)
        state[email] = {'keys': sorted(seen),
                        'last_ingest': data.get('last_updated', datetime.now().isoformat())}
        seeded += 1
        print(f"  ✓ seeded {email}")

    _save_ingest_state(state)
    print(f"\nSeeded {seeded} dossier(s) into Honcho.")


def cmd_seed_all(days: int = 365):
    emails = _tracked_peer_emails()
    print(f"Full historical ingest for {len(emails)} peers (last {days} days)...\n")
    total = 0
    for i, email in enumerate(emails, 1):
        print(f"[{i}/{len(emails)}] {email}")
        try:
            total += ingest_peer(email, days, verbose=True)
        except Exception as e:
            print(f"  ! failed: {e}", file=sys.stderr)
    print(f"\nDone. {total} new interactions ingested across {len(emails)} peers.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _opt_int(argv, flag, default):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return int(argv[i + 1])
    return default


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == 'read':
        if len(sys.argv) < 3:
            print('Usage: peer_card_tool.py read <email>', file=sys.stderr)
            sys.exit(1)
        cmd_read(sys.argv[2])

    elif cmd == 'query':
        if len(sys.argv) < 4:
            print('Usage: peer_card_tool.py query <email> "<question>"', file=sys.stderr)
            sys.exit(1)
        cmd_query(sys.argv[2], sys.argv[3])

    elif cmd == 'ingest':
        if len(sys.argv) < 3:
            print('Usage: peer_card_tool.py ingest <email> [--days 30]', file=sys.stderr)
            sys.exit(1)
        cmd_ingest(sys.argv[2], _opt_int(sys.argv, '--days', 30))

    elif cmd == 'list':
        cmd_list()

    elif cmd == 'seed-dossiers':
        cmd_seed_dossiers()

    elif cmd == 'seed-all':
        cmd_seed_all(_opt_int(sys.argv, '--days', 365))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: read, query, ingest, list, seed-dossiers, seed-all", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
