#!/usr/bin/env python3
"""Watchdog: detects Cygwin pty failures in LangGraph workers and recycles LG.

When LangGraph runs for too long on Windows, its Cygwin-based bash workers
eventually fail to allocate ptys:

    sh.EXE: *** fatal error - couldn't initialize fd 0 for /dev/pty0
    Exit Code: 256

In that state every agent bash call fails silently. The gateway's
supervisor can't catch this through LG's /health endpoint because LG
itself still serves HTTP — only its worker bash subprocess pool is
degraded.

This watchdog runs as a daemon thread inside the gateway's cron
supervisor. Once per minute it scans new lines of langgraph.log for the
pty error pattern. On detection it:
  1. Sends an owner DM ("LG degraded, restarting").
  2. Kills the python.exe currently listening on :2024.
  3. Spawns a fresh LangGraph via the same command start.sh uses.
  4. Enters a 10-min cooldown before scanning again.

State (last log file position) is held in module globals — reset on
gateway restart, which is fine because the gateway also re-tails fresh
on boot.
"""

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LANGGRAPH_LOG = PROJECT_ROOT / "langgraph.log"
BACKEND_DIR = PROJECT_ROOT / "deer-flow" / "backend"
LANGGRAPH_EXE = BACKEND_DIR / ".venv" / "Scripts" / "langgraph.exe"

PTY_ERROR_PATTERN = re.compile(
    r"couldn't initialize fd 0 for /dev/pty0|sh\.EXE: \*\*\* fatal error"
)

POLL_INTERVAL = 60
COOLDOWN_SECONDS = 600
LANGGRAPH_PORT = 2024
SPAWN_SETTLE_SECONDS = 120

logging.basicConfig(
    level=logging.INFO,
    format="[PtyWatchdog %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pty_watchdog")

_last_scan_pos = 0


def _slack(text: str) -> None:
    """Best-effort owner DM — never raises."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    owner = os.environ.get("SLACK_OWNER_USER_ID")
    if not (token and owner):
        return
    try:
        from slack_sdk import WebClient
        c = WebClient(token=token)
        ch = c.conversations_open(users=[owner])["channel"]["id"]
        c.chat_postMessage(channel=ch, text=text)
    except Exception as e:
        log.warning("Slack DM failed: %s", e)


def _find_langgraph_pid() -> int | None:
    """Find the PID listening on :2024."""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        log.error("netstat failed: %s", e)
        return None
    for line in out.splitlines():
        if f":{LANGGRAPH_PORT} " in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
    return None


def _scan_for_pty_error() -> bool:
    """Scan NEW lines of langgraph.log since the last call. Returns True on hit."""
    global _last_scan_pos
    if not LANGGRAPH_LOG.exists():
        return False
    try:
        size = LANGGRAPH_LOG.stat().st_size
        if size < _last_scan_pos:
            _last_scan_pos = 0
        with open(LANGGRAPH_LOG, "rb") as f:
            f.seek(_last_scan_pos)
            data = f.read()
        _last_scan_pos = size
    except Exception as e:
        log.warning("Could not read %s: %s", LANGGRAPH_LOG, e)
        return False
    text = data.decode("utf-8", errors="replace")
    return bool(PTY_ERROR_PATTERN.search(text))


def _restart_langgraph() -> bool:
    """Kill LG and spawn a fresh one via the standard command."""
    pid = _find_langgraph_pid()
    if pid is None:
        log.error("No process listening on :%d", LANGGRAPH_PORT)
        return False

    log.info("Killing LangGraph PID %d", pid)
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("taskkill failed: %s", e.stderr)
        return False

    time.sleep(3)

    if not LANGGRAPH_EXE.exists():
        log.error("langgraph.exe not found at %s", LANGGRAPH_EXE)
        return False

    env = os.environ.copy()
    env["BG_JOB_ISOLATED_LOOPS"] = "true"
    try:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [
                str(LANGGRAPH_EXE), "dev",
                "--config", "langgraph.json",
                "--host", "0.0.0.0",
                "--port", str(LANGGRAPH_PORT),
                "--no-browser",
                "--allow-blocking",
                "--n-jobs-per-worker", "5",
            ],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=open(LANGGRAPH_LOG, "ab"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        log.info("Spawned fresh LangGraph (detached)")
        return True
    except Exception as e:
        log.error("Failed to spawn LangGraph: %s", e)
        return False


def run_loop():
    """Main loop, invoked by cron_supervisor."""
    global _last_scan_pos
    # Start scanning from current end of log — don't fire on historical errors
    if LANGGRAPH_LOG.exists():
        _last_scan_pos = LANGGRAPH_LOG.stat().st_size
    log.info(
        "PtyWatchdog started. poll=%ds, cooldown=%ds, log=%s",
        POLL_INTERVAL, COOLDOWN_SECONDS, LANGGRAPH_LOG,
    )

    last_restart = 0
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            now = time.time()
            if now - last_restart < COOLDOWN_SECONDS:
                continue
            if not _scan_for_pty_error():
                continue

            log.warning("Cygwin pty failure detected in langgraph.log")
            _slack(
                ":rotating_light: *LangGraph pty degraded — auto-restarting*\n"
                "Detected `couldn't initialize fd 0 for /dev/pty0` in langgraph.log. "
                "Recycling the LG process so agent bash works again."
            )
            ok = _restart_langgraph()
            if ok:
                last_restart = now
                time.sleep(SPAWN_SETTLE_SECONDS)
                # Reset scan position to skip any error lines emitted during
                # the broken process's death throes
                if LANGGRAPH_LOG.exists():
                    _last_scan_pos = LANGGRAPH_LOG.stat().st_size
                _slack(
                    ":white_check_mark: *LangGraph restart complete*\n"
                    "Fresh LG process spawned; agent bash should be functional."
                )
            else:
                _slack(
                    ":warning: *LangGraph auto-restart failed*\n"
                    "Watchdog detected pty failure but could not restart LG. "
                    "Manual intervention required (taskkill :2024 + start.sh)."
                )
                last_restart = now  # cooldown anyway to avoid spam
        except Exception as e:
            log.exception("Loop error: %s", e)


if __name__ == "__main__":
    run_loop()
