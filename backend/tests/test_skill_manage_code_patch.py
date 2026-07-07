"""skill_manage.py code self-editing (patch/read --file, files command).

The self-improvement loop was previously limited to SKILL.md — the agent could
evolve its guidance but not its own code (dossier_cron.py etc.). `--file`
lifts that, with guards against the failure mode that motivated the limit:
shell-mangled self-patches corrupting files (Skills-H4).

Guards under test:
  1. --file is confined to the skill's own directory (no absolute, no ..).
  2. --old must match exactly once in code files.
  3. .py / .json results are syntax-validated; failures roll back (no write).
  4. A backup of the original lands in memory/code-backups/<skill>/.
  5. The markdown fake-newline sanitizer is NOT applied to code files.
  6. Default (no --file) behavior still targets SKILL.md unchanged.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "skills" / "public" / "self-improving-agent" / "scripts" / "skill_manage.py"
)

spec = importlib.util.spec_from_file_location("skill_manage", SCRIPT)
skill_manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill_manage)


GOOD_CODE = "def hello():\n    return 'world'\n\n\nVALUE = 1\n"


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    """A minimal skills tree with one custom skill containing code files."""
    root = tmp_path / "skills"
    skill = root / "custom" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo-skill\n---\n\n# Demo\n\nold guidance\n", encoding="utf-8")
    (skill / "cron.py").write_text(GOOD_CODE, encoding="utf-8")
    (skill / "config.json").write_text('{"key": "value"}\n', encoding="utf-8")
    (skill / "scripts").mkdir()
    (skill / "scripts" / "helper.py").write_text("X = 2\n", encoding="utf-8")
    # memory root lives under public/self-improving-agent
    (root / "public" / "self-improving-agent" / "memory").mkdir(parents=True)
    monkeypatch.setenv("SKILLS_PATH", str(root))
    return root


def _patch_args(**kw):
    defaults = {"skill_name": "demo-skill", "old": "", "new": "", "file": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_patch_file_rewrites_code(skills_root, capsys):
    skill_manage.cmd_patch(_patch_args(file="cron.py", old="VALUE = 1", new="VALUE = 42"))
    content = (skills_root / "custom" / "demo-skill" / "cron.py").read_text(encoding="utf-8")
    assert "VALUE = 42" in content
    assert "Patched demo-skill/cron.py" in capsys.readouterr().out


def test_patch_file_in_subdirectory(skills_root):
    skill_manage.cmd_patch(_patch_args(file="scripts/helper.py", old="X = 2", new="X = 3"))
    assert "X = 3" in (skills_root / "custom" / "demo-skill" / "scripts" / "helper.py").read_text(encoding="utf-8")


def test_patch_file_creates_backup(skills_root):
    skill_manage.cmd_patch(_patch_args(file="cron.py", old="VALUE = 1", new="VALUE = 42"))
    backups = list((skills_root / "public" / "self-improving-agent" / "memory" / "code-backups" / "demo-skill").iterdir())
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == GOOD_CODE  # pre-patch content


def test_patch_py_syntax_error_rolls_back(skills_root):
    with pytest.raises(SystemExit):
        skill_manage.cmd_patch(_patch_args(file="cron.py", old="VALUE = 1", new="VALUE = = broken"))
    # untouched, and no backup left behind for a rejected patch
    assert (skills_root / "custom" / "demo-skill" / "cron.py").read_text(encoding="utf-8") == GOOD_CODE
    assert not (skills_root / "public" / "self-improving-agent" / "memory" / "code-backups").exists()


def test_patch_json_validation(skills_root):
    with pytest.raises(SystemExit):
        skill_manage.cmd_patch(_patch_args(file="config.json", old='"value"', new='"value'))
    assert json.loads((skills_root / "custom" / "demo-skill" / "config.json").read_text(encoding="utf-8"))


def test_patch_file_requires_unique_match(skills_root):
    path = skills_root / "custom" / "demo-skill" / "cron.py"
    path.write_text("A = 1\nA = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        skill_manage.cmd_patch(_patch_args(file="cron.py", old="A = 1", new="A = 2"))
    assert path.read_text(encoding="utf-8") == "A = 1\nA = 1\n"


def test_patch_file_old_not_found(skills_root):
    with pytest.raises(SystemExit):
        skill_manage.cmd_patch(_patch_args(file="cron.py", old="NOT THERE", new="x"))


def test_patch_file_rejects_traversal(skills_root):
    (skills_root / "custom" / "evil.py").write_text("BAD = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        skill_manage.cmd_patch(_patch_args(file="../evil.py", old="BAD = 1", new="BAD = 2"))
    assert (skills_root / "custom" / "evil.py").read_text(encoding="utf-8") == "BAD = 1\n"


def test_patch_file_rejects_absolute_path(skills_root, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("Y = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        skill_manage.cmd_patch(_patch_args(file=str(outside), old="Y = 1", new="Y = 2"))
    assert outside.read_text(encoding="utf-8") == "Y = 1\n"


def test_patch_file_missing_file(skills_root):
    with pytest.raises(SystemExit):
        skill_manage.cmd_patch(_patch_args(file="nope.py", old="a", new="b"))


def test_code_patch_skips_markdown_sanitizer(skills_root):
    """Literal \\n in code (e.g. '\\n'.join) must survive a code patch verbatim."""
    new = "SEP = '\\n- item'"
    skill_manage.cmd_patch(_patch_args(file="cron.py", old="VALUE = 1", new=new))
    assert new in (skills_root / "custom" / "demo-skill" / "cron.py").read_text(encoding="utf-8")


def test_default_patch_still_targets_skill_md(skills_root):
    skill_manage.cmd_patch(_patch_args(old="old guidance", new="new guidance"))
    assert "new guidance" in (skills_root / "custom" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert GOOD_CODE == (skills_root / "custom" / "demo-skill" / "cron.py").read_text(encoding="utf-8")


def test_read_file(skills_root, capsys):
    skill_manage.cmd_read(SimpleNamespace(skill_name="demo-skill", file="cron.py"))
    assert "def hello" in capsys.readouterr().out


def test_files_lists_skill_contents(skills_root, capsys):
    (skills_root / "custom" / "demo-skill" / "__pycache__").mkdir()
    (skills_root / "custom" / "demo-skill" / "__pycache__" / "cron.cpython-312.pyc").write_bytes(b"junk")
    skill_manage.cmd_files(SimpleNamespace(skill_name="demo-skill"))
    out = capsys.readouterr().out
    assert "cron.py" in out and "scripts/helper.py" in out and "SKILL.md" in out
    assert "__pycache__" not in out
