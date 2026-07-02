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


def connect(statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
            connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S):
    """Return a psycopg2 connection to Redshift using REDSHIFT_* env vars."""
    import psycopg2
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")
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
