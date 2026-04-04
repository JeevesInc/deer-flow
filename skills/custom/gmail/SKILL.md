---
name: gmail
description: Use this skill when the user asks about emails, wants to search their inbox, read an email, or draft a reply. Also use when the user says "email", "inbox", "draft", "reply to", or references a specific email thread.
allowed-tools:
  - bash
  - read_file
---

# Gmail — Search, Read, and Draft Emails

Access the user's Gmail to search messages, read email content, and create draft replies with attachments.

## Commands

### Search emails
```bash
python /mnt/skills/custom/gmail/gmail_tool.py search "search query"
```
Uses Gmail search syntax — same as the Gmail search bar. Examples:
- `"from:someone@example.com"` — emails from a person
- `"subject:invoice"` — emails with "invoice" in the subject
- `"is:unread newer_than:7d"` — unread emails from the last 7 days
- `"from:cfo@company.com subject:budget"` — combine filters

### Read a specific email
```bash
python /mnt/skills/custom/gmail/gmail_tool.py read <message_id>
```
Returns full email content (headers + body). Use message IDs from search results.

### Draft a reply to an email
```bash
python /mnt/skills/custom/gmail/gmail_tool.py draft <message_id> "Your reply text here"
```
Creates a draft reply in Gmail's Drafts folder, properly threaded. The user must review and send manually.

Add attachments with `--attach`:
```bash
python /mnt/skills/custom/gmail/gmail_tool.py draft <message_id> "See attached" \
    --attach /mnt/user-data/outputs/report.xlsx \
    --attach drive:1LocDOgKKjQ4xs9bBRtkq_VvBTiCqmcMj
```

### Draft a new email
```bash
python /mnt/skills/custom/gmail/gmail_tool.py draft-new "recipient@email.com" "Subject line" "Email body text"
```

With attachments:
```bash
python /mnt/skills/custom/gmail/gmail_tool.py draft-new "recipient@email.com" "Q1 Report" "Please find attached." \
    --attach /mnt/user-data/outputs/report.xlsx
```

## Attachments

The `--attach` flag accepts two formats:
- **Local files**: `/mnt/user-data/outputs/filename.xlsx` — any file the agent has generated or downloaded
- **Google Drive files**: `drive:<FILE_ID>` — downloads the file from Drive and attaches it. Google Workspace files (Sheets, Docs, Slides) are exported as CSV/PDF automatically.

Multiple `--attach` flags can be used to attach several files.

## Rules

- ALWAYS search first to find the relevant email before reading or replying
- NEVER send emails directly — only create drafts for the user to review
- When drafting, write professional and concise text appropriate for the context
- Show the user what you drafted and confirm it's in their Drafts folder
- If a search returns no results, suggest alternative search terms
- When attaching files, prefer using `drive:<ID>` for files already on Drive rather than downloading and re-uploading
