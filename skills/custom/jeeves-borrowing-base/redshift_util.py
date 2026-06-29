"""Shared Redshift connection and dependency helpers for borrowing-base scripts."""

import os
import sys


def ensure_deps(*extras):
    """Auto-install required packages if missing.

    Always checks for psycopg2, pandas, openpyxl.
    Pass extra package names (e.g. 'xlsxwriter') to also require those.
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
        # Use 'uv pip install' — the backend venv is uv-managed and may not have the pip module.
        subprocess.check_call(['uv', 'pip', 'install', '--python', sys.executable, '-q'] + pip_names)


def connect():
    """Return a psycopg2 connection to Redshift using REDSHIFT_* env vars."""
    import psycopg2
    missing = [k for k in ('REDSHIFT_HOST', 'REDSHIFT_PORT', 'REDSHIFT_DB', 'REDSHIFT_USER', 'REDSHIFT_PASSWORD')
               if not os.environ.get(k)]
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
        connect_timeout=30,
        options='-c statement_timeout=120000',
    )
