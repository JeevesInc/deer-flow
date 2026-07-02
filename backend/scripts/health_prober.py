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


def check_venv_integrity() -> None:
    """One-shot at startup: catch interrupted-sync package wipes (P3-7)."""
    try:
        res = subprocess.run(["uv", "pip", "check"], cwd=str(_BACKEND),
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            log.error("uv pip check failed:\n%s", res.stdout[-1500:])
            notify_owner(":warning: venv integrity check failed at prober start — "
                         "run 'uv sync --inexact --frozen' in deer-flow/backend.\n"
                         + res.stdout[-500:])
    except Exception as e:
        log.warning("venv check skipped: %s", e)


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
    while True:
        try:
            mon.tick()
            tick_n += 1
            if tick_n % DOCKER_CHECK_EVERY_TICKS == 0:
                ensure_docker_monitoring()
        except Exception:
            log.exception("prober tick failed")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
