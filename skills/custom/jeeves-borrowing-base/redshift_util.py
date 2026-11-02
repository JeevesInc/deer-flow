"""DEPRECATED shim — the canonical connector lives in skills/custom/_shared/redshift.py.

Kept so existing `from redshift_util import connect` imports keep working;
new code should import from _shared/redshift directly (P2-4 / Skills-H5).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))

from redshift import connect, ensure_deps  # noqa: F401,E402
