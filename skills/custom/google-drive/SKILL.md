---
name: google-drive
description: Use this skill when the user shares a Google Drive, Google Docs, Google Sheets, or Google Slides link and wants you to read, summarize, or analyze the content. Also use when a message contains a docs.google.com or drive.google.com URL.
allowed-tools:
  - bash
  - read_file
---

# Google Drive Document Access

Read Google Docs, Sheets, and Slides shared via URL.

## Usage

Run the fetch script with any Google Drive URL:

```bash
python /mnt/skills/custom/google-drive/fetch_doc.py "<GOOGLE_DRIVE_URL>"
```

The script:
- Extracts the document ID from any Google URL format
- Authenticates using environment credentials
- Exports the document as plain text (Docs/Slides) or CSV (Sheets)
- Prints the full content to stdout

## Example

```bash
python /mnt/skills/custom/google-drive/fetch_doc.py "https://docs.google.com/document/d/1jXVx7ev95DIF1fp.../edit"
```

## Supported URL formats

- `https://docs.google.com/document/d/{ID}/edit`
- `https://docs.google.com/spreadsheets/d/{ID}/edit`
- `https://docs.google.com/presentation/d/{ID}/edit`
- `https://drive.google.com/file/d/{ID}/view`
- `https://drive.google.com/open?id={ID}`

## Rules

- Always show the document title before summarizing
- For large documents, summarize key points rather than echoing the full text
- If the script fails with a credentials error, tell the user to check their Google API setup
