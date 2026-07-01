"""
Box connection helper — Client Credentials Grant (CCG), server-to-server.

Built against the Box "SDK Gen" package (PyPI: boxsdk==10.x, imports as box_sdk_gen).
NOT the legacy boxsdk<=3.x (which exposed `from boxsdk import Client, CCGAuth`).

Credentials are read from environment variables — NO secrets in this file:
    BOX_CLIENT_ID       (required)  Custom App client ID from Box Developer Console
    BOX_CLIENT_SECRET   (required)  Custom App client secret
    BOX_ENTERPRISE_ID   (one of)    authenticate as the enterprise service account
    BOX_USER_ID         (one of)    OR authenticate as a specific managed user
        -> provide exactly one of BOX_ENTERPRISE_ID / BOX_USER_ID

Usage:
    from box_client import get_client
    client = get_client()
    me = client.users.get_user_me()
    print(me.name, me.login)

Connectivity self-test:
    uv run python box_client.py            # whoami + list root folder
"""
import os
import sys

# Corporate network (Zscaler) re-signs TLS with its own root CA. Python's bundled
# certifi store does not trust it, causing CERTIFICATE_VERIFY_FAILED. truststore
# routes verification through the OS (Windows) trust store, which has the Zscaler CA.
try:
    import truststore as _truststore
    _truststore.inject_into_ssl()
except Exception:
    pass  # falls back to certifi; will surface a clear SSL error if trust is missing


REQUIRED = ("BOX_CLIENT_ID", "BOX_CLIENT_SECRET")

# Persisted secrets fallback. Env vars win; if absent, read this JSON file so crons
# can authenticate unattended. Override path with BOX_SECRETS_FILE.
_SECRETS_FILE = os.environ.get(
    "BOX_SECRETS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "box_secrets.json"),
)


def _secrets_from_file():
    import json
    try:
        with open(_SECRETS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("[warn] could not read %s: %s" % (_SECRETS_FILE, e))
        return {}


def _get(key):
    """Env var first, then persisted secrets file."""
    val = os.environ.get(key)
    if val:
        return val
    return _secrets_from_file().get(key)


def _load_config():
    from box_sdk_gen import CCGConfig

    missing = [v for v in REQUIRED if not _get(v)]
    if missing:
        raise RuntimeError(
            "Missing Box credentials in environment: "
            + ", ".join(missing)
            + ". Set them (Box Developer Console -> Custom App -> Configuration)."
        )

    enterprise_id = _get("BOX_ENTERPRISE_ID")
    user_id = _get("BOX_USER_ID")
    if not enterprise_id and not user_id:
        raise RuntimeError(
            "Set exactly one of BOX_ENTERPRISE_ID (service-account/enterprise auth) "
            "or BOX_USER_ID (act as a specific user)."
        )
    if enterprise_id and user_id:
        raise RuntimeError(
            "Set only ONE of BOX_ENTERPRISE_ID or BOX_USER_ID, not both."
        )

    return CCGConfig(
        client_id=_get("BOX_CLIENT_ID"),
        client_secret=_get("BOX_CLIENT_SECRET"),
        enterprise_id=enterprise_id,
        user_id=user_id,
    )


def get_client():
    """Return an authenticated BoxClient using Client Credentials Grant."""
    from box_sdk_gen import BoxCCGAuth, BoxClient

    auth = BoxCCGAuth(config=_load_config())
    return BoxClient(auth=auth)


def _self_test():
    try:
        client = get_client()
    except RuntimeError as e:
        print("[config] " + str(e))
        return 2

    try:
        me = client.users.get_user_me()
        print("[ok] authenticated as: %s <%s>  (id=%s)" % (me.name, me.login, me.id))
    except Exception as e:
        print("[auth] failed to call /users/me: %s: %s" % (type(e).__name__, e))
        return 1

    try:
        root = client.folders.get_folder_items("0")
        entries = list(root.entries or [])
        print("[ok] root folder '0' has %d item(s):" % len(entries))
        for it in entries[:25]:
            print("   - [%s] %s (id=%s)" % (it.type, it.name, it.id))
    except Exception as e:
        print("[list] could not list root folder: %s: %s" % (type(e).__name__, e))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
