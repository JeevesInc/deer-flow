---
name: google-drive
description: Use this skill when the user shares a Google Drive, Google Docs, Google Sheets, or Google Slides link and wants you to read, summarize, or analyze the content. Also use when a message contains a docs.google.com or drive.google.com URL. Also use this skill when you need to share a generated file (Excel, PowerPoint, CSV, PDF, etc.) with the user — upload it to Google Drive and share the link.
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Google Drive — Read & Upload

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
- Without `--folder`: creates a "DeerFlow Output" folder in the user's Google Drive (first time only) and uploads there
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
