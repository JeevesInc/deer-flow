"""Block outbound attachments that still contain unfilled placeholders.

Why this exists
---------------
2026-09-14, thread 06e1ac72: the August Anexo J (a *payment instruction letter*
to Banco Monex, the F/12340 trustee) was sent to the counterparty with the whole
economic waterfall still reading ``MXN$[PENDIENTE]`` — clauses (i) coberturas,
(iii) CxC, (iv) Administrador, (v) intereses and (x) remanente were all blank.
Only the ISR line carried a number. Every input needed to fill them was already
downloaded in the same workspace.

The instruction that authorised the send ("just send, don't ask for follow up")
was about a follow-up *question in the body*. It was taken as authority to ship
the document as-is. No rule caught it, because no rule looked at the attachment.

This gate looks at the attachment. A document in the instruction-letter class
cannot leave with an unfilled amount in it — that is a mechanical check on a
literal string, not a judgement call, so it belongs in the tool and not in a
prompt that a turn can talk itself out of.

Deliberate bypass (blank templates, drafts a counterparty asked to fill in):

    GMAIL_ATTACH_GATE=off python gmail_tool.py draft ...
"""

import os
import re
import sys
import zipfile
from html import unescape
from io import BytesIO

# Placeholder shapes seen in real artifacts. Deliberately anchored on brackets,
# braces and angle brackets so ordinary prose cannot trip them: "pendiente de
# confirmacion" in a sentence is fine, "MXN$[PENDIENTE]" in an amount is not.
_PATTERNS = [
    re.compile(r"\[\s*(?:PENDIENTE|PENDING|POR\s+CONFIRMAR|CONFIRM(?:AR|ED|ATION)?|NEEDS?\s+CONFIRMATION|TBD|TBC|XX+|_+|\.{2,}|\?+)\s*\]", re.I),
    re.compile(r"\[\s*\]"),                      # empty brackets
    re.compile(r"\{\{[^}]{0,60}\}\}"),           # {{mustache}}
    re.compile(r"<<[^>]{0,60}>>"),               # <<angle placeholder>>
    re.compile(r"\$\s*\[[^\]]{0,40}\]"),         # MXN$[...] / USD$[ ]
]

# Bare-word rules. Applied to prose documents only: a workbook of live deal data
# legitimately carries "TBD" in a comment column, and blocking the daily
# borrowing-base form over it would only train the gate to be switched off.
_BARE_PATTERNS = [
    re.compile(r"\bTB[DC]\b"),                   # bare TBD / TBC
    re.compile(r"\bPLACEHOLDER\b", re.I),
    re.compile(r"\bLOREM IPSUM\b", re.I),
]

# Only formats we can read text out of with no third-party dependency.
_TEXT_SUFFIXES = (".txt", ".md", ".csv")
_DOCX_SUFFIXES = (".docx", ".dotx")
_XLSX_SUFFIXES = (".xlsx", ".xlsm", ".xltx")

_TAG = re.compile(r"<[^>]+>")
_PARA_BREAK = re.compile(r"</w:p>", re.I)


class AttachmentGateError(Exception):
    """An attachment still carries unfilled placeholders."""


def _docx_text(content: bytes) -> str:
    """Extract visible text from a .docx without python-docx.

    Uses zipfile + tag-stripping so the gate has zero import risk: if the gate
    itself failed to import, it would silently stop gating.
    """
    parts = []
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            names = [
                n for n in zf.namelist()
                if n == "word/document.xml"
                or re.fullmatch(r"word/(header|footer)\d*\.xml", n)
            ]
            for name in names:
                xml = zf.read(name).decode("utf-8", errors="replace")
                # Keep paragraph boundaries so findings can be reported per line.
                xml = _PARA_BREAK.sub("\n", xml)
                parts.append(unescape(_TAG.sub("", xml)))
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    return "\n".join(parts)


def _xlsx_text(content: bytes) -> str:
    """Extract visible cell text from a .xlsx without openpyxl.

    Added 2026-09-22 after a CIM-facing draft attached a wind-down workbook whose
    assumption cells still read "[CONFIRM]": the gate only read .docx, so it
    passed. Reads xl/sharedStrings.xml (every distinct string in the workbook)
    plus each sheet's inline strings, one cell per line.
    """
    parts = []
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            names = [
                n for n in zf.namelist()
                if n == "xl/sharedStrings.xml"
                or re.fullmatch(r"xl/worksheets/sheet\d*\.xml", n)
                or re.fullmatch(r"xl/comments\d*\.xml", n)
            ]
            for name in names:
                xml = zf.read(name).decode("utf-8", errors="replace")
                # Keep cell boundaries so findings are reported per cell.
                xml = re.sub(r"</(?:si|c|t)>", "\n", xml)
                parts.append(unescape(_TAG.sub("", xml)))
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    return "\n".join(parts)


def _extract_text(filename: str, content) -> str:
    lower = filename.lower()
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    if not isinstance(content, (bytes, bytearray)):
        return ""
    if lower.endswith(_DOCX_SUFFIXES):
        return _docx_text(bytes(content))
    if lower.endswith(_XLSX_SUFFIXES):
        return _xlsx_text(bytes(content))
    if lower.endswith(_TEXT_SUFFIXES):
        return bytes(content).decode("utf-8", errors="replace")
    return ""


def find_placeholders(filename: str, content) -> list[tuple[str, str]]:
    """Return [(matched_token, containing_line), ...] for a single attachment."""
    text = _extract_text(filename, content)
    if not text:
        return []

    patterns = list(_PATTERNS)
    if not filename.lower().endswith(_XLSX_SUFFIXES):
        patterns += _BARE_PATTERNS

    findings: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue

        # Several patterns legitimately match the same defect — "MXN$[PENDIENTE]"
        # trips both the bracket rule and the currency rule. Report the widest
        # match per span so the agent sees one finding per actual blank.
        spans: list[tuple[int, int, str]] = []
        for pattern in patterns:
            for match in pattern.finditer(line):
                spans.append((match.start(), match.end(), match.group(0)))
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))

        kept: list[tuple[int, int, str]] = []
        for start, end, token in spans:
            if any(start >= k_start and end <= k_end for k_start, k_end, _ in kept):
                continue
            kept.append((start, end, token))

        snippet = line if len(line) <= 160 else line[:157] + "..."
        for _, _, token in kept:
            key = (token.upper(), snippet)
            if key in seen:
                continue
            seen.add(key)
            findings.append((token, snippet))
    return findings


def gate_enabled() -> bool:
    return os.environ.get("GMAIL_ATTACH_GATE", "on").strip().lower() not in (
        "off", "0", "false", "no",
    )


def assert_sendable(filename: str, content) -> None:
    """Raise AttachmentGateError if this attachment is not fit to send.

    Called for every attachment on every draft/send path.
    """
    if not gate_enabled():
        return

    findings = find_placeholders(filename, content)
    if not findings:
        return

    lines = [
        f"BLOCKED: '{filename}' still contains {len(findings)} unfilled "
        f"placeholder(s). It was not attached and nothing was sent.",
        "",
    ]
    for token, snippet in findings[:12]:
        lines.append(f"  {token}  <-  {snippet}")
    if len(findings) > 12:
        lines.append(f"  ... and {len(findings) - 12} more")
    lines += [
        "",
        "Fill every one of these from a verified source before sending. A "
        "payment instruction with a blank amount is worse than a late one.",
        "If the blank document is genuinely what the recipient asked for, "
        "re-run with GMAIL_ATTACH_GATE=off.",
    ]
    raise AttachmentGateError("\n".join(lines))


def main(argv: list[str]) -> int:
    """Standalone check: python attachment_gate.py <file> [<file> ...]"""
    if len(argv) < 2:
        print("Usage: python attachment_gate.py <file> [<file> ...]", file=sys.stderr)
        return 2
    bad = 0
    for path in argv[1:]:
        try:
            with open(path, "rb") as fh:
                content = fh.read()
        except OSError as exc:
            print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
            bad += 1
            continue
        findings = find_placeholders(os.path.basename(path), content)
        if findings:
            bad += 1
            print(f"FAIL {path} — {len(findings)} placeholder(s)")
            for token, snippet in findings[:12]:
                print(f"  {token}  <-  {snippet}")
        else:
            print(f"OK   {path}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
