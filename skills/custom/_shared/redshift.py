"""Shared Redshift connection helpers (P2-4 / Skills-H5).

Eight skills used to carry their own psycopg2 connectors with silently
divergent timeouts (60/120/300/none) and SSL settings. This is the one
canonical connector; new code imports from here.

Windows gotcha (memory: windows-fixes): `sslmode='require'` +
`sslrootcert='disable'` is REQUIRED — cert verification fails without it.
Redshift is only reachable when the Zscaler tunnel is authenticated; a
timeout/SSL-closed error usually means run zscaler-reauth, not a code bug.

Usage:
    import sys, os
    sys.path.insert(0, os.path.join(os.environ.get('SKILLS_PATH', '/mnt/skills'), 'custom', '_shared'))
    from redshift import connect
    with connect() as conn:
        ...
"""

import os
import sys

DEFAULT_STATEMENT_TIMEOUT_MS = 120_000
DEFAULT_CONNECT_TIMEOUT_S = 30

_REQUIRED_ENV = ('REDSHIFT_HOST', 'REDSHIFT_PORT', 'REDSHIFT_DB',
                 'REDSHIFT_USER', 'REDSHIFT_PASSWORD')


def ensure_deps(*extras):
    """Auto-install required packages if missing.

    Always checks for psycopg2, pandas, openpyxl. Pass extra package names
    (e.g. 'xlsxwriter') to also require those.
    """
    required = ['psycopg2', 'pandas', 'openpyxl'] + list(extras)
    missing = False
    for pkg in required:
        try:
            __import__(pkg)
        except ImportError:
            missing = True
            break
    if missing:
        import subprocess
        pip_names = [('psycopg2-binary' if p == 'psycopg2' else p) for p in required]
        # 'uv pip install' — the backend venv is uv-managed and may lack the pip module.
        subprocess.check_call(['uv', 'pip', 'install', '--python', sys.executable, '-q'] + pip_names)


# ── Zscaler ZPA auto-recovery ────────────────────────────────────────────────
# Redshift is only reachable through the ZPA tunnel. When ZPA drops its auth,
# the TCP connect still succeeds (ZPA answers) and then the session is torn
# down, so psycopg2 reports "server closed the connection unexpectedly" rather
# than anything that looks like an auth problem.
#
# 2026-08-17: ZPA was deauthed from 07:22 to 08:40. cap-markets-refresh and
# analytics_cron each logged one warning per attempt and produced no data for
# 78 minutes. The Zscaler monitor never noticed, because its health checks are
# the ZSATray keep-alive and an HTTPS fetch of google.com — neither of which
# travels the ZPA route. Nothing in the system connected "Redshift is dead" to
# "the tunnel needs reauth", so the outage only ended when a human intervened.
#
# We can't run the reauth script directly: the continuous monitor holds a
# singleton lock for its entire lifetime, so a spawned `--once` run exits
# immediately without doing anything. Instead we drop a request file that the
# monitor consumes on its next cycle (and that a scheduled `--once` run will
# also pick up if no monitor is alive).
_ZPA_DEAUTH_SIGNATURES = (
    'server closed the connection unexpectedly',
    'ssl syscall error',
    'ssl connection has been closed unexpectedly',
    'could not connect to server',
    'connection timed out',
    'timeout expired',
    'no route to host',
)

_REAUTH_REQUEST_FILE = os.environ.get(
    'ZSCALER_REAUTH_REQUEST_FILE',
    os.path.join('C:' + os.sep, 'Jeeves', 'redshift-bot', 'tmp', 'zscaler_force_reauth.request'),
)
# Monitor polls every 120s and a reauth cycle takes ~40s, so the default gives
# one full detect-and-fix round plus headroom.
_REAUTH_WAIT_S = int(os.environ.get('REDSHIFT_ZPA_WAIT_S', '210'))
_REAUTH_POLL_S = 15


def _looks_like_zpa_deauth(exc) -> bool:
    msg = str(exc).lower()
    return any(sig in msg for sig in _ZPA_DEAUTH_SIGNATURES)


def _request_zscaler_reauth(reason: str) -> bool:
    """Best-effort: ask the Zscaler monitor to reauthenticate. Never raises."""
    try:
        os.makedirs(os.path.dirname(_REAUTH_REQUEST_FILE), exist_ok=True)
        with open(_REAUTH_REQUEST_FILE, 'w', encoding='utf-8') as fh:
            fh.write(f'redshift connector (pid {os.getpid()}): {reason}'[:400])
        return True
    except Exception:
        return False


def _raw_connect(statement_timeout_ms: int, connect_timeout_s: int):
    import psycopg2
    return psycopg2.connect(
        host=os.environ['REDSHIFT_HOST'],
        port=int(os.environ['REDSHIFT_PORT']),
        dbname=os.environ['REDSHIFT_DB'],
        user=os.environ['REDSHIFT_USER'],
        password=os.environ['REDSHIFT_PASSWORD'],
        sslmode='require',
        sslrootcert='disable',
        connect_timeout=connect_timeout_s,
        options=f'-c statement_timeout={int(statement_timeout_ms)}',
    )


def connect(statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
            connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S,
            auto_reauth: bool = True):
    """Return a psycopg2 connection to Redshift using REDSHIFT_* env vars.

    On a failure that matches a Zscaler ZPA deauth, request a reauth and retry
    until ``REDSHIFT_ZPA_WAIT_S`` elapses. Set ``auto_reauth=False`` (or
    ``REDSHIFT_ZPA_AUTORECOVER=0``) for callers that must fail fast.
    """
    import time

    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

    try:
        return _raw_connect(statement_timeout_ms, connect_timeout_s)
    except Exception as first_error:
        enabled = auto_reauth and os.environ.get('REDSHIFT_ZPA_AUTORECOVER', '1') != '0'
        if not enabled or not _looks_like_zpa_deauth(first_error):
            raise
        if not _request_zscaler_reauth(str(first_error)[:200]):
            raise
        sys.stderr.write(
            f'[redshift] connection failed like a Zscaler ZPA deauth ({first_error}); '
            f'requested reauth, retrying for up to {_REAUTH_WAIT_S}s\n')
        deadline = time.time() + _REAUTH_WAIT_S
        while time.time() < deadline:
            time.sleep(_REAUTH_POLL_S)
            try:
                conn = _raw_connect(statement_timeout_ms, connect_timeout_s)
                sys.stderr.write('[redshift] reconnected after Zscaler reauth\n')
                return conn
            except Exception as retry_error:
                if not _looks_like_zpa_deauth(retry_error):
                    raise
        raise first_error


def query(sql, params=None, statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS):
    """One-shot query returning (columns, rows). Opens and closes a connection."""
    conn = connect(statement_timeout_ms=statement_timeout_ms)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall() if cur.description else []
        cur.close()
        return columns, rows
    finally:
        try:
            conn.close()
        except Exception:
            pass
