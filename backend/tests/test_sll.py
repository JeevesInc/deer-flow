"""Tests for the Synthetic Limbic Layer (SLL) skill."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the SLL skill directory to sys.path so we can import its modules.
_SLL_DIR = Path(__file__).resolve().parents[2] / "skills" / "custom" / "sll"
sys.path.insert(0, str(_SLL_DIR))


@pytest.fixture
def sll_env(tmp_path, monkeypatch):
    """Redirect SLL storage paths to a temp directory."""
    import storage  # noqa: WPS433

    fake_dir = tmp_path / "sll"
    monkeypatch.setattr(storage, "SLL_DIR", fake_dir)
    monkeypatch.setattr(storage, "PENDING_PATH", fake_dir / "pending.json")
    monkeypatch.setattr(storage, "LOG_PATH", fake_dir / "log.jsonl")
    monkeypatch.setattr(storage, "LESSONS_PATH", fake_dir / "lessons.jsonl")
    monkeypatch.setattr(storage, "RETRIEVALS_PATH", fake_dir / "retrievals.jsonl")
    return storage


# ---------------------------------------------------------------------------
# storage.py
# ---------------------------------------------------------------------------


def test_storage_roundtrip_pending(sll_env):
    assert sll_env.read_pending() is None
    sll_env.write_pending({"hello": "world"})
    assert sll_env.read_pending() == {"hello": "world"}
    sll_env.clear_pending()
    assert sll_env.read_pending() is None


def test_storage_jsonl_append_and_read(sll_env):
    p = sll_env.LOG_PATH
    sll_env.append_jsonl(p, {"a": 1})
    sll_env.append_jsonl(p, {"b": 2})
    assert sll_env.read_jsonl(p) == [{"a": 1}, {"b": 2}]


def test_storage_jsonl_rewrite(sll_env):
    p = sll_env.LESSONS_PATH
    sll_env.append_jsonl(p, {"x": 1})
    sll_env.write_jsonl(p, [{"y": 2}])
    assert sll_env.read_jsonl(p) == [{"y": 2}]


def test_storage_truncate(sll_env):
    assert sll_env.truncate("short", 100) == "short"
    out = sll_env.truncate("x" * 100, 10)
    assert out.endswith("...") and len(out) <= 13


def test_add_lesson_assigns_id_and_persists(sll_env):
    lid = sll_env.add_lesson({"text": "[!] AVOID x"})
    assert lid
    all_l = sll_env.all_lessons()
    assert len(all_l) == 1
    assert all_l[0]["id"] == lid
    assert all_l[0]["text"] == "[!] AVOID x"


def test_delete_lessons_by_id_removes_only_matched(sll_env):
    a = sll_env.add_lesson({"text": "a"})
    b = sll_env.add_lesson({"text": "b"})
    removed = sll_env.delete_lessons_by_id({a})
    assert removed == 1
    remaining = sll_env.all_lessons()
    assert len(remaining) == 1
    assert remaining[0]["id"] == b


def test_update_lesson_retrieval_bumps_counts(sll_env):
    a = sll_env.add_lesson({"text": "a", "retrieval_count": 0, "last_retrieved_at": None})
    sll_env.update_lesson_retrieval({a})
    sll_env.update_lesson_retrieval({a})
    entry = sll_env.all_lessons()[0]
    assert entry["retrieval_count"] == 2
    assert entry["last_retrieved_at"] is not None


# ---------------------------------------------------------------------------
# sll_score.py
# ---------------------------------------------------------------------------


def test_end_of_turn_writes_pending(sll_env):
    import sll_score  # noqa: WPS433

    with patch.object(sll_score, "extract_lesson", return_value="[!] AVOID stuff"):
        args = MagicMock(
            apply_sentiment=False,
            user_reply="",
            task="t",
            response="r",
            verbose=False,
        )
        rc = sll_score.mode_end_of_turn(args)

    assert rc == 0
    pending = sll_env.read_pending()
    assert pending is not None
    assert pending["task"] == "t"
    assert pending["response"] == "r"
    assert pending["composite"] == sll_score.DEFAULT_COMPOSITE


def test_end_of_turn_with_empty_inputs_is_noop(sll_env):
    import sll_score  # noqa: WPS433

    args = MagicMock(
        apply_sentiment=False, user_reply="", task="", response="", verbose=False
    )
    rc = sll_score.mode_end_of_turn(args)
    assert rc == 0
    assert sll_env.read_pending() is None


def test_apply_sentiment_no_pending_is_noop(sll_env):
    import sll_score  # noqa: WPS433

    args = MagicMock(apply_sentiment=True, user_reply="ok", verbose=False, task="", response="")
    rc = sll_score.mode_apply_sentiment(args)
    assert rc == 0
    assert sll_env.read_jsonl(sll_env.LOG_PATH) == []


def test_apply_sentiment_explicit_correction_stores_failure_lesson(sll_env):
    import sll_score  # noqa: WPS433

    sll_env.write_pending({
        "task": "compute X",
        "response": "X = 5",
        "composite": 0.6,
        "candidate_failure_lesson": "[!] AVOID guessing",
        "candidate_success_lesson": "[+] DO double-check",
        "scored_at": "2026-06-02T00:00:00+00:00",
    })

    with patch.object(sll_score, "classify_sentiment", return_value="explicit_correction"):
        args = MagicMock(apply_sentiment=True, user_reply="no wrong", verbose=False)
        rc = sll_score.mode_apply_sentiment(args)

    assert rc == 0
    lessons = sll_env.all_lessons()
    assert len(lessons) == 1
    assert lessons[0]["text"] == "[!] AVOID guessing"
    assert lessons[0]["outcome"] == "failure"
    assert lessons[0]["boost"] == 2.5
    assert lessons[0]["composite"] == 0.1
    log = sll_env.read_jsonl(sll_env.LOG_PATH)
    assert len(log) == 1
    assert log[0]["sentiment"] == "explicit_correction"
    assert log[0]["final_composite"] == 0.1
    assert log[0]["outcome"] == "failure"
    assert log[0]["lesson_id"] == lessons[0]["id"]
    assert sll_env.read_pending() is None


def test_apply_sentiment_explicit_praise_stores_success_lesson(sll_env):
    import sll_score  # noqa: WPS433

    sll_env.write_pending({
        "task": "compute X",
        "response": "X = 5",
        "composite": 0.6,
        "candidate_failure_lesson": "[!] AVOID guessing",
        "candidate_success_lesson": "[+] DO double-check",
        "scored_at": "2026-06-02T00:00:00+00:00",
    })

    with patch.object(sll_score, "classify_sentiment", return_value="explicit_praise"):
        args = MagicMock(apply_sentiment=True, user_reply="perfect", verbose=False)
        sll_score.mode_apply_sentiment(args)

    lessons = sll_env.all_lessons()
    assert lessons[0]["outcome"] == "success"
    assert lessons[0]["boost"] == 2.0
    assert lessons[0]["composite"] == 0.95
    assert lessons[0]["text"] == "[+] DO double-check"


def test_apply_sentiment_implicit_negative_stores_failure(sll_env):
    import sll_score  # noqa: WPS433

    sll_env.write_pending({
        "task": "compute X",
        "response": "X = 5",
        "composite": 0.6,
        "candidate_failure_lesson": "[!] AVOID handwaving",
        "candidate_success_lesson": "[+] DO be precise",
        "scored_at": "2026-06-02T00:00:00+00:00",
    })

    with patch.object(sll_score, "classify_sentiment", return_value="implicit_negative"):
        args = MagicMock(apply_sentiment=True, user_reply="ok", verbose=False)
        sll_score.mode_apply_sentiment(args)

    lessons = sll_env.all_lessons()
    assert lessons[0]["outcome"] == "failure"
    # 0.6 + (-0.40) implicit_negative penalty = 0.20 (penalty was re-tuned 0.35→0.40)
    assert abs(lessons[0]["composite"] - 0.20) < 1e-9


def test_apply_sentiment_implicit_positive_stores_success(sll_env):
    import sll_score  # noqa: WPS433

    sll_env.write_pending({
        "task": "compute X",
        "response": "X = 5",
        "composite": 0.6,
        "candidate_failure_lesson": "[!] AVOID handwaving",
        "candidate_success_lesson": "[+] DO be precise",
        "scored_at": "2026-06-02T00:00:00+00:00",
    })

    with patch.object(sll_score, "classify_sentiment", return_value="implicit_positive"):
        args = MagicMock(apply_sentiment=True, user_reply="ok now do Y", verbose=False)
        sll_score.mode_apply_sentiment(args)

    # Re-tuned: boost 0.15→0.25 and SUCCESS_THRESHOLD 0.80→0.72, so
    # 0.6 + 0.25 = 0.85 ≥ 0.72 → success (building-on-output now reinforces).
    lessons = sll_env.all_lessons()
    assert len(lessons) == 1
    assert lessons[0]["outcome"] == "success"
    assert abs(lessons[0]["composite"] - 0.85) < 1e-9
    log = sll_env.read_jsonl(sll_env.LOG_PATH)
    assert log[0]["outcome"] == "success"
    assert log[0]["lesson_stored"] == "[+] DO be precise"


def test_apply_sentiment_clamps_to_range(sll_env):
    import sll_score  # noqa: WPS433

    sll_env.write_pending({
        "task": "t",
        "response": "r",
        "composite": 1.5,  # invalid, but defensive
        "candidate_failure_lesson": None,
        "candidate_success_lesson": None,
        "scored_at": "2026-06-02T00:00:00+00:00",
    })

    with patch.object(sll_score, "classify_sentiment", return_value="implicit_positive"):
        args = MagicMock(apply_sentiment=True, user_reply="cool", verbose=False)
        sll_score.mode_apply_sentiment(args)

    log = sll_env.read_jsonl(sll_env.LOG_PATH)
    assert 0.0 <= log[0]["final_composite"] <= 1.0


# ---------------------------------------------------------------------------
# sll_inject.py
# ---------------------------------------------------------------------------


def test_inject_no_lessons_returns_empty(sll_env, capsys):
    import sll_inject  # noqa: WPS433

    rc = sll_inject.main(["--task", "do something"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_inject_returns_all_when_below_rank_threshold(sll_env, capsys):
    import sll_inject  # noqa: WPS433

    sll_env.add_lesson({"text": "[!] AVOID one"})
    sll_env.add_lesson({"text": "[+] DO two"})

    rc = sll_inject.main(["--task", "anything"])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0
    assert out == ["[!] AVOID one", "[+] DO two"]


def test_inject_normalizes_missing_prefix(sll_env, capsys):
    import sll_inject  # noqa: WPS433

    sll_env.add_lesson({"text": "AVOID guessing dates"})
    sll_env.add_lesson({"text": "DO check schemas"})

    sll_inject.main(["--task", "x"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0].startswith("[!] AVOID")
    assert out[1].startswith("[+] DO")


def test_inject_uses_ranker_when_above_threshold(sll_env, capsys):
    import sll_inject  # noqa: WPS433

    for i in range(5):
        sll_env.add_lesson({"text": f"[+] DO lesson {i}"})

    with patch.object(sll_inject, "rank_with_haiku", return_value=[2, 0]):
        sll_inject.main(["--task", "x", "--top-k", "2"])

    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["[+] DO lesson 2", "[+] DO lesson 0"]


def test_inject_logs_retrieval_and_bumps_counts(sll_env):
    import sll_inject  # noqa: WPS433

    lid = sll_env.add_lesson({
        "text": "[+] DO things",
        "retrieval_count": 0,
        "last_retrieved_at": None,
    })

    sll_inject.main(["--task", "do things"])

    meta = sll_env.all_lessons()
    assert meta[0]["retrieval_count"] == 1
    assert meta[0]["last_retrieved_at"] is not None
    retrievals = sll_env.read_jsonl(sll_env.RETRIEVALS_PATH)
    assert len(retrievals) == 1
    assert retrievals[0]["lessons"][0]["id"] == lid


def test_rank_with_haiku_handles_code_fence_response(sll_env):
    import sll_inject  # noqa: WPS433

    lessons = [{"text": f"l{i}"} for i in range(5)]
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(text='```json\n{"indices": [3, 1]}\n```')]
    fake_client.messages.create.return_value = fake_resp
    with patch.object(sll_inject.storage, "anthropic_client", return_value=fake_client):
        result = sll_inject.rank_with_haiku("task", lessons, 2)
    assert result == [3, 1]


def test_rank_with_haiku_fallback_when_no_client(sll_env):
    import sll_inject  # noqa: WPS433

    lessons = [{"text": f"l{i}"} for i in range(5)]
    with patch.object(sll_inject.storage, "anthropic_client", return_value=None):
        result = sll_inject.rank_with_haiku("task", lessons, 2)
    assert result == [0, 1]


def test_rank_with_haiku_filters_invalid_indices(sll_env):
    import sll_inject  # noqa: WPS433

    lessons = [{"text": f"l{i}"} for i in range(3)]
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(text='{"indices": [0, 99, 2]}')]
    fake_client.messages.create.return_value = fake_resp
    with patch.object(sll_inject.storage, "anthropic_client", return_value=fake_client):
        result = sll_inject.rank_with_haiku("task", lessons, 5)
    assert result == [0, 2]


# ---------------------------------------------------------------------------
# sll_dashboard.py
# ---------------------------------------------------------------------------


def test_dashboard_full_runs_on_empty_state(sll_env, capsys):
    import sll_dashboard  # noqa: WPS433

    rc = sll_dashboard.main(["--full"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SLL DASHBOARD" in out


def test_dashboard_prune_dry_run_does_not_delete(sll_env):
    import sll_dashboard  # noqa: WPS433

    sll_env.add_lesson({
        "text": "[!] AVOID stale",
        "created_at": "2020-01-01T00:00:00+00:00",
        "retrieval_count": 0,
    })

    rc = sll_dashboard.main(["--prune", "--dry-run"])
    assert rc == 0
    assert len(sll_env.all_lessons()) == 1


def test_dashboard_prune_drops_stale_and_keeps_recent(sll_env):
    import sll_dashboard  # noqa: WPS433

    sll_env.add_lesson({
        "text": "[!] AVOID stale",
        "created_at": "2020-01-01T00:00:00+00:00",
        "retrieval_count": 0,
    })
    sll_env.add_lesson({
        "text": "[+] DO fresh",
        "created_at": sll_env.now_iso(),
        "retrieval_count": 0,
    })

    rc = sll_dashboard.main(["--prune"])
    assert rc == 0
    remaining = sll_env.all_lessons()
    texts = [e["text"] for e in remaining]
    assert texts == ["[+] DO fresh"]
