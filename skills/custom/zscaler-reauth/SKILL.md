---
name: zscaler-reauth
description: Use this skill when you get a Redshift connection error, SSL error, network timeout, or any "could not connect" / "Connection refused" / "timeout expired" error that suggests VPN/Zscaler connectivity is down. Also use when the user explicitly asks you to fix Zscaler or reauthenticate.
allowed-tools:
  - bash
  - read_file
---

# Zscaler Reauth — Network Connectivity Recovery

When Redshift queries fail with connection errors, the most common cause is Zscaler VPN session expiration. This skill triggers reauthentication automatically.

## When to Use

Call this skill when you see any of these errors:
- `could not connect to server: Connection refused`
- `timeout expired` / `connection timed out`
- `SSL connection has been closed unexpectedly`
- `OperationalError: could not translate host name`
- `Network is unreachable`
- Any Redshift/psycopg2 connection failure after a query was working earlier

## How to Trigger Reauth

```bash
python /mnt/skills/custom/zscaler-reauth/reauth.py
```

This will:
1. Check if Zscaler is actually deauthenticated (exits immediately if already connected)
2. Extract the auth URL from Zscaler logs
3. Open Edge via Playwright, enter Okta credentials, select FastPass
4. Use pyautogui to click through the native Okta Verify dialog
5. If FastPass fails, fall back to Okta push notification (sends Slack DM asking Brian to approve)
6. Wait for auth to complete and verify connectivity is restored

**Typical runtime**: 30-90 seconds.

## After Reauth

After the script completes successfully, **retry your original Redshift query**. The connection pool will automatically reconnect on the next attempt.

If reauth fails, tell the user:
> "Zscaler reauthentication failed. You may need to authenticate manually on the laptop."

## Known Failure Modes & Fixes (encoded in script)

### 1. False timeout on push approval (fixed 2026-05-27)
**Symptom:** Script sends ":x: Push was not approved within 180s" even when user approved it.
**Root cause:** `_wait_for_push_approval` used `page.wait_for_url()` in a background thread — Playwright uses greenlets and cannot be called cross-thread. It crashed immediately, reporting timeout.
**Fix:** Replaced threading approach with a synchronous 2s-interval polling loop that checks both `page.url` and `_check_zpa_state_via_registry()` on the main thread. Whichever fires first wins.

### 2. Page-closed crash after FastPass (fixed 2026-05-27)
**Symptom:** FastPass completes, Okta Verify says authenticated, but script reports failure: `"Playwright automation failed: Page.wait_for_timeout: Target page, context or browser has been closed"`.
**Root cause:** When the `zscaler://` or `zsa://` protocol URL fires, Edge closes the Playwright page immediately. `page.wait_for_timeout(5000)` then throws "Target page has been closed" — this bubbled up to the outer `except`, skipped `_verify_zpa_authenticated`, and returned False even though the token had landed.
**Fix:** Wrapped post-auth `page.wait_for_timeout` calls in try/except for "closed". If caught, call `_verify_zpa_authenticated(timeout=45)` directly. If registry flips to AUTHENTICATED → return True. This is now the normal success path for FastPass flows.

## Do NOT

- Do not call this skill preemptively — only when you actually get a connection error
- Do not call it more than once per conversation — if it fails once, escalate to the user
- Do not retry the reauth script in a loop
