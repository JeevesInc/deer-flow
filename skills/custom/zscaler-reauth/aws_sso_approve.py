"""Approve an AWS IAM Identity Center (SSO) device code unattended, via Okta FastPass.

Promoted from thread a2463338 (2026-10-22), where it worked after Brian corrected the
agent for claiming Okta/FastPass needed a human. MUST run inside Brian's interactive
Windows session (FastPass), so call it through session_launcher, not directly:

    import os, sys
    sys.path.insert(0, os.path.join(os.environ['SKILLS_PATH'], 'custom', 'zscaler-reauth'))
    from session_launcher import run_in_user_session
    rc, out = run_in_user_session([sys.executable, '<this file>', '<USER-CODE>', '<log path>'], timeout=240)

Flow: 1) start `aws sso login --use-device-code --no-browser` (USERPROFILE/HOME =
C:\\Users\\BrianMauck) in a background process and read the user code from its output;
2) run this with the code; 3) the CLI login completes on its own. Log + screenshot go to
<log path> and <log path>.png.
"""
import re
import sys

sys.path.insert(0, r"C:\Jeeves\redshift-bot\scripts")
import zscaler_reauth as zr  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

START = "https://d-9a672d6eac.awsapps.com/start/#/device?user_code="


def main(code, out):
    def log(s):
        with open(out, "a", encoding="utf-8") as f:
            f.write(s + "\n")

    zr._load_env_config()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=False)
        kw = {}
        if zr.OKTA_COOKIE_FILE.exists():
            kw["storage_state"] = str(zr.OKTA_COOKIE_FILE)
        ctx = b.new_context(**kw)
        pg = ctx.new_page()
        try:
            pg.goto(START + code, wait_until="networkidle", timeout=60000)
            pg.wait_for_timeout(4000)
            log("at " + pg.url)
            if "okta.com" in pg.url:
                ok = zr._handle_okta_flow(pg)
                log("okta flow returned %s" % ok)
                pg.wait_for_url(re.compile(r"awsapps\.com"), timeout=120000)
                pg.wait_for_load_state("networkidle")
                pg.wait_for_timeout(3000)
            for label in ("Confirm and continue", "Allow access"):
                btn = pg.get_by_role("button", name=re.compile(label, re.I))
                try:
                    btn.first.wait_for(timeout=25000)
                    btn.first.click()
                    log("clicked " + label)
                    pg.wait_for_timeout(4000)
                except Exception:
                    log("no button " + label)
            body = pg.inner_text("body")[:300].replace("\n", " ")
            log(("APPROVED " if "Request approved" in body else "FINAL ") + body)
        except Exception as e:
            log("ERROR %r at %s" % (str(e)[:200], pg.url))
        try:
            pg.screenshot(path=out + ".png")
        except Exception:
            pass
        b.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
