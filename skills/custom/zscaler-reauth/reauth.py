#!/usr/bin/env python3
"""
Zscaler reauth — callable from deer-flow agent.

Checks connectivity, runs reauth if needed, verifies recovery.
Exits 0 on success (or if already connected), 1 on failure.
"""

import os
import subprocess
import sys
import time

# Resolve paths — this runs inside the deer-flow bash sandbox,
# so /mnt/skills/custom/ maps to the real skills directory.
# The main script lives at the repo root: scripts/zscaler_reauth.py
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
MAIN_SCRIPT = os.path.join(REPO_ROOT, "scripts", "zscaler_reauth.py")

# Also check Windows-native path in case /mnt mapping doesn't resolve
if not os.path.exists(MAIN_SCRIPT):
    MAIN_SCRIPT = r"C:\Jeeves\redshift-bot\scripts\zscaler_reauth.py"


def check_redshift_connectivity() -> bool:
    """Quick check: can we connect to Redshift right now?"""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get("REDSHIFT_HOST", ""),
            port=int(os.environ.get("REDSHIFT_PORT", "5439")),
            dbname=os.environ.get("REDSHIFT_DB", ""),
            user=os.environ.get("REDSHIFT_USER", ""),
            password=os.environ.get("REDSHIFT_PASSWORD", ""),
            sslmode="require",
            sslrootcert="disable",
            connect_timeout=10,
        )
        conn.cursor().execute("SELECT 1")
        conn.close()
        return True
    except Exception as e:
        print(f"Redshift connectivity check failed: {e}", file=sys.stderr)
        return False


def _check_rate_limit() -> bool:
    """Return True if the shared rate limit is active (caller should skip reauth)."""
    import json as _json
    from datetime import datetime as _dt, timedelta as _td
    state_file = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', '..', '..', 'zscaler_reauth_state.json'))
    if not os.path.exists(state_file):
        state_file = 'C:/Jeeves/redshift-bot/zscaler_reauth_state.json'
    try:
        with open(state_file) as f:
            state = _json.load(f)
        now = _dt.now()
        cutoff = now - _td(hours=1)
        attempts = [t for t in state.get('attempts_this_hour', [])
                    if _dt.fromisoformat(t) > cutoff]
        if len(attempts) >= 4:
            print(f'Reauth rate limit: {len(attempts)} attempts in the last hour. Skipping.', file=sys.stderr)
            return True
        last = state.get('last_reauth_attempt')
        if last:
            last_dt = _dt.fromisoformat(last)
            if (now - last_dt).total_seconds() < 300:
                remaining = 300 - (now - last_dt).total_seconds()
                print(f'Reauth cooldown active ({remaining:.0f}s remaining). Skipping.', file=sys.stderr)
                return True
    except Exception:
        pass
    return False


def _in_session_zero() -> bool:
    """True if this process is in Windows session 0 (running as a service).

    ZSATray lives in the interactive user's session. Token injection drives that
    tray window via UI Automation, which cannot cross the session boundary, so a
    reauth launched from session 0 can never complete no matter how many times it
    retries.
    """
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        sid = ctypes.c_ulong()
        if k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid)):
            return sid.value == 0
    except Exception as e:
        print(f"session detection failed: {e}", file=sys.stderr)
    return False


def _alert_owner(msg: str) -> None:
    """DM Brian through the audited slack_tool send path (never hand-rolled)."""
    try:
        tool = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "slack-search", "slack_tool.py"))
        if not os.path.exists(tool):
            print(f"slack_tool not found at {tool}", file=sys.stderr)
            return
        subprocess.run([sys.executable, tool, "send", "U05B5HGNCN9", msg], timeout=60)
    except Exception as e:
        print(f"owner alert failed: {e}", file=sys.stderr)


def _boot_epoch() -> float:
    """Wall-clock epoch of the last boot, from the monotonic tick counter."""
    import ctypes as _c
    return time.time() - (_c.windll.kernel32.GetTickCount64() * 0.001)


def _alert_once_per_boot(key: str, msg: str) -> bool:
    """Send msg at most once per boot. Returns True if it was sent."""
    import json as _json
    marker = r"C:\Jeeves\redshift-bot\zscaler_alert_marker.json"
    boot_id = "%s-%d" % (key, int(_boot_epoch()))
    try:
        seen = _json.load(open(marker)) if os.path.exists(marker) else {}
    except Exception:
        seen = {}
    if seen.get(key) == boot_id:
        print("alert %s already sent for this boot -- staying quiet." % key)
        return False
    _alert_owner(msg)
    seen[key] = boot_id
    try:
        with open(marker, "w") as f:
            _json.dump(seen, f)
    except Exception as e:
        print("marker write failed: %s" % e, file=sys.stderr)
    return True


def _hand_off_to_monitor(wait_s: int = 240) -> None:
    """The continuous monitor holds the singleton lock, so our --once child
    exited without doing anything (2026-09-28: that turned into a false
    'reauth ran but Redshift is still unreachable' alert). Drop the out-of-band
    request file the monitor honours (Step 1.6 in run_reauth_cycle) and give it
    time to do the reauth before we judge the outcome."""
    req = r"C:\Jeeves\redshift-bot\tmp\zscaler_force_reauth.request"
    try:
        with open(req, "w", encoding="utf-8") as f:
            f.write("skill reauth.py (lock held by monitor)")
        print(f"Monitor holds the lock -- requested reauth via {req}; waiting up to {wait_s}s")
    except Exception as e:
        print(f"could not write reauth request: {e}", file=sys.stderr)
        return
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(20)
        if check_redshift_connectivity():
            print("Monitor completed the reauth.")
            return


def main():
    # Step 1: Verify the problem — maybe it's a transient blip
    print("Checking Redshift connectivity...")
    if check_redshift_connectivity():
        print("Redshift is reachable — no reauth needed.")
        sys.exit(0)

    print("Redshift is unreachable. Triggering Zscaler reauth...")

    # Step 1.5: Check shared rate limit before attempting
    if _check_rate_limit():
        print('Rate limit active -- not attempting reauth. Waiting for cooldown.', file=sys.stderr)
        sys.exit(1)

    # Step 1.6: Session-0 preflight. As a Windows service we run as SYSTEM in
    # session 0 while ZSATray runs in Brian's desktop session. UI Automation
    # cannot cross that boundary, so reauth structurally cannot succeed here --
    # retrying just burns 3 minutes and eats the hourly rate limit. Alert and
    # exit instead of failing blind.
    if _in_session_zero():
        sys.path.insert(0, SCRIPT_DIR)
        from session_launcher import find_active_session, session_report
        sid, who = find_active_session()
        if sid is None:
            import datetime as _dt
            evidence = session_report()
            boot = _dt.datetime.fromtimestamp(_boot_epoch()).strftime("%b %d %H:%M")
            print("ERROR: session 0 and no interactive session. %s" % evidence,
                  file=sys.stderr)
            _alert_once_per_boot(
                "no_interactive_session",
                ":lock: *Redshift and every data cron are down until this laptop is "
                "unlocked once.* It rebooted at %s for a Windows update and stopped "
                "at the sign-in screen, so no desktop session exists (%s), ZSATray "
                "never started, and there is no user token for ZPA to reauth with. "
                "Automatic sign-in after update restarts is now armed, so a reboot "
                "will restore the session by itself from here on -- this one still "
                "needs a single unlock. You will not get this alert again until the "
                "next boot." % (boot, evidence)
            )
            sys.exit(1)

    # Step 2: Run the main reauth script (single attempt)
    if not os.path.exists(MAIN_SCRIPT):
        print(f"ERROR: Reauth script not found at {MAIN_SCRIPT}", file=sys.stderr)
        sys.exit(1)

    # --force: skip the daemon's general-connectivity gate. The keep-alive log
    # only reflects the public-internet tunnel, so it can read "connected" while
    # ZPA (private access, which Redshift goes through) is deauthed. We already
    # know Redshift is broken — go straight to reauth.
    reauth_cmd = [sys.executable, MAIN_SCRIPT, "--once", "--force"]

    if _in_session_zero():
        # We are a Windows service (SYSTEM, session 0). ZSATray lives in the
        # interactive session and the token-injection step drives it through UI
        # Automation, which cannot cross a session boundary. So do not reach
        # across it -- spawn the reauth ON the other side using the logged-on
        # user's token (WTSQueryUserToken + CreateProcessAsUserW). The child is
        # an ordinary session-1 user process: it sees the tray, drives UIA, and
        # needs no human. This is what makes headless reauth work as a service.
        from session_launcher import run_in_user_session, find_active_session
        sid, user = find_active_session()
        print(f"Session 0 detected -- launching reauth into session {sid} ({user}).")
        rc, out = run_in_user_session(reauth_cmd, timeout=300)
        if out:
            print(out[-4000:])
        print(f"Session-{sid} reauth exited rc={rc}")
        if out and "already running" in out:
            _hand_off_to_monitor()
    else:
        subprocess.run(reauth_cmd, timeout=300, capture_output=False)

    # Step 3: Wait a moment for Zscaler tunnel to re-establish
    print("Waiting for Zscaler tunnel to stabilize...")
    time.sleep(10)

    # Step 4: Verify Redshift is reachable now
    for attempt in range(3):
        if check_redshift_connectivity():
            print("Redshift connectivity restored! You can retry your query now.")
            sys.exit(0)
        print(f"Redshift still unreachable (attempt {attempt + 1}/3), waiting...")
        time.sleep(5)

    print("ERROR: Reauth ran but Redshift is still unreachable.", file=sys.stderr)
    _alert_owner(
        ":lock: *Zscaler reauth ran but Redshift is still unreachable.* The "
        "automated flow completed without restoring ZPA -- this one needs eyes. "
        "Check the Zscaler tray on the laptop."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
