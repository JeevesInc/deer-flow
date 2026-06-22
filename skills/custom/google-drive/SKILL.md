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
- Without `--folder`: uploads to "DeerFlow Output" — **NEVER do this. Brian does not use DeerFlow Output (explicit correction, 2026-06-11).** ALWAYS pass `--folder <ID>` with the correct destination from the jeeves-capital-markets folder map. If you do not know the right folder, ask Brian — do not default.
- With `--folder`: uploads directly to the specified Drive folder
- Makes it viewable by anyone with the link
- Prints the shareable link

**IMPORTANT:** Always upload generated files to Google Drive and share the link in your response. Do NOT just say the file was created — the user cannot access local files.

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
