"""Tests for the classifier eval harness (skills/custom/jeeves-eval).

An eval harness is the one piece of code where a silent bug is maximally
expensive: it reports green while the thing it measures regresses, and it is
trusted precisely because nobody re-derives its numbers by hand. So the scoring
arithmetic, the label policy, and the state file get real coverage.

No API key or network needed — the classifier and judge are both stubbed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
EVAL_DIR = BACKEND_DIR.parent / "skills" / "custom" / "jeeves-eval"


def _load(name: str):
    """Load an eval module by path — they are skill scripts, not a package."""
    for extra in (EVAL_DIR, BACKEND_DIR.parent / "skills" / "custom" / "_shared"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", EVAL_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def runner():
    return _load("run_eval")


@pytest.fixture()
def builder():
    return _load("build_golden_set")


def _row(expected: bool, predicted: bool, action_score=None):
    return {
        "expected_actionable": expected,
        "predicted_actionable": predicted,
        "correct": expected == predicted,
        "action_score": action_score,
        "latency_ms": 100.0,
    }


# -- scoring arithmetic -------------------------------------------------------


def test_perfect_scores(runner):
    rows = [_row(True, True), _row(True, True), _row(False, False), _row(False, False)]
    m = runner.compute_metrics(rows)
    assert (m["accuracy"], m["precision"], m["recall"], m["f1"]) == (1.0, 1.0, 1.0, 1.0)
    assert (m["true_positives"], m["true_negatives"]) == (2, 2)


def test_confusion_cells_are_assigned_correctly(runner):
    rows = [
        _row(True, True),    # tp
        _row(False, True),   # fp
        _row(True, False),   # fn
        _row(False, False),  # tn
    ]
    m = runner.compute_metrics(rows)
    assert m["true_positives"] == 1
    assert m["false_positives"] == 1
    assert m["false_negatives"] == 1
    assert m["true_negatives"] == 1
    assert m["accuracy"] == 0.5
    assert m["precision"] == 0.5
    assert m["recall"] == 0.5


def test_precision_and_recall_are_not_transposed(runner):
    """Two false negatives and no false positives must read as perfect
    precision and poor recall — swapping them would invert every conclusion
    drawn from the dashboard."""
    rows = [_row(True, True), _row(True, False), _row(True, False), _row(False, False)]
    m = runner.compute_metrics(rows)
    assert m["precision"] == 1.0
    assert m["recall"] == pytest.approx(1 / 3, abs=1e-4)


def test_no_predicted_positives_does_not_divide_by_zero(runner):
    m = runner.compute_metrics([_row(True, False), _row(False, False)])
    assert m["precision"] == 0.0
    assert m["f1"] == 0.0


def test_action_score_ignores_unjudged_cases(runner):
    """None means "no ground truth to judge against", not zero. Averaging it in
    as 0 would drag the mean down for cases the judge deliberately skipped."""
    rows = [_row(True, True, 2), _row(True, True, None), _row(True, True, 1)]
    m = runner.compute_metrics(rows)
    assert m["action_score_mean"] == 1.5
    assert m["action_score_n"] == 2


def test_action_score_is_none_when_nothing_judged(runner):
    m = runner.compute_metrics([_row(True, True, None)])
    assert m["action_score_mean"] is None


# -- state file ---------------------------------------------------------------


def test_state_roundtrip_and_history_cap(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "_eval_state.json")
    monkeypatch.setattr(runner, "MAX_HISTORY", 3)
    for i in range(5):
        runner.write_state({"accuracy": i / 10}, f"run-{i}", {"size": 60, "built_at": "x"})
    state = json.loads((tmp_path / "_eval_state.json").read_text(encoding="utf-8"))
    assert len(state["runs"]) == 3, "history must be capped, not grow forever"
    assert state["latest"]["label"] == "run-4"
    assert state["runs"][-1]["label"] == "run-4"


def test_corrupt_state_does_not_crash_the_run(runner, tmp_path, monkeypatch):
    path = tmp_path / "_eval_state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(runner, "STATE_PATH", path)
    runner.write_state({"accuracy": 0.9}, "recovered", {"size": 1, "built_at": "x"})
    assert json.loads(path.read_text(encoding="utf-8"))["latest"]["label"] == "recovered"


def test_delta_line_reports_previous_run(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "_eval_state.json")
    runner.write_state({"accuracy": 0.5, "precision": 0.5, "recall": 0.5, "f1": 0.5},
                       "old", {"size": 60, "built_at": "x"})
    line = runner._delta_line({"accuracy": 0.6, "precision": 0.5, "recall": 0.4, "f1": 0.5})
    assert "accuracy +0.100" in line
    assert "recall -0.100" in line


def test_delta_line_handles_first_ever_run(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "_eval_state.json")
    assert "no previous run" in runner._delta_line({"accuracy": 0.9})


# -- label policy -------------------------------------------------------------


def test_ignored_and_unknown_are_never_labels(builder):
    """The whole set is only as good as its weakest label."""
    assert "ignored" in builder.EXCLUDED_OUTCOMES
    assert "unknown" in builder.EXCLUDED_OUTCOMES
    assert not (builder.EXCLUDED_OUTCOMES & builder.POSITIVE_OUTCOMES)
    assert not (builder.EXCLUDED_OUTCOMES & builder.NEGATIVE_OUTCOMES)


def test_positive_and_negative_outcomes_are_disjoint(builder):
    assert not (builder.POSITIVE_OUTCOMES & builder.NEGATIVE_OUTCOMES)


def test_thread_progressed_is_dropped_not_scored(builder, monkeypatch):
    """A colleague replying is not evidence Brian had to act. Scoring these as
    positives produced a wave of bogus false negatives on the first eval run."""
    proposals = [{"slack_ts": "1", "gmail_msg_id": "g1", "sender_domain": "tryjeeves.com"},
                 {"slack_ts": "2", "gmail_msg_id": "g2", "sender_domain": "cim-llc.com"}]
    outcomes = [
        {"proposal_slack_ts": "1", "outcome": "handled_elsewhere", "gmail_disposition": "thread_progressed"},
        {"proposal_slack_ts": "2", "outcome": "handled_elsewhere", "gmail_disposition": "owner_replied"},
    ]
    monkeypatch.setattr(
        builder, "_read_jsonl",
        lambda path: outcomes if "outcomes" in str(path) else proposals,
    )
    got = builder.candidates_from_proposals()
    assert [c["gmail_msg_id"] for c in got] == ["g2"]
    assert got[0]["expected_actionable"] is True


def test_rejected_and_dismissed_become_negatives(builder, monkeypatch):
    proposals = [{"slack_ts": "1", "gmail_msg_id": "g1"}, {"slack_ts": "2", "gmail_msg_id": "g2"}]
    outcomes = [
        {"proposal_slack_ts": "1", "outcome": "rejected"},
        {"proposal_slack_ts": "2", "outcome": "dismissed"},
    ]
    monkeypatch.setattr(
        builder, "_read_jsonl",
        lambda path: outcomes if "outcomes" in str(path) else proposals,
    )
    got = builder.candidates_from_proposals()
    assert len(got) == 2
    assert all(c["expected_actionable"] is False for c in got)


def test_observations_exclude_already_proposed_threads(builder, monkeypatch):
    """An email that was already surfaced cannot also serve as a true negative."""
    obs = [
        {"kind": "no_reply", "thread_id": "t1", "body": "x" * 200, "from_domain": "a.com"},
        {"kind": "no_reply", "thread_id": "t2", "body": "y" * 200, "from_domain": "b.com"},
        {"kind": "sent_email", "thread_id": "t3", "body": "z" * 200},
        {"kind": "no_reply", "thread_id": "t4", "body": "too short"},
    ]
    monkeypatch.setattr(builder, "_read_jsonl", lambda path: obs)
    got = builder.candidates_from_observations({"t1"})
    assert [c["gmail_msg_id"] for c in got] == ["t2"]
    assert got[0]["expected_actionable"] is False


# -- stratification -----------------------------------------------------------


def test_stratify_balances_classes(builder):
    cands = (
        [{"expected_actionable": True, "sender_domain": f"d{i}.com"} for i in range(50)]
        + [{"expected_actionable": False, "sender_domain": f"e{i}.com"} for i in range(50)]
    )
    picked = builder.stratify(cands, 20, seed=1)
    assert len(picked) == 20
    assert sum(1 for c in picked if c["expected_actionable"]) == 10


def test_stratify_caps_a_dominant_domain(builder):
    """Without the cap the set measures one counterparty relationship."""
    cands = (
        [{"expected_actionable": True, "sender_domain": "loud.com"} for _ in range(50)]
        + [{"expected_actionable": True, "sender_domain": f"d{i}.com"} for i in range(50)]
        + [{"expected_actionable": False, "sender_domain": f"e{i}.com"} for i in range(50)]
    )
    picked = builder.stratify(cands, 40, seed=1)
    loud = sum(1 for c in picked if c["sender_domain"] == "loud.com")
    assert loud <= max(1, int(40 * builder.MAX_SHARE_PER_DOMAIN))


def test_stratify_is_reproducible_for_a_fixed_seed(builder):
    cands = [{"expected_actionable": i % 2 == 0, "sender_domain": f"d{i}.com"} for i in range(60)]
    a = builder.stratify(cands, 20, seed=7)
    b = builder.stratify(cands, 20, seed=7)
    assert [c["sender_domain"] for c in a] == [c["sender_domain"] for c in b]


# -- judge --------------------------------------------------------------------


@pytest.fixture()
def judge():
    return _load("judge")


def test_judge_skips_when_there_is_no_proposed_action(judge):
    verdict = judge.judge_proposed_action({"owner_reply": "go ahead"}, {"proposed_action": ""})
    assert verdict["score"] is None


def test_judge_skips_when_there_is_no_ground_truth(judge):
    """Scoring without something to compare against manufactures a number."""
    case = {"owner_reply": "", "gmail_disposition": "read_no_action"}
    verdict = judge.judge_proposed_action(case, {"proposed_action": "Draft a reply"})
    assert verdict["score"] is None


def test_judge_rejects_out_of_range_scores(judge):
    class _Resp:
        content = [type("B", (), {"type": "text", "text": '{"score": 7, "reasoning": "hmm"}'})()]

    class _Client:
        class messages:
            @staticmethod
            def create(**_kw):
                return _Resp()

    case = {"owner_reply": "do it", "inputs": {}}
    verdict = judge.judge_proposed_action(case, {"proposed_action": "x"}, client=_Client())
    assert verdict["score"] is None
    assert "out-of-range" in verdict["reasoning"]


def test_judge_parses_a_fenced_response(judge):
    """Adaptive thinking puts a ThinkingBlock first and the model often fences
    its JSON — both broke naive parsing elsewhere in this codebase."""
    text = '```json\n{"score": 2, "reasoning": "matches what Brian did"}\n```'
    blocks = [
        type("B", (), {"type": "thinking", "text": ""})(),
        type("B", (), {"type": "text", "text": text})(),
    ]

    class _Client:
        class messages:
            @staticmethod
            def create(**_kw):
                return type("R", (), {"content": blocks})()

    verdict = judge.judge_proposed_action(
        {"owner_reply": "go", "inputs": {}}, {"proposed_action": "x"}, client=_Client()
    )
    assert verdict["score"] == 2


def test_judge_error_degrades_to_none(judge):
    class _Client:
        class messages:
            @staticmethod
            def create(**_kw):
                raise RuntimeError("529 overloaded")

    verdict = judge.judge_proposed_action(
        {"owner_reply": "go", "inputs": {}}, {"proposed_action": "x"}, client=_Client()
    )
    assert verdict["score"] is None
