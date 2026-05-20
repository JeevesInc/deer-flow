"""Supervised background cron runner for the gateway process.

Runs cron jobs (like dossier_cron) as daemon threads that auto-restart
on crash. Starts/stops with the gateway lifecycle — no separate process
management needed.
"""

import importlib.util
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Restart delay after crash (seconds) — exponential backoff with cap
_INITIAL_RESTART_DELAY = 5
_MAX_RESTART_DELAY = 300  # 5 minutes
_RESET_AFTER = 600  # Reset backoff after 10 min of healthy running


class CronSupervisor:
    """Supervises a blocking cron function in a daemon thread with auto-restart."""

    def __init__(self, name: str, target: callable):
        self.name = name
        self._target = target
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._restart_delay = _INITIAL_RESTART_DELAY

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("[CronSupervisor] %s is already running", self.name)
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._supervised_loop,
            name=f"cron-{self.name}",
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
        """Run the target with auto-restart on crash."""
        while not self._stop_event.is_set():
            started_at = time.monotonic()
            try:
                logger.info("[CronSupervisor] %s starting", self.name)
                self._target()
                # If run_loop returns normally, we're done
                break
            except Exception:
                elapsed = time.monotonic() - started_at
                if elapsed > _RESET_AFTER:
                    self._restart_delay = _INITIAL_RESTART_DELAY

                logger.exception(
                    "[CronSupervisor] %s crashed, restarting in %ds",
                    self.name,
                    self._restart_delay,
                )

                if self._stop_event.wait(timeout=self._restart_delay):
                    break  # Stop was requested during wait

                self._restart_delay = min(self._restart_delay * 2, _MAX_RESTART_DELAY)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_supervisors: list[CronSupervisor] = []


def _load_and_start(name: str, script_path: Path) -> None:
    """Load a cron script by path and start it under supervision."""
    if not script_path.exists():
        logger.info("[CronSupervisor] %s not found at %s, skipping", name, script_path)
        return
    try:
        spec = importlib.util.spec_from_file_location(name, str(script_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sv = CronSupervisor(name, mod.run_loop)
        _supervisors.append(sv)
        sv.start()
    except Exception:
        logger.exception("[CronSupervisor] Failed to load %s", name)


def start_crons() -> None:
    """Start all configured cron jobs. Called from gateway lifespan."""
    skills_dir = Path(__file__).resolve().parent.parent.parent.parent / "skills" / "custom"
    backend_dir = Path(__file__).resolve().parent.parent.parent

    _load_and_start("dossier-cron", skills_dir / "jeeves-dossier" / "dossier_cron.py")
    _load_and_start("analytics-cron", skills_dir / "jeeves-analytics" / "analytics_cron.py")
    # email-monitor disabled 2026-05-20 — replaced by webhook_receiver.py (Gmail Pub/Sub push + Haiku classifier).
    # Re-enable only if the webhook pipe goes down for an extended period.
    # _load_and_start("email-monitor", skills_dir / "gmail" / "email_monitor_cron.py")
    _load_and_start("report-scheduler", skills_dir / "jeeves-borrowing-base" / "report_scheduler_cron.py")
    _load_and_start("knowledge-crawler", skills_dir / "knowledge-crawler" / "knowledge_cron.py")
    _load_and_start("revenue-comp", skills_dir / "jeeves-analytics" / "revenue_comp_cron.py")
    _load_and_start("state-backup", backend_dir / "scripts" / "backup_state.py")
    _load_and_start("checkpoint-cleanup", backend_dir / "scripts" / "checkpoint_cleanup_cron.py")
    _load_and_start("slack-dm-monitor", skills_dir / "slack-search" / "slack_dm_monitor_cron.py")
    _load_and_start("bot-dm-history", backend_dir / "scripts" / "bot_dm_history_cron.py")
    _load_and_start("dreams-cron", skills_dir / "gmail" / "dreams_cron.py")
    _load_and_start("eod-review", skills_dir / "gmail" / "eod_review_cron.py")


def stop_crons() -> None:
    """Stop all running cron jobs. Called from gateway lifespan."""
    for sv in _supervisors:
        sv.stop()
    _supervisors.clear()
