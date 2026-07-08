"""Repo-wide gate: no live code may use resp.content[0].text.

Adaptive-thinking models (claude-sonnet-5 / claude-opus-4-8) may lead with a
ThinkingBlock, where .text raises AttributeError. The 2026-07-07 QA sweep
found 172 failed dossier syntheses from exactly this, months after the same
bug was fixed point-wise in the proposal synthesizer and CICO (see
test_cico_gate.py). Use _shared/llm.text_from_response() instead.
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]  # deer-flow/
_SCAN = [
    _REPO / "skills" / "custom",
    _REPO / "backend" / "scripts",
    _REPO / "backend" / "webhook_receiver.py",
    _REPO / "backend" / "app",
]
# The helper's own docstring documents the forbidden pattern.
_EXEMPT = {_REPO / "skills" / "custom" / "_shared" / "llm.py"}
_PATTERN = re.compile(r"\.content\[0\]\.text")


def _offending_lines(path: Path) -> list[str]:
    hits = []
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue  # comments may reference the pattern to warn against it
        if _PATTERN.search(line):
            hits.append(f"{path.relative_to(_REPO)}:{i}")
    return hits


def test_no_content0_text_in_live_code():
    offenders = []
    for root in _SCAN:
        for f in [root] if root.is_file() else sorted(root.rglob("*.py")):
            if f in _EXEMPT:
                continue
            offenders.extend(_offending_lines(f))
    assert not offenders, (
        "resp.content[0].text found — use _shared/llm.text_from_response() "
        f"(ThinkingBlock-safe): {offenders}"
    )
