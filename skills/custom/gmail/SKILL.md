---
name: gmail
description: Use this skill when the user asks about emails, wants to search their inbox, read an email, or draft a reply. Also use when the user says "email", "inbox", "draft", "reply to", or references a specific email thread.
allowed-tools:
  - bash
  - read_file
---

# Gmail — Search, Read, and Draft Emails

Access the user's Gmail to search messages, read email content, and create draft replies.

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

### Draft a new email
```bash
python /mnt/skills/custom/gmail/gmail_tool.py draft-new "recipient@email.com" "Subject line" "Email body text"
```

## Rules

- ALWAYS search first to find the relevant email before reading or replying
- NEVER send emails directly — only create drafts for the user to review
- When drafting, write professional and concise text appropriate for the context
- Show the user what you drafted and confirm it's in their Drafts folder
- If a search returns no results, suggest alternative search terms
