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
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + pip_names)


def connect():
    """Return a psycopg2 connection to Redshift using REDSHIFT_* env vars."""
    import psycopg2
    return psycopg2.connect(
        host=os.environ['REDSHIFT_HOST'],
        port=int(os.environ['REDSHIFT_PORT']),
        dbname=os.environ['REDSHIFT_DB'],
        user=os.environ['REDSHIFT_USER'],
        password=os.environ['REDSHIFT_PASSWORD'],
        sslmode='require',
        sslrootcert='disable',
    )
