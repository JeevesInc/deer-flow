"""Tests for scripts/backup_state.py (P0-5 / Ops-C1/C2 regression).

Two fixes covered:
  1. Exclude checkpoints.db* (the multi-GB .bak_* copies bloated every archive
     ~10x) and _cap_markets_scratch.
  2. Fold a Qdrant snapshot into the archive — long-term memory lives in a docker
     volume outside .deer-flow/ and was never backed up.
The Qdrant call is mocked; these need no server.
"""
from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
BACKUP = BACKEND_DIR / "scripts" / "backup_state.py"


def _load():
    spec = importlib.util.spec_from_file_location("backup_state_under_test", BACKUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bs():
    return _load()


def test_excluded_matches_bak_and_scratch(bs):
    assert bs._excluded("checkpoints.db")
    assert bs._excluded("checkpoints.db.bak_1780607842")  # the multi-GB copies
    assert bs._excluded("checkpoints.db-wal")
    assert bs._excluded("_cap_markets_scratch")
    assert bs._excluded("threads")
    assert not bs._excluded("memory.json")
    assert not bs._excluded("dossiers")


def test_walk_paths_skips_excluded(bs, tmp_path):
    (tmp_path / "memory.json").write_text("{}", encoding="utf-8")
    (tmp_path / "checkpoints.db.bak_123").write_text("x" * 100, encoding="utf-8")
    scratch = tmp_path / "_cap_markets_scratch"
    scratch.mkdir()
    (scratch / "big.tmp").write_text("y", encoding="utf-8")
    dossiers = tmp_path / "dossiers"
    dossiers.mkdir()
    (dossiers / "a.json").write_text("{}", encoding="utf-8")

    names = {arc for _, arc in bs._walk_paths(tmp_path)}
    assert "memory.json" in names
    assert names & {"dossiers/a.json", "dossiers\\a.json"}  # kept (path sep varies)
    assert not any("checkpoints.db" in n for n in names)
    assert not any("_cap_markets_scratch" in n for n in names)


def test_make_backup_excludes_bak_and_includes_snapshot(bs, tmp_path, monkeypatch):
    src = tmp_path / ".deer-flow"
    src.mkdir()
    (src / "memory.json").write_text('{"v": 2}', encoding="utf-8")
    (src / "checkpoints.db.bak_999").write_text("z" * 1000, encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()

    monkeypatch.setattr(bs, "_state_dir", lambda: src)
    monkeypatch.setattr(bs, "_backup_dir", lambda: backups)

    # Fake a Qdrant snapshot file instead of hitting a server.
    def fake_snap(dest_dir: Path):
        p = dest_dir / "qdrant-deerflow_memories.snapshot"
        p.write_text("snapshot-bytes", encoding="utf-8")
        return p

    monkeypatch.setattr(bs, "_snapshot_qdrant", fake_snap)

    out = bs.make_backup()
    assert out is not None and out.exists()
    with tarfile.open(out, "r:gz") as tf:
        names = tf.getnames()
    assert "memory.json" in names
    assert any(n.startswith("qdrant/") for n in names)          # snapshot folded in
    assert not any("checkpoints.db" in n for n in names)         # bak excluded


def test_make_backup_survives_qdrant_down(bs, tmp_path, monkeypatch):
    src = tmp_path / ".deer-flow"
    src.mkdir()
    (src / "memory.json").write_text("{}", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(bs, "_state_dir", lambda: src)
    monkeypatch.setattr(bs, "_backup_dir", lambda: backups)
    monkeypatch.setattr(bs, "_snapshot_qdrant", lambda d: None)  # simulate Qdrant down

    out = bs.make_backup()
    assert out is not None and out.exists()  # still produces an archive
    with tarfile.open(out, "r:gz") as tf:
        assert "memory.json" in tf.getnames()


# --- off-box Drive push -------------------------------------------------------


class _Exec:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _FakeFiles:
    def __init__(self, store, list_result):
        self.store = store
        self._list_result = list_result

    def list(self, **kw):
        self.store["list"].append(kw)
        return _Exec(self._list_result)

    def create(self, **kw):
        self.store["create"].append(kw)
        return _Exec({"id": "new-file-id"})

    def delete(self, **kw):
        self.store["delete"].append(kw)
        return _Exec({})


class _FakeSvc:
    def __init__(self, store, list_result):
        self._files = _FakeFiles(store, list_result)

    def files(self):
        return self._files


def test_push_to_drive_uploads_into_folder(bs, tmp_path, monkeypatch):
    archive = tmp_path / "deerflow-state-20260101T000000.tar.gz"
    archive.write_bytes(b"fake-gzip")
    store = {"list": [], "create": [], "delete": []}
    # folder lookup returns an existing folder id
    monkeypatch.setattr(bs, "_drive_service", lambda: _FakeSvc(store, {"files": [{"id": "FOLDER"}]}))

    assert bs.push_to_drive(archive) is True
    # one create call = the file upload, parented to the resolved folder
    up = [c for c in store["create"] if c.get("body", {}).get("parents")]
    assert up and up[0]["body"]["parents"] == ["FOLDER"]
    assert up[0]["body"]["name"] == archive.name


def test_push_to_drive_is_best_effort_on_failure(bs, tmp_path, monkeypatch):
    archive = tmp_path / "deerflow-state-x.tar.gz"
    archive.write_bytes(b"x")

    def boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(bs, "_drive_service", boom)
    assert bs.push_to_drive(archive) is False  # returns, does not raise
