---
name: google-drive
description: Use this skill when the user shares a Google Drive, Google Docs, Google Sheets, or Google Slides link and wants you to read, summarize, or analyze the content. Also use when a message contains a docs.google.com or drive.google.com URL. Also use this skill when you need to share a generated file (Excel, PowerPoint, CSV, PDF, etc.) with the user — upload it to Google Drive and share the link.
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Google Drive — Read & Upload

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.


## ⚠️ ROOT CAUSE FOUND 2026-08-08 — the import that keeps failing was documented WRONG

`google_auth.py` is NOT in this skill directory. It lives in `custom/_shared/` and exports
exactly one thing: `get_credentials(required=True)` (there is no `get_drive_service`). Every
prior instruction to insert `custom/google-drive` on `sys.path` was wrong, which is why the
same `ModuleNotFoundError` recurred five times through 2026-08-07 (`inspect_nb.py`). Sibling
scripts here work because they insert `os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_shared")`.

**To inspect a Drive file locally, do not write a script — run:**
```bash
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/google-drive/download_file.py <file_id_or_url> --output-dir "$WORKSPACE_PATH"
```
It handles binary files byte-for-byte and exports Sheets/Docs/Slides to xlsx/docx/pptx.

## ⚠️ STOP — Do not import `google_auth` from ad-hoc workspace scripts

**4th recurrence 2026-07-22 (`find_parent.py`, Kroll additional-IDs thread) -- still happening despite this note existing since 07-07/07-13 and being escalated 07-20. The lesson is not "add sys.path.insert," it is: DO NOT WRITE A NEW SCRIPT.** Every recurrence so far has been an agent writing a brand-new one-off workspace script (`search_drive.py`, `find_parent.py`, etc.) to do something an existing script already does, and getting the import wrong along the way. If you catch yourself about to `write_file` a new `.py` in `workspace/` that touches Drive, stop and check this list first:

| Need | Use this, do not write a new script |
|---|---|
| Fetch/read a Doc, Sheet, Slide, or file by URL | `fetch_doc.py` |
| Upload a generated file to Drive | `upload_to_drive.py` |
| Browse/list a folder, or find where a file/folder lives (incl. finding a parent folder) | `list_drive_folder.py --recursive` |

Call it directly: `uv run python C:/Jeeves/redshift-bot/deer-flow/skills/custom/google-drive/<script>.py ...`.

Only write a new one-off script if the task genuinely has no match above -- and even then, copy an existing script as your starting point (it already has the working import) rather than writing the import block from memory. If you still end up writing a new script, add this sys.path.insert line before the import:
```python
import sys, os
sys.path.insert(0, os.path.join(os.environ.get('SKILLS_PATH', 'C:/Jeeves/redshift-bot/deer-flow/skills'), 'custom', 'google-drive'))
from google_auth import get_drive_service
```

## Fetch a Google Doc/Sheet/Slide

```bash
python /mnt/skills/custom/google-drive/fetch_doc.py "<GOOGLE_DRIVE_URL>"
```

Supported file types:
- Google Docs → plain text
- Google Sheets → CSV
- Google Slides → plain text
- .docx/.doc → text via python-docx
- .xlsx/.xls → CSV via openpyxl
- .pptx/.ppt → text via python-pptx
- PDF and other binaries → downloaded to `/mnt/user-data/workspace/` for further processing

## Upload a file to Google Drive

After creating any output file (Excel, PowerPoint, CSV, PDF, etc.), upload it so the user can access it:

```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "/mnt/user-data/outputs/<filename>"
```

Upload to a specific Drive folder instead of the default:

```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "/mnt/user-data/outputs/<filename>" --folder "<FOLDER_ID>"
```

The script:
- Without `--folder`: uploads to "DeerFlow Output" at Drive root.
- With `--folder`: uploads directly to the specified Drive folder
- Makes it viewable by anyone with the link
- Prints the shareable link

**Default behavior is update-in-place, not create-new.** The script strips date suffixes (e.g. "- 20260610") from the filename to find an existing copy of "the same document" and updates that file's content in place (same ID, same link). This is correct for a living/canonical doc, but **wrong for recurring daily-dated deliverables** (EOD reviews, daily reports, dream summaries, etc.) where each day's file should persist as its own distinct artifact. For those: always pass `--new` to force a new file, or check the script's own output ("UPDATED existing file" vs "CREATED new file") and verify the returned file ID does not match a prior day's known ID before trusting the result. Never assume update-in-place is safe just because the filename has a date in it -- that date is exactly what gets stripped for matching. (Logged 2026-07-19.)

**The actual rule (clarified 2026-07-01, supersedes the old 2026-06-11 note): Brian's objection was never to the DeerFlow Output folder name specifically — it's that files must never be left ONLY in a session-scratch folder he will never find (C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/11a3d829-f0ac-4d03-a651-0ee6f7047a8b/user-data/outputs, thread-local temp dirs, etc). Default behavior: actively search Drive first for an appropriate existing home (search by counterparty/project/topic name; use the jeeves-capital-markets folder map for CM work, or a files().list name-contains search for anything else) before uploading. Walk up a candidate folder's parents to confirm it is actually the right project, not a name coincidence.**
- Clear existing folder found -> upload with --folder <ID>.
- Genuinely new, non-Capital-Markets work with no existing home (e.g. a cross-team ops PRD) -> do not guess-create a folder. Tell Brian what you searched and ask where it should live.
- Only fall back to bare DeerFlow Output for true one-off asks with no clear home -- and say so explicitly, do not do it silently.

**IMPORTANT:** Always upload generated files to Google Drive and share the link in your response. Do NOT just say the file was created -- the user cannot access local files. Never leave a deliverable only in C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/11a3d829-f0ac-4d03-a651-0ee6f7047a8b/user-data/outputs or another session/thread-scoped path.

## Browse a Google Drive folder

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "<FOLDER_ID_OR_URL>"
```

List recursively (default depth 2):

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "<FOLDER_ID_OR_URL>" --recursive
```

Set a custom max depth:

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "<FOLDER_ID_OR_URL>" --recursive --max-depth 3
```

Lists each item with name, ID, mimeType, and last modified date. Folders appear first, then files.

## Supported URL formats (fetch & browse)

- `https://docs.google.com/document/d/{ID}/edit`
- `https://docs.google.com/spreadsheets/d/{ID}/edit`
- `https://docs.google.com/presentation/d/{ID}/edit`
- `https://drive.google.com/file/d/{ID}/view`
- `https://drive.google.com/open?id={ID}`
- `https://drive.google.com/drive/folders/{ID}` (for folder browsing)

## Rules

- Always show the document title before summarizing fetched docs
- For large documents, summarize key points rather than echoing the full text
- **Always upload generated files to Drive and share the link** — never leave files only on the local filesystem
- If a script fails with a credentials error, tell the user to check their Google API setup

---



<!-- Evolution: 2026-07-15 | source: self-improving-agent -->
- **When Brian says a file is "saved"/"updated"/"complete" and you already read that file earlier in the session, re-fetch it and check its last-modified timestamp before answering -- do not reuse the earlier in-session read.** Violated 2026-07-14 (MX Servicer Report / Anexo J thread): first pass answered from stale numbers; took two corrections ("take another look", then "it's the same file... you should be using the numbers in the servicer report") before re-pulling and finding the file's modified time had moved and Available Amount had changed materially. Check modified-time on every re-visit of a file already read this session, not just on first fetch.
## Slides API cannot insert a locally-generated image (2026-07-30)

`presentations.batchUpdate` with `replaceImage` or `createImage` requires a **publicly fetchable URL**.
Uploading the PNG to Drive and sharing it does NOT work: the tryjeeves.com domain policy rejects
`type: anyone` with `publishOutNotPermitted`, and domain-scoped sharing still fails Slides' fetch
(400 Invalid on both the `uc?export=download&id=` and `lh3.googleusercontent.com/d/` forms).
Do not burn retries cycling URL formats -- there isn't a working one.

Working pattern: **do all text and geometry edits via the API first**, so a failed image step costs
nothing and never leaves the slide half-edited. Then get the bytes in through the Slides UI.
Never delete the existing placeholder image until the new one is confirmed present: diff the page's
`pageElements` objectIds before/after the insert, then `updatePageElementTransform` (ABSOLUTE, with
scaleX/scaleY = target EMU divided by native size EMU) and only then `deleteObject` the old image.
If the UI path also fails, hand Brian the PNG plus the exact target position and size to drop it
into -- do not report the slide as done.

## Playwright Browser Automation (Comments, UI interactions)

Use this when the Drive/Docs API cannot do the job — specifically for **adding inline anchored comments** to Google Docs. The Drive API `comments.create` anchor field is a known broken Google bug (always shows "original content deleted" in the UI). Playwright is the correct and only reliable approach.

### ⚠️ Critical: Drive API anchored comments are BROKEN — never use them
Never use `drive_service.comments().create()` with an `anchor` field for Google Docs. It is a confirmed long-standing Google bug. Always use Playwright instead.

### Session file (Brian's saved Google auth)
```
C:\Users\BrianMauck\AppData\Local\Temp\playwright_google_session.json
```
Load this in every Playwright context — no login required as long as the file exists and the session is valid.

### One-time login (only if session file is missing or expired)
```python
from playwright.sync_api import sync_playwright
import time

SESSION_PATH = r"C:\Users\BrianMauck\AppData\Local\Temp\playwright_google_session.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--start-maximized"])
    context = browser.new_context(no_viewport=True)
    page = context.new_page()
    page.goto("https://accounts.google.com/signin/v2/identifier?continue=https://docs.google.com")
    print("Waiting for Brian to log in...")
    while True:
        time.sleep(3)
        cookies = context.cookies()
        session = [c for c in cookies if c["name"] in ("__Secure-1PSID", "SID") and "google.com" in c["domain"]]
        if session and "accounts.google.com" not in page.url:
            context.storage_state(path=SESSION_PATH)
            print("Session saved.")
            break
    context.close()
    browser.close()
```

### Standard Playwright session (every other use)
```python
from playwright.sync_api import sync_playwright

SESSION_PATH = r"C:\Users\BrianMauck\AppData\Local\Temp\playwright_google_session.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--start-maximized"])
    context = browser.new_context(no_viewport=True, storage_state=SESSION_PATH)
    page = context.new_page()
    page.goto(DOC_URL, wait_until="domcontentloaded")
    time.sleep(6)
    # ... do work ...
    context.close()
    browser.close()
```

### Adding an inline anchored comment to a Google Doc
```python
import subprocess, time

def clip(text):
    subprocess.run("clip", input=text.encode("utf-8"), shell=True)
    time.sleep(0.3)

def add_comment(page, search_text, comment_text):
    # Find the text
    page.keyboard.press("Control+f")
    time.sleep(1.2)
    clip(search_text)
    page.keyboard.press("Control+a")
    page.keyboard.press("Control+v")
    time.sleep(1.5)
    # Close find — cursor lands at found text
    page.keyboard.press("Escape")
    time.sleep(0.8)
    # Open comment box
    page.keyboard.press("Control+Alt+m")
    time.sleep(2)
    # Paste and submit
    clip(comment_text)
    page.keyboard.press("Control+v")
    time.sleep(0.5)
    page.keyboard.press("Control+Enter")
    time.sleep(3)
```

### Hard-won lessons — do NOT repeat these failed paths
- ❌ **Do not** use `launch_persistent_context` with Brian's Chrome User Data dir — Chrome blocks CDP on the default profile (exit code 21)
- ❌ **Do not** try to kill Chrome and copy the profile — background processes hold locks
- ❌ **Do not** try to read/decrypt Chrome cookies directly — Chrome 127+ uses App-Bound (v20) encryption that cannot be bypassed from Python
- ❌ **Do not** call the Chrome ElevationService COM interface — it verifies callers are Chrome itself
- ✅ The saved session JSON is the only reliable auth path. If expired, run the one-time login block.
