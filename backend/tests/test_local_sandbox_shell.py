"""Shell resolution for the bash tool (P3-1 fallout, 2026-07-07).

Under the start.sh supervisor LangGraph inherited git-bash's PATH, so
shutil.which("sh") found Git's sh.exe. As a Windows service only the machine
PATH is present (Git\\cmd but NOT Git\\usr\\bin) — every bash tool call died
with "No suitable shell executable found". The fix resolves Git Bash by
explicit path on Windows. It must NEVER use shutil.which("bash") there:
on a stock PATH that finds C:\\WINDOWS\\system32\\bash.exe, which is WSL.
"""

import os

import pytest

import deerflow.sandbox.local.local_sandbox as local_sandbox
from deerflow.sandbox.local.local_sandbox import LocalSandbox


@pytest.fixture()
def windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")


def test_prefers_git_bash_when_posix_shells_absent(windows, monkeypatch):
    git_bash = os.path.join(r"C:\Program Files", "Git", "bin", "bash.exe")
    monkeypatch.setattr(local_sandbox.os.path, "isfile", lambda p: p == git_bash)
    monkeypatch.setattr(local_sandbox.shutil, "which", lambda _: None)

    assert LocalSandbox._get_shell() == git_bash


def test_falls_back_to_git_sh_then_path_sh(windows, monkeypatch):
    git_sh = os.path.join(r"C:\Program Files", "Git", "usr", "bin", "sh.exe")
    monkeypatch.setattr(local_sandbox.os.path, "isfile", lambda p: p == git_sh)
    monkeypatch.setattr(local_sandbox.shutil, "which", lambda _: None)

    assert LocalSandbox._get_shell() == git_sh


def test_never_asks_which_for_bash(windows, monkeypatch):
    # which("bash") on a service PATH returns WSL's system32 bash.exe —
    # resolving it would run commands in a different OS.
    monkeypatch.setattr(local_sandbox.os.path, "isfile", lambda p: False)
    asked = []

    def fake_which(name):
        asked.append(name)
        return None

    monkeypatch.setattr(local_sandbox.shutil, "which", fake_which)

    with pytest.raises(RuntimeError):
        LocalSandbox._get_shell()
    assert "bash" not in asked


def test_which_sh_still_wins_when_git_absent(windows, monkeypatch):
    monkeypatch.setattr(local_sandbox.os.path, "isfile", lambda p: False)
    monkeypatch.setattr(local_sandbox.shutil, "which", lambda name: r"D:\tools\sh.exe" if name == "sh" else None)

    assert LocalSandbox._get_shell() == r"D:\tools\sh.exe"
