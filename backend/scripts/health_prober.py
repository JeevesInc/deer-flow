#!/usr/bin/env python3
"""DeerFlow health prober — the supervision brain for the WinSW services (P3-1).

WinSW handles process-death restarts natively; this covers what it can't see:
  - a process that is UP but WEDGED (port open, endpoint dead/hanging)
  - the gateway's healthy-but-busy pattern (deep /health starved during cron
    bursts — needs a long grace, the 2026-06-16 flap)
  - the qdrant/monitoring docker containers (agent runs memory-less if qdrant
    stops — P0-6)
  - venv integrity (interrupted-sync package wipes — P3-7)

Design: probe every TICK seconds, restart the owning Windows service only
after `fail_threshold` CONSECUTIVE failures, never more than once per
RESTART_COOLDOWN per service, and DM the owner on every restart + recovery.
All side effects (probe/restart/notify/docker) are injected so the decision
logic is unit-testable (test_health_prober.py).

Replaces the start.sh supervision loop. The entire cygwin-PID / lockdir /
heartbeat / duplicate-supervisor problem space is gone: Windows SCM is the
singleton, WinSW owns the logs (no copytruncate NUL corruption).
"""

from __future__ import annotations

import logging
import os
import shutil
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
_SHARED = _BACKEND.parent / "skills" / "custom" / "_shared"
for p in (str(_BACKEND), str(_SHARED)):
    if p not in sys.path:
        sys.path.insert(0, p)

# As a Windows service there is no shell profile to populate the environment —
# notify.dm_owner needs SLACK_BOT_TOKEN/SLACK_OWNER_USER_ID from backend/.env.
try:
    from dotenv import load_dotenv
    load_dotenv(_BACKEND / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
                    format="[prober %(asctime)s] %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("prober")

TICK_SECONDS = int(os.environ.get("PROBER_TICK_SECONDS", "30"))
RESTART_COOLDOWN = int(os.environ.get("PROBER_RESTART_COOLDOWN", "300"))
DOCKER_CHECK_EVERY_TICKS = 10  # ~5 min


@dataclass
class Check:
    """One endpoint probe bound to the Windows service that owns it."""
    name: str
    service: str
    url: str
    fail_threshold: int          # consecutive failed ticks before restart
    critical: bool = True        # non-critical -> restart but never alarm-spam
    # runtime state
    streak: int = 0
    down_since: float | None = None
    notified_down: bool = False


def default_checks() -> list[Check]:
    return [
        Check("langgraph", "deerflow-langgraph", "http://localhost:2024/ok", 2),
        # /livez answers as soon as the loop serves; /health does deep checks
        # and is starved during cron-startup bursts — 8 ticks (~4 min) grace,
        # same as GW_HEALTH_GRACE_TICKS in the old supervisor.
        Check("gateway-live", "deerflow-gateway", "http://localhost:8001/livez", 2),
        Check("gateway-deep", "deerflow-gateway", "http://localhost:8001/health", 8),
        Check("webhook", "deerflow-webhook", "http://localhost:8080/health", 3),
        Check("ngrok", "deerflow-ngrok", "http://127.0.0.1:4040/api/tunnels", 3,
              critical=False),
        Check("dashboard", "deerflow-dashboard", "http://localhost:8100/_stcore/health", 5,
              critical=False),
    ]


# ---------------------------------------------------------------------------
# Side-effect backends (injected into Monitor for testability)
# ---------------------------------------------------------------------------

def http_probe(url: str, timeout: float = 5.0) -> bool:
    try:
        import httpx
        return httpx.get(url, timeout=timeout).status_code == 200
    except Exception:
        return False


def restart_windows_service(service: str) -> bool:
    """net stop (tolerating already-stopped) then net start."""
    try:
        subprocess.run(["net", "stop", service, "/y"], capture_output=True, timeout=90)
        res = subprocess.run(["net", "start", service], capture_output=True, timeout=120)
        return res.returncode == 0
    except Exception as e:
        log.error("restart of %s failed: %s", service, e)
        return False


def notify_owner(text: str) -> None:
    try:
        from notify import dm_owner
        dm_owner(text)
    except Exception as e:
        log.warning("owner DM failed: %s", e)


def ensure_docker_monitoring() -> None:
    """Keep the monitoring/qdrant containers up (P0-6: a stopped
    deerflow-qdrant means the agent silently runs with no memory)."""
    compose_dir = _BACKEND.parent.parent / "monitoring"
    try:
        res = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return  # engine down (user session / Docker Desktop) — logon path owns that
        names = set(res.stdout.split())
        expected = {"deerflow-qdrant", "deerflow-grafana", "deerflow-prometheus",
                    "deerflow-loki", "deerflow-promtail"}
        missing = expected - names
        if missing:
            log.warning("containers missing (%s) — docker compose up -d", ", ".join(sorted(missing)))
            subprocess.run(["docker", "compose", "up", "-d"], cwd=str(compose_dir),
                           capture_output=True, timeout=180)
            notify_owner(f":whale: Monitoring containers were down ({', '.join(sorted(missing))}) — restarted via compose.")
    except FileNotFoundError:
        pass  # no docker CLI — nothing to manage
    except Exception as e:
        log.warning("docker check failed: %s", e)


def find_uv() -> str | None:
    """Resolve uv.exe — a service account's PATH does not include the
    per-user Python Scripts directory where uv lives."""
    found = shutil.which("uv")
    if found:
        return found
    candidates = [_BACKEND / ".venv" / "Scripts" / "uv.exe"]
    for scripts_dir in Path(r"C:\Users").glob(r"*\AppData\Local\Programs\Python\Python3*\Scripts"):
        candidates.append(scripts_dir / "uv.exe")
    for cand in candidates:
        if cand.exists():
            return str(cand)
    return None


def check_venv_integrity() -> None:
    """One-shot at startup: catch interrupted-sync package wipes (P3-7)."""
    uv = find_uv()
    if uv is None:
        log.warning("venv check skipped: uv.exe not found")
        return
    try:
        res = subprocess.run([uv, "pip", "check"], cwd=str(_BACKEND),
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            log.error("uv pip check failed:\n%s", res.stdout[-1500:])
            notify_owner(":warning: venv integrity check failed at prober start — "
                         "run 'uv sync --inexact --frozen' in deer-flow/backend.\n"
                         + res.stdout[-500:])
    except Exception as e:
        log.warning("venv check skipped: %s", e)



# ---------------------------------------------------------------------------
# 2026-10-08 hardening (Brian: "that all needs to never happen again")
#  1. Every LangGraph/Gateway restart -> owner DM, whoever caused it.
#  2. checkpoints.db self-heal: stuck WAL reader -> restart LangGraph;
#     >50% free pages -> VACUUM; dead (error/interrupted) threads -> deleted.
#  All silent when there is nothing to fix.
# ---------------------------------------------------------------------------

WATCHED_SERVICES = ("deerflow-langgraph", "deerflow-gateway")
_CKPT_DB = _BACKEND / ".deer-flow" / "checkpoints.db"
_CHANNEL_STORE = _BACKEND / ".deer-flow" / "channels" / "store.json"
_LG = "http://localhost:2024"
_GW = "http://localhost:8001"
_DB_STATE = {"stuck_wal_strikes": 0}


def service_pid(service: str) -> int | None:
    try:
        out = subprocess.run(["sc", "queryex", service], capture_output=True, text=True, timeout=20).stdout
        for line in out.splitlines():
            if line.strip().startswith("PID"):
                pid = int(line.split(":")[1].strip())
                return pid or None
    except Exception:
        pass
    return None


def watch_restarts(known: dict, mon) -> None:
    for svc in WATCHED_SERVICES:
        pid = service_pid(svc)
        prev = known.get(svc)
        known[svc] = pid
        if prev is None or pid is None or pid == prev:
            continue
        mine = mon.last_restart.get(svc)
        who = "by the health prober" if (mine is not None and mon.clock() - mine < 600) else "externally (not the prober)"
        notify_owner(f":arrows_counterclockwise: {svc} restarted {who} at "
                     f"{time.strftime('%H:%M')} (PID {prev} -> {pid}). In-flight runs on it were cut off.")


def _busy_runs() -> int | None:
    try:
        import httpx
        r = httpx.post(f"{_LG}/threads/search", json={"status": "busy", "limit": 50}, timeout=15)
        return len(r.json())
    except Exception:
        return None


def _delete_dead_threads() -> None:
    import httpx
    from datetime import datetime, timezone
    protected = set()
    try:
        for e in json.loads(_CHANNEL_STORE.read_text(encoding="utf-8")).values():
            if isinstance(e, dict) and isinstance(e.get("thread_id"), str):
                protected.add(e["thread_id"])
    except Exception:
        return  # can't tell what's mapped to Slack -> delete nothing
    cutoff = time.time() - 3600
    failed = []
    deleted = 0
    for st in ("error", "interrupted"):
        try:
            batch = httpx.post(f"{_LG}/threads/search", json={"status": st, "limit": 500}, timeout=30).json()
        except Exception:
            return
        for t in batch:
            tid = t.get("thread_id")
            if not tid or tid in protected:
                continue
            try:
                upd = datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if upd > cutoff:
                continue
            r = httpx.delete(f"{_LG}/threads/{tid}", timeout=30)
            if r.status_code in (200, 204, 404):
                deleted += 1
                try:
                    httpx.delete(f"{_GW}/api/threads/{tid}", timeout=30)
                except Exception:
                    pass
            else:
                failed.append(tid[:8])
    if deleted:
        log.info("dead-thread sweep: deleted %d", deleted)
    if len(failed) >= 3:
        notify_owner(f":warning: Dead-thread sweep: {len(failed)} LangGraph DELETEs failed "
                     f"({', '.join(failed[:5])}) — checkpoint DB likely wedged; next DB check will act.")


def check_checkpoint_db(mon) -> None:
    import sqlite3
    if not _CKPT_DB.exists():
        return
    wal = Path(str(_CKPT_DB) + "-wal")
    try:
        con = sqlite3.connect(str(_CKPT_DB), timeout=10)
        con.execute("PRAGMA busy_timeout=10000")
        busy_flag = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        pages = con.execute("PRAGMA page_count").fetchone()[0]
        free = con.execute("PRAGMA freelist_count").fetchone()[0]
        con.close()
    except Exception as e:
        log.warning("checkpoint DB check failed: %s", e)
        return
    wal_mb = wal.stat().st_size / 1e6 if wal.exists() else 0.0
    db_mb = _CKPT_DB.stat().st_size / 1e6

    # (a) A reader pinned in LangGraph blocks WAL checkpoints; writers then die
    #     with 'database is locked' and DELETE /threads 500s. Two strikes -> restart.
    if busy_flag and wal_mb > 50:
        _DB_STATE["stuck_wal_strikes"] += 1
    else:
        _DB_STATE["stuck_wal_strikes"] = 0
    if _DB_STATE["stuck_wal_strikes"] >= 2 and _busy_runs() == 0:
        mon.last_restart["deerflow-langgraph"] = mon.clock()
        ok = restart_windows_service("deerflow-langgraph")
        _DB_STATE["stuck_wal_strikes"] = 0
        notify_owner(f":wrench: checkpoints.db WAL stuck at {wal_mb:.0f} MB (LangGraph held a stale read) — "
                     f"restarted deerflow-langgraph: {'OK' if ok else 'FAILED, needs a look'}. No runs were active.")
        return

    # (b) Bloat: deletes leave free pages; never let it reach 1.2 GB again.
    if pages and free / pages > 0.5 and db_mb > 300 and _busy_runs() == 0:
        try:
            con = sqlite3.connect(str(_CKPT_DB), timeout=30)
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("VACUUM")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
            notify_owner(f":broom: checkpoints.db was {db_mb:.0f} MB ({free * 100 // pages}% free pages) — "
                         f"VACUUMed to {_CKPT_DB.stat().st_size / 1e6:.0f} MB.")
        except Exception as e:
            log.warning("VACUUM skipped: %s", e)

    _delete_dead_threads()

# ---------------------------------------------------------------------------
# Decision core (pure-ish; fully injected)
# ---------------------------------------------------------------------------

@dataclass
class Monitor:
    checks: list[Check]
    probe: callable = http_probe
    restart: callable = restart_windows_service
    notify: callable = notify_owner
    clock: callable = time.monotonic
    restart_cooldown: float = RESTART_COOLDOWN
    last_restart: dict = field(default_factory=dict)   # service -> monotonic ts

    def tick(self) -> None:
        # One probe pass; a service with several checks restarts if ANY of its
        # checks crosses threshold (deduped by the per-service cooldown).
        for c in self.checks:
            ok = bool(self.probe(c.url))
            if ok:
                if c.notified_down:
                    self.notify(f":white_check_mark: {c.name} recovered.")
                if c.streak >= c.fail_threshold:
                    log.info("%s recovered (was %s failed ticks)", c.name, c.streak)
                c.streak = 0
                c.down_since = None
                c.notified_down = False
                continue

            c.streak += 1
            if c.down_since is None:
                c.down_since = self.clock()
            log.warning("%s probe failed (streak %s/%s)", c.name, c.streak, c.fail_threshold)
            if c.streak >= c.fail_threshold:
                self._maybe_restart(c)

    def _maybe_restart(self, c: Check) -> None:
        now = self.clock()
        last = self.last_restart.get(c.service)
        if last is not None and (now - last) < self.restart_cooldown:
            # Recently restarted (possibly by a sibling check) — let it settle.
            return
        self.last_restart[c.service] = now
        log.error("%s unhealthy for %s ticks — restarting %s", c.name, c.streak, c.service)
        ok = self.restart(c.service)
        if not c.notified_down:
            c.notified_down = True
            self.notify(
                f":arrows_counterclockwise: {c.name} unhealthy "
                f"({c.streak} consecutive failed probes) — service {c.service} "
                f"restart {'issued' if ok else 'FAILED'}."
            )
        if not ok:
            self.notify(f":rotating_light: restart of {c.service} FAILED — manual attention needed.")
        # Fresh grace after a restart attempt.
        for sib in self.checks:
            if sib.service == c.service:
                sib.streak = 0


def main() -> None:
    log.info("Health prober starting (tick=%ss, cooldown=%ss)", TICK_SECONDS, RESTART_COOLDOWN)
    check_venv_integrity()
    mon = Monitor(checks=default_checks())
    tick_n = 0
    known_pids: dict = {}
    db_every = max(1, int(1800 // TICK_SECONDS))  # checkpoint DB check every ~30 min
    while True:
        try:
            mon.tick()
            tick_n += 1
            if tick_n % DOCKER_CHECK_EVERY_TICKS == 0:
                ensure_docker_monitoring()
        except Exception:
            log.exception("prober tick failed")
        try:
            watch_restarts(known_pids, mon)
        except Exception:
            log.exception("restart watch failed")
        if tick_n % db_every == 0:
            try:
                check_checkpoint_db(mon)
            except Exception:
                log.exception("checkpoint DB check failed")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
