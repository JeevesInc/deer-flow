"""Load deer-flow/backend/.env into os.environ.

Import and call load_env() at the start of any cron script that runs
outside of `uv run` (which loads .env automatically). Safe to call
multiple times — never overwrites already-set variables.
"""

import os
from pathlib import Path


def load_env() -> None:
    """Load deer-flow/backend/.env into os.environ (no-op if already set)."""
    env_file = Path(__file__).resolve().parent.parent.parent.parent / 'backend' / '.env'
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)
    except ImportError:
        _manual_load(env_file)


def _manual_load(env_file: Path) -> None:
    """Fallback: parse .env manually without python-dotenv."""
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip()
            # Strip surrounding quotes
            if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
