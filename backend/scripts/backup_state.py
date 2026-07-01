"""Nightly backup of high-value DeerFlow state.

Tars the critical (small, hard-to-regenerate) pieces of .deer-flow/ into
a timestamped archive plus a fresh Qdrant snapshot (the live long-term memory,
which lives in a docker volume — NOT under .deer-flow/ — and was therefore
previously not backed up at all), and prunes archives older than RETENTION_DAYS.

Excludes:
  - checkpoints.db*                (incl. multi-GB .bak_* copies; transient, own retention)
  - threads/                       (per-thread workspaces, regenerable)
  - slack_downloads/               (cached binaries, regenerable)
  - knowledge/                     (re-crawled by knowledge-crawler cron)
  - _cap_markets_scratch/          (regenerable dashboard scratch, ~100s of MB)

Run as a cron in cron_supervisor.py (daily at 03:00 local).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
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
    "_cap_markets_scratch",
}

# Any top-level entry starting with one of these is excluded too. Catches the
# multi-GB checkpoints.db.bak_<epoch> copies that bloated every archive ~10x.
EXCLUDE_PREFIXES = ("checkpoints.db",)


def _excluded(name: str) -> bool:
    return name in EXCLUDE_NAMES or any(name.startswith(p) for p in EXCLUDE_PREFIXES)


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
    return bool(rel.parts) and _excluded(rel.parts[0])


def _walk_paths(root: Path):
    """Yield (abs_path, arcname) for every file under root, excluding top-level
    names in EXCLUDE_NAMES. Walks manually so one unreadable file doesn't abort.
    """
    for entry in root.iterdir():
        if _excluded(entry.name):
            continue
        if entry.is_file() or entry.is_symlink():
            yield entry, entry.name
            continue
        if entry.is_dir():
            for sub in entry.rglob("*"):
                if not sub.is_file():
                    continue
                yield sub, str(sub.relative_to(root))


def _snapshot_qdrant(dest_dir: Path) -> Path | None:
    """Create + download a Qdrant snapshot of the mem0 collection.

    Long-term memory (~4,400 facts) lives in the deerflow-qdrant docker volume,
    outside .deer-flow/, so the state tar never captured it — a box-disk loss
    meant losing all memory. This snapshots the collection to a file we fold
    into the archive. Server-side snapshot is deleted afterward so the volume
    doesn't accumulate copies. Best-effort: returns None if Qdrant is down.
    """
    try:
        from qdrant_client import QdrantClient

        from deerflow.agents.memory.mem0_store import MEM0_COLLECTION

        host = os.environ.get("MEM0_QDRANT_HOST", "localhost")
        port = int(os.environ.get("MEM0_QDRANT_PORT", "6333"))
        client = QdrantClient(host=host, port=port)
        snap = client.create_snapshot(collection_name=MEM0_COLLECTION)
        name = getattr(snap, "name", None) or (snap.get("name") if isinstance(snap, dict) else None)
        if not name:
            logger.warning("[backup] qdrant snapshot returned no name; skipping")
            return None
        dest = dest_dir / f"qdrant-{MEM0_COLLECTION}.snapshot"
        url = f"http://{host}:{port}/collections/{MEM0_COLLECTION}/snapshots/{name}"
        try:
            urllib.request.urlretrieve(url, dest)  # noqa: S310 (trusted localhost)
        finally:
            try:
                client.delete_snapshot(collection_name=MEM0_COLLECTION, snapshot_name=name)
            except Exception as e:  # noqa: BLE001
                logger.debug("[backup] could not delete server-side snapshot %s: %s", name, e)
        logger.info("[backup] qdrant snapshot %.1f MB", dest.stat().st_size / (1024 * 1024))
        return dest
    except Exception as e:  # noqa: BLE001 — Qdrant down / client missing must not fail the backup
        logger.warning("[backup] qdrant snapshot failed (memory not captured this run): %s", e)
        return None


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

        # Fold in a fresh Qdrant snapshot (long-term memory) under qdrant/.
        with tempfile.TemporaryDirectory() as td:
            snap = _snapshot_qdrant(Path(td))
            if snap and snap.exists():
                try:
                    tf.add(str(snap), arcname=f"qdrant/{snap.name}", recursive=False)
                    added += 1
                except (PermissionError, OSError) as e:
                    logger.warning("[backup] failed to add qdrant snapshot: %s", e)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info("[backup] Wrote %s (%.1f MB, %d files, %d skipped)", out, size_mb, added, skipped)
    if skipped:
        logger.info("[backup] Skipped %d locked file(s) — usually sqlite wal/shm or vector-store internals", skipped)
    return out


# Off-box copy to Google Drive. Secrets (.env) are intentionally NOT included —
# the state archive never contains them, and per the owner's choice credentials
# are re-entered by hand on a new box rather than egressed to Drive.
DRIVE_FOLDER_NAME = os.environ.get("DEERFLOW_BACKUP_DRIVE_FOLDER", "DeerFlow Backups")
_FOLDER_MIME = "application/vnd.google-apps.folder"


def _drive_service():
    shared = Path(__file__).resolve().parents[2] / "skills" / "custom" / "_shared"
    if shared.exists() and str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    from google_auth import get_credentials
    from googleapiclient.discovery import build

    creds = get_credentials(required=True)
    return build("drive", "v3", credentials=creds)


def _drive_folder_id(svc) -> str:
    res = svc.files().list(
        q=f"name='{DRIVE_FOLDER_NAME}' and mimeType='{_FOLDER_MIME}' and trashed=false",
        spaces="drive", fields="files(id)",
    ).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    folder = svc.files().create(
        body={"name": DRIVE_FOLDER_NAME, "mimeType": _FOLDER_MIME}, fields="id"
    ).execute()
    logger.info("[backup] created Drive folder %r", DRIVE_FOLDER_NAME)
    return folder["id"]


def _prune_drive(svc, folder_id: str) -> int:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=RETENTION_DAYS)
    res = svc.files().list(
        q=f"'{folder_id}' in parents and trashed=false and name contains 'deerflow-state-'",
        spaces="drive", fields="files(id,name,createdTime)",
    ).execute()
    dropped = 0
    for f in res.get("files", []):
        try:
            created = dt.datetime.fromisoformat(f.get("createdTime", "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if created < cutoff:
            try:
                svc.files().delete(fileId=f["id"]).execute()
                dropped += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("[backup] drive prune failed for %s: %s", f.get("name"), e)
    if dropped:
        logger.info("[backup] pruned %d Drive archive(s) older than %dd", dropped, RETENTION_DAYS)
    return dropped


def push_to_drive(archive: Path) -> bool:
    """Upload the archive to the Drive backup folder. Best-effort: a failure here
    must never fail the (already-written) local backup, but it IS logged loudly
    because a silently-broken off-box copy is the whole risk we're mitigating.
    """
    try:
        from googleapiclient.http import MediaFileUpload

        svc = _drive_service()
        folder_id = _drive_folder_id(svc)
        media = MediaFileUpload(str(archive), mimetype="application/gzip", resumable=True)
        svc.files().create(
            body={"name": archive.name, "parents": [folder_id]},
            media_body=media, fields="id",
        ).execute()
        logger.info("[backup] pushed %s to Drive/%s", archive.name, DRIVE_FOLDER_NAME)
        _prune_drive(svc, folder_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[backup] OFF-BOX Drive push FAILED (only local copy exists): %s", e)
        return False


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
            path = make_backup()
            prune_old()
            if path is not None:
                push_to_drive(path)
        except Exception:
            logger.exception("[backup] run failed; will try again tomorrow")


if __name__ == "__main__":
    # Allow `python backup_state.py` for one-off manual backups.
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="DeerFlow state backup")
    parser.add_argument("--no-drive", action="store_true", help="Local archive only; skip the off-box Google Drive push")
    args = parser.parse_args()

    path = make_backup()
    prune_old()
    if path is None:
        raise SystemExit(1)
    if not args.no_drive:
        push_to_drive(path)
