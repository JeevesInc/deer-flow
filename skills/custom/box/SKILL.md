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

### Box Sign — NOT AVAILABLE on the current token (verified 2026-08-27)
`box_client` exposes `sign_requests` (the SDK attribute is present, `has sign_requests: True`), but
BOTH `sign_requests.get_sign_requests()` and `create_sign_request()` return `BoxAPIError` — the app
lacks the Sign scopes. The SDK attribute existing is NOT evidence of permission; do not report a Sign
capability as working until a live LIST call returns 200. This blocked the Kroll third-party access
letter on 2026-08-27: the prefilled PDF uploaded fine (`file_id 2431260908588`) and only the signature
request failed. **Workaround until the scope is granted:** upload the prefilled PDF to Box, then ask
the signer (Brandon McGathy, GC) to run Box Sign from the Box UI, or route via a channel that works —
do not burn turns retrying `create_sign_request`. **Fix (Brian / IT):** Box Admin Console → Apps →
Custom Apps Manager → the Capital-Markets-Analyst app → enable the **"Manage Signature Requests"**
application scope, then **re-authorize** the app (scope changes require re-authorization; the existing
client credentials keep working).
**Re-verified 2026-09-30: still 403** on the NB $2MM Borrowing Request + 4.02(d) certificate — the Sign
request had to be started from the Box web UI.

### DOCX -> PDF before upload: there is NO LibreOffice/soffice on this host (re-confirmed 2026-09-30)
`shutil.which('soffice')` is None and neither Program Files LibreOffice path exists — never write a
`subprocess.run([soffice, ...])` step. Use Word COM (`win32com.client.Dispatch('Word.Application')`)
and pass it an **absolute real Windows path** (`os.path.abspath` of a path built from `OUTPUTS_PATH` /
`WORKSPACE_PATH`), never a virtual `mnt/user-data` path or a relative one — on 2026-09-30 Word returned
*"Sorry, we couldn't find your file"* and box_up.py fell back to uploading the .docx.

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

## Operational hard rules (learned the hard way)

- **A signature is NEVER proven by a file existing.** Box Sign writes an **UNSIGNED copy of the
  document into "My Sign Requests" the moment the request is created**, so any watcher that matches
  on filename alone will report a signature that has not happened. On 2026-09-22 that produced a
  "Signed officer certification came back from Box" DM to Brian at 10:14 and a distribution draft
  built on an unsigned NB Section 6.01(c) certificate; both had to be retracted, and Brian's rule
  followed: *"until then, there shouldn't be a draft."* **Prove execution by the sign-request
  STATUS, or by a size/hash change against the recorded unsigned original** (see
  `.deer-flow/_nb_cert_watch.json`, which stores `unsigned_size` for exactly this comparison), or by
  the Box Signing Log. And while an external signature is pending, **build no outbound draft at all**.

- **Never satisfy a Box deliverable with a shortcut, web_link, or bookmark.** Diligence
  folders must contain REAL FILE BYTES. A Box shortcut/Drive-shortcut looks like a filed
  document in the folder listing but downloads/syncs as a 0-byte stub -- Brian discovered
  this on 2026-07-24 when Pedros Kroll underwriting files uploaded as stubs
  ("they were just stubs, and not real files"). Always download the real content and
  `uploads.upload_file` the bytes into the target folder.
- **Verify every write.** After any upload/copy/move, re-list the parent folder and assert
  each intended item has `type == "file"` and `size > 0`. Report the count of 0-byte items
  explicitly -- a 0-byte file in a data room is a defect whether it came from a shortcut or
  from a bad source export (the 6 empty SCR reports -- 2Eletro, Copastur, Easytech, Flytour,
  Onfly, Voll -- were bad at source and had to be re-pulled from Pedro).
- **Folders are addressed by numeric ID, never by name.** Passing a name yields
  `Cannot access folder (ID: Kroll)`, which reads like a permissions error but is not.
  Resolve name -> ID first by listing the parent (`folders.get_folder_items`) or searching.
  Known IDs: Kroll root `391275668946`, Phase 1 `391276628990`.
- **Do not guess `box_sdk_gen` class names.** There is no `UpdateFileByIdRequestBody` and no
  request-body class for search -- both were invented and cost ImportErrors on 2026-07-24.
  The managers take plain kwargs:
  `client.files.update_file_by_id(file_id, name="New Name.pdf")`,
  `client.search.search_for_content(query="...", ancestor_folder_ids=["<id>"])`,
  `client.files.delete_file_by_id(file_id)`,
  `client.uploads.upload_file(UploadFileAttributes(name=..., parent=UploadFileAttributesParentField(id=...)), file=<bytes stream>)`.
  If unsure, `dir(box_sdk_gen.managers.files.FilesManager)` / `inspect.signature(...)` before writing the script.
  `box_sdk_gen` IS installed in the backend venv -- run scripts with `$PYTHON_PATH`.
- **Sorting a tree by recency** (Brian asks this when hunting "the folder someone touched"):
  request `fields=["name","modified_at","size","type"]` and sort client-side on `modified_at`;
  Box folder listings are not returned in modified order.

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
