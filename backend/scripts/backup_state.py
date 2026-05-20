"""Nightly backup of high-value DeerFlow state.

Tars the critical (small, hard-to-regenerate) pieces of .deer-flow/ into
a timestamped archive and prunes archives older than RETENTION_DAYS.

Excludes:
  - checkpoints.db / -shm / -wal  (large, transient, has its own retention)
  - threads/                       (per-thread workspaces, regenerable)
  - slack_downloads/               (cached binaries, regenerable)
  - knowledge/                     (re-crawled by knowledge-crawler cron)

Run as a cron in cron_supervisor.py (daily at 03:00 local).
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil
import tarfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

RETENTION_DAYS = 14
DAILY_INTERVAL_SEC = 24 * 3600
BACKUP_HOUR_LOCAL = 3  # run around 03:00

EXCLUDE_NAMES = {
    "checkpoints.db",
    "checkpoints.db-shm",
    "checkpoints.db-wal",
    "threads",
    "slack_downloads",
    "knowledge",
}


def _state_dir() -> Path:
    return Path(__file__).resolve().parent.parent / ".deer-flow"


def _backup_dir() -> Path:
    d = Path(__file__).resolve().parent.parent / ".deer-flow-backups"
    d.mkdir(exist_ok=True)
    return d


def _should_skip(path: Path, src_root: Path) -> bool:
    try:
        rel = path.relative_to(src_root)
    except ValueError:
        return False
    return rel.parts and rel.parts[0] in EXCLUDE_NAMES


def _walk_paths(root: Path):
    """Yield (abs_path, arcname) for every file under root, excluding top-level
    names in EXCLUDE_NAMES. Walks manually so one unreadable file doesn't abort.
    """
    for entry in root.iterdir():
        if entry.name in EXCLUDE_NAMES:
            continue
        if entry.is_file() or entry.is_symlink():
            yield entry, entry.name
            continue
        if entry.is_dir():
            for sub in entry.rglob("*"):
                if not sub.is_file():
                    continue
                yield sub, str(sub.relative_to(root))


def make_backup() -> Path | None:
    src = _state_dir()
    if not src.exists():
        logger.warning("[backup] %s does not exist; skipping", src)
        return None

    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out = _backup_dir() / f"deerflow-state-{ts}.tar.gz"
    logger.info("[backup] Writing %s", out)

    added = 0
    skipped = 0
    with tarfile.open(out, "w:gz") as tf:
        for abs_path, arcname in _walk_paths(src):
            try:
                tf.add(str(abs_path), arcname=arcname, recursive=False)
                added += 1
            except (PermissionError, OSError) as e:
                # Common cause: a sqlite shm/wal or vector store file is
                # held open by the running gateway. Skip and continue —
                # a 99%-complete backup beats no backup.
                skipped += 1
                logger.debug("[backup] skipped %s: %s", abs_path, e)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info("[backup] Wrote %s (%.1f MB, %d files, %d skipped)", out, size_mb, added, skipped)
    if skipped:
        logger.info("[backup] Skipped %d locked file(s) — usually sqlite wal/shm or vector-store internals", skipped)
    return out


def prune_old() -> int:
    cutoff = dt.datetime.now() - dt.timedelta(days=RETENTION_DAYS)
    dropped = 0
    for f in _backup_dir().glob("deerflow-state-*.tar.gz"):
        try:
            mtime = dt.datetime.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                f.unlink()
                dropped += 1
            except OSError as e:
                logger.warning("[backup] failed to prune %s: %s", f, e)
    if dropped:
        logger.info("[backup] Pruned %d archives older than %d days", dropped, RETENTION_DAYS)
    return dropped


def _free_disk_gb() -> float:
    try:
        return shutil.disk_usage(_backup_dir()).free / (1024 ** 3)
    except OSError:
        return -1.0


def _seconds_until_next_run() -> float:
    now = dt.datetime.now()
    today_run = now.replace(hour=BACKUP_HOUR_LOCAL, minute=0, second=0, microsecond=0)
    next_run = today_run if today_run > now else today_run + dt.timedelta(days=1)
    return max(60.0, (next_run - now).total_seconds())


def run_loop() -> None:
    """Entry point invoked by cron_supervisor."""
    logger.info("[backup] cron started; retention=%dd, target=%02d:00 local",
                RETENTION_DAYS, BACKUP_HOUR_LOCAL)
    while True:
        sleep_sec = _seconds_until_next_run()
        logger.info("[backup] next run in %.0fs", sleep_sec)
        time.sleep(sleep_sec)
        try:
            free_gb = _free_disk_gb()
            if 0 < free_gb < 2:
                logger.warning("[backup] only %.1f GB free; skipping this run", free_gb)
                continue
            make_backup()
            prune_old()
        except Exception:
            logger.exception("[backup] run failed; will try again tomorrow")


if __name__ == "__main__":
    # Allow `python backup_state.py` for one-off manual backups.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    path = make_backup()
    prune_old()
    if path is None:
        raise SystemExit(1)
