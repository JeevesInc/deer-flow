"""Supervised background cron runner for the gateway process.

Runs cron jobs (like dossier_cron) as daemon threads that auto-restart
on crash. Starts/stops with the gateway lifecycle -- no separate process
management needed.

Crash alerting: on first crash (and again at each backoff doubling),
posts a Slack DM to the owner so silent failures are visible.
"""

import importlib.util
import logging
import os
import threading
import time
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)

_INITIAL_RESTART_DELAY = 5
_MAX_RESTART_DELAY = 300
_RESET_AFTER = 600


def _slack_alert(text: str) -> None:
    """Post a DM to the owner. Best-effort -- never raises."""
    token    = os.environ.get("SLACK_BOT_TOKEN")
    owner_id = os.environ.get("SLACK_OWNER_USER_ID")
    if not token or not owner_id:
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        ch = client.conversations_open(users=[owner_id])["channel"]["id"]
        client.chat_postMessage(channel=ch, text=text)
    except Exception as e:
        logger.warning("[CronSupervisor] Slack alert failed: %s", e)


class CronSupervisor:
    """Supervises a blocking cron function in a daemon thread with auto-restart."""

    def __init__(self, name: str, target: callable, startup_delay: float = 0.0):
        self.name = name
        self._target = target
        self._startup_delay = startup_delay
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._restart_delay = _INITIAL_RESTART_DELAY
        self._crash_count = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("[CronSupervisor] %s is already running", self.name)
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._supervised_loop,
            name="cron-" + self.name,
            daemon=True,
        )
        self._thread.start()
        logger.info("[CronSupervisor] %s started", self.name)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("[CronSupervisor] %s stopped", self.name)

    def _supervised_loop(self) -> None:
        # Staggered startup: every cron used to fire its first iteration the
        # instant the gateway started, so ~15 crons (Redshift, Anthropic Vision,
        # mem0 derivation) hammered at once and starved the async /health probe
        # on startup → the supervisor SIGKILLed a healthy-but-busy gateway →
        # restart → same thundering herd (the 2026-06-16 flap). Spread the first
        # runs out (interruptible so stop() is still responsive).
        if self._startup_delay > 0 and self._stop_event.wait(timeout=self._startup_delay):
            return
        while not self._stop_event.is_set():
            started_at = time.monotonic()
            try:
                logger.info("[CronSupervisor] %s starting", self.name)
                self._target()
                if self._crash_count > 0:
                    _slack_alert(
                        ":white_check_mark: *Cron recovered: `" + self.name + "`* -- "
                        + "running normally after " + str(self._crash_count) + " crash(es)."
                    )
                break
            except Exception:
                self._crash_count += 1
                elapsed = time.monotonic() - started_at
                if elapsed > _RESET_AFTER:
                    self._restart_delay = _INITIAL_RESTART_DELAY

                tb = traceback.format_exc()
                logger.exception(
                    "[CronSupervisor] %s crashed (attempt %d), restarting in %ds",
                    self.name, self._crash_count, self._restart_delay,
                )

                # Alert on first crash, then at each power-of-two to avoid spam
                should_alert = (
                    self._crash_count == 1
                    or (self._crash_count & (self._crash_count - 1) == 0)
                )
                if should_alert:
                    last_lines = chr(10).join(tb.strip().splitlines()[-6:])
                    _slack_alert(
                        ":rotating_light: *Cron crashed: `" + self.name + "`* "
                        + "(attempt #" + str(self._crash_count) + ", retry in "
                        + str(self._restart_delay) + "s)" + chr(10)
                        + "```" + last_lines + "```"
                    )

                if self._stop_event.wait(timeout=self._restart_delay):
                    break

                self._restart_delay = min(self._restart_delay * 2, _MAX_RESTART_DELAY)


_supervisors: list[CronSupervisor] = []

# Seconds between each cron's first run, to avoid a thundering-herd startup
# burst that starves the gateway's /health probe (see _supervised_loop).
_STAGGER_SECONDS = 8.0
_stagger_index = 0


def _load_and_start(name: str, script_path: Path) -> None:
    global _stagger_index
    if not script_path.exists():
        logger.info("[CronSupervisor] %s not found at %s, skipping", name, script_path)
        return
    try:
        spec = importlib.util.spec_from_file_location(name, str(script_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sv = CronSupervisor(name, mod.run_loop, startup_delay=_stagger_index * _STAGGER_SECONDS)
        _stagger_index += 1
        _supervisors.append(sv)
        sv.start()
    except Exception:
        tb = traceback.format_exc()
        logger.exception("[CronSupervisor] Failed to load %s", name)
        last_lines = chr(10).join(tb.strip().splitlines()[-6:])
        _slack_alert(
            ":rotating_light: *Cron failed to load: `" + name + "`*" + chr(10)
            + "Path: `" + str(script_path) + "`" + chr(10)
            + "```" + last_lines + "```" + chr(10)
            + "This cron is *not running*. Fix the script and restart the gateway."
        )


def start_crons() -> None:
    """Start all configured cron jobs. Called from gateway lifespan."""
    skills_dir = Path(__file__).resolve().parent.parent.parent.parent / "skills" / "custom"
    backend_dir = Path(__file__).resolve().parent.parent.parent

    _load_and_start("dossier-cron", skills_dir / "jeeves-dossier" / "dossier_cron.py")
    _load_and_start("analytics-cron", skills_dir / "jeeves-analytics" / "analytics_cron.py")
    # email-monitor disabled 2026-05-20 -- replaced by webhook_receiver.py
    # _load_and_start("email-monitor", skills_dir / "gmail" / "email_monitor_cron.py")
    _load_and_start("report-scheduler", skills_dir / "jeeves-borrowing-base" / "report_scheduler_cron.py")
    _load_and_start("knowledge-crawler", skills_dir / "knowledge-crawler" / "knowledge_cron.py")
    _load_and_start("revenue-comp", skills_dir / "jeeves-analytics" / "revenue_comp_cron.py")
    _load_and_start("state-backup", backend_dir / "scripts" / "backup_state.py")
    _load_and_start("checkpoint-cleanup", backend_dir / "scripts" / "checkpoint_cleanup_cron.py")
    _load_and_start("idle-thread-cleanup", backend_dir / "scripts" / "idle_thread_cleanup_cron.py")
    _load_and_start("slack-dm-monitor", skills_dir / "slack-search" / "slack_dm_monitor_cron.py")
    _load_and_start("bot-dm-history", backend_dir / "scripts" / "bot_dm_history_cron.py")
    _load_and_start("cap-markets-refresh", backend_dir / "scripts" / "cap_markets_metrics_refresh.py")
    _load_and_start("cico-cash", backend_dir / "scripts" / "cico_cash_extract.py")
    _load_and_start("diligence-registry", skills_dir / "jeeves-diligence" / "diligence_registry_cron.py")
    _load_and_start("dreams-cron", skills_dir / "gmail" / "dreams_cron.py")
    _load_and_start("eod-review", skills_dir / "gmail" / "eod_review_cron.py")
    _load_and_start("langgraph-pty-watchdog", backend_dir / "scripts" / "langgraph_pty_watchdog.py")
    _load_and_start("honcho-sync", skills_dir / "honcho-peers" / "honcho_sync_cron.py")
    # cm-dashboard moved OUT of the gateway 2026-06-16. Loading it here called
    # spec.loader.exec_module() on the Streamlit script, whose UNGUARDED module
    # top level executed the full dashboard render + 3 synchronous Redshift
    # queries (load_dq_history / load_roll_rate / load_nco_history) on the
    # gateway's main thread at EVERY startup. Under slow Redshift/Zscaler that
    # blocked /health past the supervisor's 60s readiness gate, so the
    # supervisor kill/restart-looped the gateway (171 "unhealthy" events on
    # 2026-06-16) and orphaned a Streamlit subprocess each cycle. The dashboard
    # now runs as a supervised, log-isolated service launched from start.sh
    # (start_dashboard), decoupled from the gateway lifecycle.
    # _load_and_start("cm-dashboard", backend_dir / "scripts" / "cm_credit_health_app.py")


def stop_crons() -> None:
    for sv in _supervisors:
        sv.stop()
    _supervisors.clear()
