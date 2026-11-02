"""SKILL.md lint (P2-6 / Skills-H3/H4).

The agent's skill docs are its self-knowledge — drift here becomes agent
hallucination (a known class: describing disabled crons as active, pointing
at dead stores, corrupted bash blocks from unsanitized self-patches).

Rules enforced for every skills/custom/*/SKILL.md:
  1. YAML frontmatter present with a `name:` field.
  2. No literal `\\n` / `/n` fake-newline corruption (unsanitized self-patch
     writes — the jeeves-redshift incident).
  3. No hardcoded `threads/<uuid>` paths (they die with the thread).
  4. Every same-skill script referenced actually exists on disk.

Pre-existing violations are pinned in KNOWN_VIOLATIONS as a burn-down list:
fixing a file means removing its entry; NEW violations fail immediately.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_CUSTOM = Path(__file__).resolve().parent.parent.parent / "skills" / "custom"
SKILL_MDS = sorted(SKILLS_CUSTOM.glob("*/SKILL.md"))

# Burn-down list: (skill_name, rule) pairs that are known-dirty. Remove the
# entry when the file is fixed. Adding to this list requires justification.
KNOWN_VIOLATIONS: set[tuple[str, str]] = {
    # Both files carry uncommitted P1-7 strategic-context edits awaiting
    # Brian's review — fixing their example paths now would entangle that
    # review. Clean them when P1-7 lands.
    ("google-drive", "thread-uuid-path"),
    ("jeeves-diligence", "thread-uuid-path"),
}

# `/n` immediately followed by a command word = a newline eaten by a bad
# self-patch (observed: `sql/ngrep`, `cfo-org-kb/ngit`). Plain `/n` alone
# would false-positive on real paths/URLs.
_FAKE_NEWLINE = re.compile(r"/n(?=(git|grep|cat|ls |cd |uv |python)\b)")
_LITERAL_BACKSLASH_N = re.compile(r"\\n[A-Za-z#*-]")  # \n glued to following text
_THREAD_UUID = re.compile(r"threads[/\\][0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}")
_SCRIPT_REF = re.compile(r"(?:/mnt/skills/custom|skills/custom)/([\w.-]+)/([\w.-]+\.py)")


def _violations(md: Path) -> list[str]:
    text = md.read_text(encoding="utf-8", errors="replace")
    skill = md.parent.name
    problems = []

    if not text.lstrip().startswith("---") or not re.search(r"(?m)^name:\s*\S", text.split("---")[1] if text.count("---") >= 2 else ""):
        problems.append("frontmatter")

    if _FAKE_NEWLINE.search(text) or _LITERAL_BACKSLASH_N.search(text):
        problems.append("fake-newline")

    if _THREAD_UUID.search(text):
        problems.append("thread-uuid-path")

    for ref_skill, script in set(_SCRIPT_REF.findall(text)):
        if ref_skill == skill and not (SKILLS_CUSTOM / ref_skill / script).exists():
            problems.append(f"missing-script:{script}")

    return problems


def test_skill_mds_discovered():
    assert len(SKILL_MDS) >= 10, f"expected the custom skills tree, found {len(SKILL_MDS)}"


@pytest.mark.parametrize("md", SKILL_MDS, ids=[p.parent.name for p in SKILL_MDS])
def test_skill_md_lint(md):
    skill = md.parent.name
    problems = [p for p in _violations(md)
                if (skill, p.split(":")[0]) not in KNOWN_VIOLATIONS]
    assert not problems, (
        f"{skill}/SKILL.md lint violations: {problems}. "
        "Fix the doc (drifted docs become agent hallucinations) — or, only "
        "for pre-existing debt, add to KNOWN_VIOLATIONS with justification."
    )


# ---------------------------------------------------------------------------
# Self-patch sanitization (the write path that produced the corruption)
# ---------------------------------------------------------------------------

def _load_skill_manage():
    import importlib.util

    path = (SKILLS_CUSTOM.parent / "public" / "self-improving-agent"
            / "scripts" / "skill_manage.py")
    spec = importlib.util.spec_from_file_location("skill_manage_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sanitize_converts_fake_newline_before_markdown():
    sm = _load_skill_manage()
    out = sm._sanitize_patch_text(r"one canonical Sheet.\n- **Alignment is mandatory**: yes")
    assert "\\n" not in out
    assert "Sheet.\n- **Alignment" in out


def test_sanitize_leaves_code_snippets_alone():
    sm = _load_skill_manage()
    legit = r"use '\n'.join(lines) and text.split('\n') in scripts"
    assert sm._sanitize_patch_text(legit) == legit
