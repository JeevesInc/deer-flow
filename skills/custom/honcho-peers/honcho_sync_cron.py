#!/usr/bin/env python3
"""Honcho peer cards sync cron.

Runs every HONCHO_SYNC_INTERVAL seconds (default: 4 hours).
For each peer tracked in Honcho, pulls the last HONCHO_SYNC_DAYS days
of interactions from Slack, Gmail, Calendar, and Gemini notes and
stores any new sessions in Honcho + updates the ONA graph.

Runs under app/gateway/cron_supervisor.py via run_loop() — same pattern
as dossier_cron and analytics_cron.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='[HonchoSync %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('honcho_sync')

_SKILL_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _SKILL_DIR.parent / '_shared'
sys.path.insert(0, str(_SHARED_DIR))

from env_loader import load_env  # noqa: E402
load_env()

SYNC_INTERVAL = int(os.environ.get('HONCHO_SYNC_INTERVAL', str(4 * 3600)))  # 4 hours
SYNC_DAYS = int(os.environ.get('HONCHO_SYNC_DAYS', '1'))  # look back 1 day on each run
INTER_PEER_DELAY = float(os.environ.get('HONCHO_SYNC_PEER_DELAY', '0.5'))  # seconds between peers


def _get_known_peers() -> list[str]:
    """Return list of peer emails from Honcho. Falls back to dossier JSON scan."""
    try:
        sys.path.insert(0, str(_SKILL_DIR))
        from peer_card_tool import _get_honcho_client, _iter_pages, _peer_email  # noqa: E402
        client = _get_honcho_client()
        peers = sorted({_peer_email(p) for p in _iter_pages(client.peers())})
        peers = [p for p in peers if p and '@' in p]
        if peers:
            return peers
    except Exception as e:
        log.warning("Could not fetch Honcho user list: %s — falling back to dossier scan", e)

    # Fallback: scan dossier directory
    dossier_dir = _SKILL_DIR.parent.parent.parent / 'backend' / '.deer-flow' / 'dossiers'
    emails = []
    if dossier_dir.exists():
        import json
        for f in sorted(dossier_dir.glob('*.json')):
            if f.name.startswith('_') or f.name == 'ona_graph.json':
                continue
            try:
                data = json.loads(f.read_text())
                email = data.get('email', '')
                if email and '@' in email:
                    emails.append(email)
            except Exception:
                pass
    return emails


def run_sync():
    from peer_card_tool import ingest_peer  # noqa: E402

    log.info("Starting peer sync run (last %d days)...", SYNC_DAYS)
    peers = _get_known_peers()
    log.info("Found %d peers to sync", len(peers))

    success = 0
    errors = 0
    for i, email in enumerate(peers, 1):
        log.info("[%d/%d] Syncing %s", i, len(peers), email)
        try:
            n = ingest_peer(email, SYNC_DAYS, verbose=False)
            log.info("  ✓ %s: %d new sessions", email, n)
            success += 1
        except Exception as e:
            log.error("  ✗ %s: %s", email, e)
            errors += 1
        time.sleep(INTER_PEER_DELAY)

    log.info("Sync complete. %d succeeded, %d errors.", success, errors)

    # Refresh the Grafana ONA graph (Honcho Postgres tables) from the updated
    # ona_graph.json. Best-effort — never let a viz refresh fail the sync.
    try:
        sys.path.insert(0, str(_SKILL_DIR))
        import ona_tool  # noqa: E402
        # Keep all external counterparties + top internal connectors, top-3 ties
        # each — clean enough to read, complete enough to be useful.
        ona_tool.cmd_export_postgres(per_node=3, max_nodes=90)
        log.info("ONA Grafana tables refreshed.")
    except Exception as e:
        log.warning("ONA export to Postgres skipped: %s", e)


def run_loop():
    """Blocking loop — intended to be called from cron_supervisor."""
    log.info("Honcho sync cron started (interval=%ds, look_back=%dd)", SYNC_INTERVAL, SYNC_DAYS)
    while True:
        try:
            run_sync()
        except Exception as e:
            log.error("Sync run failed: %s", e)
        log.info("Sleeping %d seconds until next sync...", SYNC_INTERVAL)
        time.sleep(SYNC_INTERVAL)


if __name__ == '__main__':
    # One-shot mode when invoked directly
    run_sync()
