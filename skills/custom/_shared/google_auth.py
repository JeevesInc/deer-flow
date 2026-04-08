"""Shared Google OAuth credential helper for all custom skills.

All skills that use Google APIs (Drive, Gmail, Calendar) share the same
OAuth refresh token flow. This module centralises that logic so credential
setup is defined once.

Required environment variables:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN
"""

import os
import sys


def _ensure_google_deps():
    """Auto-install google-api-python-client and google-auth if missing."""
    try:
        from google.oauth2.credentials import Credentials  # noqa: F401
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q',
                               'google-api-python-client', 'google-auth'])


def get_credentials(required=True):
    """Build Google OAuth Credentials from environment variables.

    Args:
        required: If True (default), prints error and exits on missing vars.
                  If False, returns None when vars are missing (graceful skip).
    """
    _ensure_google_deps()
    from google.oauth2.credentials import Credentials

    for var in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN'):
        if not os.environ.get(var):
            if required:
                print(f"ERROR: Missing environment variable {var}", file=sys.stderr)
                sys.exit(1)
            return None

    return Credentials(
        token=None,
        refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
    )
