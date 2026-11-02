"""Regression tests for the Gemini meeting-notes Gmail search.

Live-verified 2026-07-02: Gemini notes arrive from gemini-notes@google.com
with subject `Notes: “Meeting name” Jul 1, 2026`. The crons searched
from:meet-recordings-noreply@google.com + subject:"Notes from" — that sender
sent exactly ONE email ever (a 2024 recording notification, never notes), so
the EOD and dreams reviews continuously found zero meeting notes.

The dossier Drive path (name contains 'Notes by Gemini') was already correct
and stays untouched.
"""
from __future__ import annotations

from pathlib import Path

GMAIL_SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "custom" / "gmail"


def _src(name: str) -> str:
    return (GMAIL_SKILL / name).read_text(encoding="utf-8")


def test_eod_searches_the_real_notes_sender():
    src = _src("eod_review_cron.py")
    assert "from:gemini-notes@google.com" in src
    assert 'from:meet-recordings-noreply@google.com after:" + today_short' not in src


def test_dreams_searches_the_real_notes_sender():
    src = _src("dreams_cron.py")
    assert "from:gemini-notes@google.com" in src
    assert "from:meet-recordings-noreply@google.com after:{lookback_date}" not in src


def test_dead_subject_phrase_not_used_as_a_query():
    # subject:"Notes from your meeting" matches nothing — the real subject is
    # `Notes: “...”`. It may appear in prose warning AGAINST it, but never as
    # the "Also try" search instruction.
    for name in ("eod_review_cron.py", "dreams_cron.py"):
        src = _src(name)
        assert "Also try: subject:'Notes from your meeting'" not in src
        assert 'Also try the Gmail search: `subject:"Notes from your meeting"' not in src


def test_dossier_drive_query_untouched():
    dossier = (GMAIL_SKILL.parent / "jeeves-dossier" / "dossier_tool.py").read_text(encoding="utf-8")
    assert "name contains 'Notes by Gemini'" in dossier  # this path was always correct
