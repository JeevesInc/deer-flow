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

    # Step 2: Run the main reauth script (single attempt)
    if not os.path.exists(MAIN_SCRIPT):
        print(f"ERROR: Reauth script not found at {MAIN_SCRIPT}", file=sys.stderr)
        sys.exit(1)

    # --force: skip the daemon's general-connectivity gate. The keep-alive log
    # only reflects the public-internet tunnel, so it can read "connected" while
    # ZPA (private access, which Redshift goes through) is deauthed. We already
    # know Redshift is broken — go straight to reauth.
    result = subprocess.run(
        [sys.executable, MAIN_SCRIPT, "--once", "--force"],
        timeout=300,  # 5 min max
        capture_output=False,  # let output flow to agent
    )

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
    print("Brian may need to authenticate manually on the laptop.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
