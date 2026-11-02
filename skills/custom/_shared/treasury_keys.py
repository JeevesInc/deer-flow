"""Read treasury API keys (Stripe, Bridge, Airwallex) captured from Keeper 2026-10-14.
DPAPI-encrypted (daemon machine account) in .deer-flow secrets folder. Never print values."""
import ctypes, ctypes.wintypes as wt, json, re
BLOB_PATH = r"C:\Jeeves\redshift-bot\deer-flow\backend\.deer-flow\secrets\treasury_api_keys.dpapi"
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
def _unprotect(b):
    buf = ctypes.create_string_buffer(b, len(b))
    inb = _BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))); out = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inb), None, None, None, None, 0x1, ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed (wrong account?)")
    r = ctypes.string_at(out.pbData, out.cbData); ctypes.windll.kernel32.LocalFree(out.pbData); return r
def _store():
    with open(BLOB_PATH, "rb") as f:
        return json.loads(_unprotect(f.read()))
def stripe_key():
    return re.search(r"\b[sr]k_live_[A-Za-z0-9]+", _store()["Stripe"]["note"]).group(0)
def bridge_key():
    return re.search(r"\bsk-live-[A-Za-z0-9]+", _store()["Bridge"]["note"]).group(0)
def airwallex_creds():
    v = [l.strip() for l in _store()["Airwallex"]["note"].splitlines() if l.strip() and not l.strip().endswith(":")]
    return v[0], v[1]
