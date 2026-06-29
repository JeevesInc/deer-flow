---
name: box
description: Use this skill whenever the work involves Box or a Box data room (box.com) — any diligence data room, deal data room, document repository, or per-request document folder hosted in Box. This skill is how you both READ and WRITE Box. It authenticates via Client Credentials Grant in as-user mode (inheriting the user's full Box folder access) and lets you list, read, download, upload, move, rename, and delete files and folders. IMPORTANT — Box is a separate system from Google Drive. Having only Drive, Gmail, and Slack tools does NOT mean you lack Box access — you DO have full Box read/write access through this skill, so never tell the user you cannot write to Box. Use it any time work involves a "Box folder", "Box site", "data room", uploading or copying documents into Box, or filling empty Box folders.
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Box (Client Credentials Grant)

Server-to-server Box connection for the daemon. Built on Box's SDK Gen package
(PyPI `boxsdk==10.x`, imports as `box_sdk_gen`) — NOT legacy `boxsdk<=3.x`.

## Status
LIVE as of 2026-06-16. Authenticating in **as-user mode** impersonating Brian Mauck
(`brian.mauck@tryjeeves.com`, BOX_USER_ID `26253882253`) — the service account inherits
Brian's full folder access (CIM, Vista, Akin/PSK Legal Diligence, NB Diligence, data rooms,
Atalaya warehouse, etc.). Creds persisted in `box_secrets.json` (local, not committed).
No allowed-origin/CORS or redirect URI is needed for CCG (server-side).

### Key IDs (verified)
- Real Jeeves Box **Enterprise ID = 849738981** (fetched from live API via
  `users.get_user_me(fields=["enterprise"])`).
- WARNING: the number in the service-account name `AutomationUser_2596889_...` is **NOT** the
  enterprise ID. Using `2596889` as BOX_ENTERPRISE_ID yields
  `400 invalid_grant: Grant credentials are invalid`. This was the original connection bug.
- Service account: `Capital-Markets-Analyst <AutomationUser_2596889_yOXPhFOUVD@boxdevedition.com>`
  (id `51677187670`). In pure enterprise/service-account mode it sees an EMPTY root (0 items)
  until folders are collaborated to it — which is why we run in as-user mode instead.

### Switching auth modes
- **As-user (current):** set `BOX_USER_ID` only (no `BOX_ENTERPRISE_ID`). Sees that user's content.
- **Enterprise/service account:** set `BOX_ENTERPRISE_ID=849738981` only. Sees only folders
  collaborated to the service account.
- Set exactly ONE of the two. Env vars override `box_secrets.json`.

## One-time setup (Brian / IT, in Box Developer Console)
1. Create a Custom App -> Authentication Method = Client Credentials Grant.
2. App Access Level = App + Enterprise Access.
3. Application Scopes = at least "Read all files and folders"; add write scopes to push files.
4. Submit for authorization in the Box Admin Console (Apps -> Custom Apps Manager -> Authorize).
5. Hand off: Client ID, Client Secret, and Enterprise ID (or a specific User ID).

## Environment variables
    BOX_CLIENT_ID        required
    BOX_CLIENT_SECRET    required
    BOX_ENTERPRISE_ID    auth as enterprise service account  (set ONE of these two)
    BOX_USER_ID          OR auth as a specific managed user

## Usage
    import sys, os
    sys.path.insert(0, os.path.join(os.environ.get("SKILLS_PATH","C:/Jeeves/redshift-bot/deer-flow/skills"), "custom", "box"))
    from box_client import get_client
    client = get_client()
    me = client.users.get_user_me()
    items = client.folders.get_folder_items("0")   # "0" = root

## Connectivity self-test
    uv run python C:/Jeeves/redshift-bot/deer-flow/skills/custom/box/box_client.py
Prints the authenticated identity and lists the root folder, or a clear config error.

## Notes
- The service account starts with its OWN empty root. To see existing company folders,
  use Enterprise Access + as-user calls, or collaborate the service account
  (its ...@boxdevedition.com address) onto the target folders.
- Legacy `from boxsdk import Client, CCGAuth` will NOT work with the installed SDK.

## Persisted credentials (for unattended crons)
Creds are stored in `box_secrets.json` next to `box_client.py` and loaded automatically
when env vars are absent. Env vars always take precedence. Override path with
`BOX_SECRETS_FILE`. Keys: BOX_CLIENT_ID, BOX_CLIENT_SECRET, and one of
BOX_ENTERPRISE_ID / BOX_USER_ID. Current production file uses BOX_USER_ID `26253882253`
(as-user mode, Brian). This file holds a live secret — keep it local, do not commit.

## TLS through Zscaler
`box_client.py` calls `truststore.inject_into_ssl()` on import so Box API verification
uses the Windows trust store (which has the Zscaler root CA). Without it you get
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`.
