"""
Knowledge crawler cron — runs inside the gateway via CronSupervisor.

Schedule:
  - Email + Slack: every 6 hours
  - Drive: every 24 hours
  - Synthesis runs after each crawl cycle
"""

import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("knowledge-cron")

# Interval between crawl cycles (seconds)
CRAWL_INTERVAL = 6 * 3600  # 6 hours


def _ensure_env():
    """Load .env if not already loaded."""
    env_path = Path(__file__).resolve().parent.parent.parent.parent / "backend" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if key not in os.environ:
                os.environ[key] = val.strip().strip('"').strip("'")


def run_loop():
    """Blocking loop — called by CronSupervisor."""
    _ensure_env()

    # Add skill dirs to path
    skill_dir = Path(__file__).resolve().parent
    shared_dir = skill_dir.parent / "_shared"
    for p in [str(skill_dir), str(shared_dir)]:
        if p not in sys.path:
            sys.path.insert(0, p)

    from crawler import run_crawl

    log.info(f"Knowledge crawler cron started. Crawl interval: {CRAWL_INTERVAL}s")

    while True:
        try:
            log.info("Running knowledge crawl cycle...")
            items = run_crawl()
            log.info(f"Crawl cycle complete: {items} items processed")
        except Exception as e:
            log.error(f"Crawl cycle failed: {e}", exc_info=True)

        time.sleep(CRAWL_INTERVAL)
