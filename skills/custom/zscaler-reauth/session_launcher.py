#!/usr/bin/env python3
"""Run a command inside the interactive desktop session from a session-0 service.

Why this exists
---------------
DeerFlow runs as a Windows service: SYSTEM, session 0. ZSATray runs in Brian's
session 1. Zscaler reauth ends with a UI Automation step against the tray window,
and UIA cannot cross a session boundary -- which is why headless reauth failed.

The fix is not to give up on headless, it is to stop trying to reach ACROSS the
boundary and instead spawn the worker ON THE OTHER SIDE of it. A SYSTEM process
holding SE_TCB_PRIVILEGE can duplicate the logged-on user's token
(WTSQueryUserToken) and start a process with it on winsta0\\default via
CreateProcessAsUserW. That child is a normal session-1 user process: it sees the
tray, it can drive UIA, and no human touches anything.

Exposes:
    find_active_session()  -> (session_id, username) or (None, None)
    run_in_user_session(argv, log_path, timeout) -> (exit_code, output)

CLI:
    session_launcher.py --selftest     prove the mechanism, print child's session
    session_launcher.py -- <cmd ...>   run an arbitrary command in the user session
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import time

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)

WTSActive = 0
WTSConnected = 1
WTSDisconnected = 4
MAXIMUM_ALLOWED = 0x02000000
TokenPrimary = 1
SecurityImpersonation = 2
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
NORMAL_PRIORITY_CLASS = 0x00000020
INFINITE = 0xFFFFFFFF
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0


class WTS_SESSION_INFOW(ctypes.Structure):
    _fields_ = [("SessionId", wt.DWORD),
                ("pWinStationName", wt.LPWSTR),
                ("State", wt.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR),
                ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.POINTER(wt.BYTE)),
                ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


def current_session_id():
    sid = ctypes.c_ulong()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(sid))
    return sid.value


def find_active_session():
    """Return (session_id, username) of the interactive console session.

    WTSGetActiveConsoleSessionId alone is not enough: it returns 0xFFFFFFFF when
    the console is switching, and it misses an RDP-connected user. Enumerate and
    prefer a session in the WTSActive state that has a real username.
    """
    count = wt.DWORD(0)
    ptr = ctypes.POINTER(WTS_SESSION_INFOW)()
    if not wtsapi32.WTSEnumerateSessionsW(wt.HANDLE(0), 0, 1,
                                          ctypes.byref(ptr), ctypes.byref(count)):
        return None, None
    active, disconnected = [], []
    try:
        for i in range(count.value):
            s = ptr[i]
            if s.SessionId == 0 or s.State not in (WTSActive, WTSDisconnected):
                continue
            buf = wt.LPWSTR()
            n = wt.DWORD(0)
            user = ""
            # WTSUserName = 5
            if wtsapi32.WTSQuerySessionInformationW(wt.HANDLE(0), s.SessionId, 5,
                                                    ctypes.byref(buf), ctypes.byref(n)):
                user = buf.value or ""
                wtsapi32.WTSFreeMemory(buf)
            if not user:
                continue
            (active if s.State == WTSActive else disconnected).append(
                (s.SessionId, user))
    finally:
        wtsapi32.WTSFreeMemory(ptr)

    # A locked desktop is still WTSActive. An RDP-disconnected or fast-user-switched
    # desktop reports WTSDisconnected but still owns a usable primary token and still
    # has ZSATray running in it, so it is perfectly launchable. Only "nobody signed in
    # at all" is truly unlaunchable. Prefer Active, fall back to Disconnected, and
    # prove the choice by actually acquiring the token instead of trusting the state.
    for sid, user in active + disconnected:
        tok = wt.HANDLE()
        if wtsapi32.WTSQueryUserToken(wt.DWORD(sid), ctypes.byref(tok)):
            kernel32.CloseHandle(tok)
            return sid, user
    if active or disconnected:
        return (active + disconnected)[0]
    return None, None


def session_report():
    """Human-readable evidence line for every enumerated session."""
    names = {0: "Active", 1: "Connected", 2: "ConnectQuery", 3: "Shadow",
             4: "Disconnected", 5: "Idle", 6: "Listen", 7: "Reset", 8: "Down",
             9: "Init"}
    count = wt.DWORD(0)
    ptr = ctypes.POINTER(WTS_SESSION_INFOW)()
    if not wtsapi32.WTSEnumerateSessionsW(wt.HANDLE(0), 0, 1,
                                          ctypes.byref(ptr), ctypes.byref(count)):
        return "session enumeration failed"
    rows = []
    try:
        for i in range(count.value):
            s = ptr[i]
            if s.SessionId >= 65536:
                continue
            buf = wt.LPWSTR()
            n = wt.DWORD(0)
            user = ""
            if wtsapi32.WTSQuerySessionInformationW(wt.HANDLE(0), s.SessionId, 5,
                                                    ctypes.byref(buf), ctypes.byref(n)):
                user = buf.value or ""
                wtsapi32.WTSFreeMemory(buf)
            rows.append("s%d %s user=%s" % (s.SessionId,
                                            names.get(s.State, s.State),
                                            user or "none"))
    finally:
        wtsapi32.WTSFreeMemory(ptr)
    return "; ".join(rows)


def run_in_user_session(argv, log_path=None, timeout=300):
    """Start argv in the interactive session; wait; return (exit_code, output).

    Output is captured by redirecting the child's stdout/stderr into a file at
    the OS level -- CreateProcessAsUserW cannot inherit our pipes across the
    session boundary cleanly, and a file is what survives.
    """
    sid, user = find_active_session()
    if sid is None:
        return 1, "no active interactive session found"

    if log_path is None:
        log_path = os.path.join(tempfile.gettempdir(),
                                f"session_launch_{int(time.time())}.log")

    htok = wt.HANDLE()
    if not wtsapi32.WTSQueryUserToken(wt.DWORD(sid), ctypes.byref(htok)):
        err = ctypes.get_last_error()
        hint = " (needs SE_TCB_PRIVILEGE -- must run as SYSTEM)" if err == 1314 else ""
        return 1, f"WTSQueryUserToken failed: WinError {err}{hint}"

    hdup = wt.HANDLE()
    if not advapi32.DuplicateTokenEx(htok, MAXIMUM_ALLOWED, None,
                                     SecurityImpersonation, TokenPrimary,
                                     ctypes.byref(hdup)):
        kernel32.CloseHandle(htok)
        return 1, f"DuplicateTokenEx failed: WinError {ctypes.get_last_error()}"
    kernel32.CloseHandle(htok)

    env = ctypes.c_void_p()
    if not userenv.CreateEnvironmentBlock(ctypes.byref(env), hdup, False):
        env = None  # not fatal; child inherits a minimal environment

    # cmd.exe wrapper gives us shell-level redirection into the log file.
    quoted = " ".join(f'"{a}"' if " " in str(a) else str(a) for a in argv)
    cmdline = f'cmd.exe /c {quoted} > "{log_path}" 2>&1'

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = "winsta0\\default"   # the interactive desktop -- required for UIA
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    pi = PROCESS_INFORMATION()

    flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | NORMAL_PRIORITY_CLASS
    ok = advapi32.CreateProcessAsUserW(
        hdup, None, ctypes.create_unicode_buffer(cmdline), None, None, False,
        flags, env, os.environ.get("SystemDrive", "C:") + "\\", ctypes.byref(si),
        ctypes.byref(pi))
    if not ok:
        err = ctypes.get_last_error()
        if env:
            userenv.DestroyEnvironmentBlock(env)
        kernel32.CloseHandle(hdup)
        return 1, f"CreateProcessAsUserW failed: WinError {err}"

    kernel32.WaitForSingleObject(pi.hProcess, int(timeout * 1000))
    code = wt.DWORD()
    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(pi.hThread)
    if env:
        userenv.DestroyEnvironmentBlock(env)
    kernel32.CloseHandle(hdup)

    out = ""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            out = f.read()
    except Exception as e:
        out = f"(could not read child log {log_path}: {e})"
    return code.value, out


def _selftest():
    print(f"parent session = {current_session_id()}")
    sid, user = find_active_session()
    print(f"active session = {sid}  user = {user!r}")
    if sid is None:
        print("SELFTEST FAIL: no interactive session")
        return 1
    probe = (
        "import ctypes,os,getpass;"
        "s=ctypes.c_ulong();"
        "ctypes.windll.kernel32.ProcessIdToSessionId("
        "ctypes.windll.kernel32.GetCurrentProcessId(),ctypes.byref(s));"
        "print('child_session=',s.value);"
        "print('child_user=',getpass.getuser());"
        "print('child_localappdata=',os.environ.get('LOCALAPPDATA'))"
    )
    code, out = run_in_user_session([sys.executable, "-c", probe], timeout=60)
    print(f"child rc={code}")
    print(out.strip())
    return 0 if ("child_session= 0" not in out and code == 0) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--" in sys.argv:
        i = sys.argv.index("--")
        rc, out = run_in_user_session(sys.argv[i + 1:], timeout=600)
        print(out)
        sys.exit(rc)
    print(__doc__)
