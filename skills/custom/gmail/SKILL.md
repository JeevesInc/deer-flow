---
name: gmail
description: Use this skill when the user asks about emails, wants to search their inbox, read an email, download attachments, or draft a reply. Also use when the user says "email", "inbox", "draft", "reply to", "attachment", "download", or references a specific email thread.
allowed-tools:
  - bash
  - read_file
---

# Gmail -- Search, Read, and Draft Emails

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source. Never guess, assume, extrapolate, or fill gaps with general knowledge. Mark unverified items as **[Needs Confirmation]**.

Access the user's Gmail to search messages, read email content, and create draft replies with attachments.

## Commands

### Search emails
```bash
python /mnt/skills/custom/gmail/gmail_tool.py search "search query"
```
Uses Gmail search syntax. Examples:
- `"from:someone@example.com"` -- emails from a person
- `"subject:invoice"` -- emails with "invoice" in the subject
- `"is:unread newer_than:7d"` -- unread emails from the last 7 days

**Search returns AT MOST 10 results with no pagination.** A broad query that returns exactly 10 is probably truncated -- re-run with narrower slices (per-day `after:`/`before:`, per-sender) and union the results.

### Read a specific email
```bash
python /mnt/skills/custom/gmail/gmail_tool.py read <message_id>
```

### Download attachments
```bash
python /mnt/skills/custom/gmail/gmail_tool.py download <message_id>
python /mnt/skills/custom/gmail/gmail_tool.py download <message_id> --output-dir /mnt/user-data/outputs
```

### Draft a reply
```bash
python /mnt/skills/custom/gmail/gmail_tool.py draft <message_id> "Your reply text here"
python /mnt/skills/custom/gmail/gmail_tool.py draft <message_id> "See attached" --attach /mnt/user-data/outputs/report.xlsx
```

### Draft a new email
```bash
python /mnt/skills/custom/gmail/gmail_tool.py draft-new "recipient@email.com" "Subject line" "Body text"
python /mnt/skills/custom/gmail/gmail_tool.py draft-new "recipient@email.com" "Q1 Report" "See attached." --attach /mnt/user-data/outputs/report.xlsx
```

## Attachments

The `--attach` flag accepts:
- Local files (use `/mnt/user-data/outputs/...` -- never hard-code a `threads/<uuid>` path)
- Google Drive files: `drive:<FILE_ID>` (auto-exported for Sheets/Docs/Slides)

## Inbound email pipeline (what actually runs)

**`email_monitor_cron.py` is DISABLED (since 2026-05-20).** Inbound Gmail flows through: Gmail watch -> Pub/Sub -> ngrok -> `webhook_receiver.py` -> Haiku classifier -> Slack proposal DM to Brian. Nothing auto-dispatches from email. If "no proposals are coming in", first check the Gmail watch renewal (`gmail-watch-renew` cron in the gateway) -- an expired watch was a 33-day silent outage once.

If Brian asks to tweak dispatch config: `python /mnt/skills/custom/gmail/dispatch_config_tool.py show` (also `add-keyword`, `toggle`, `add-counterparty`, `set max-concurrent-runs`).

## Background crons (gateway daemon threads)

These live in this skill directory but run inside the gateway via `cron_supervisor.py`:

- **Dreams (`dreams_cron.py`)** -- once nightly in the 2-8am PT window. Behavior/infra review + silent memory consolidation (STRATEGIC_CONTEXT.md, SOUL.md size budget). Applies changes directly -- there is NO approve/discard step. Messages Brian ONLY if it found real problems; a silent dream is a successful dream. Manual run: `uv run python dreams_cron.py once`.
- **EOD review (`eod_review_cron.py`)** -- daily from 17:00 (first tick past the hour). Surfaces unhandled items as a HIGH/MEDIUM/LOW briefing to Brian's Slack DM. **Briefing only -- never creates Gmail drafts** (Brian deletes them). Manual: `uv run python eod_review_cron.py once`.
- **Weekly open items (`weekly_open_items_cron.py`)** -- Monday 08:00 (catches up after downtime). Sweeps the past 7 days for threads still awaiting Brian, live Gmail + Slack only.
- **Proposal learner (`proposal_learner.py`)** -- runs inside the EOD cycle; pairs webhook proposals with Brian's replies and stores classifier patterns in mem0 (`proposal-patterns` namespace).

## Rules

- ALWAYS search first to find the relevant email before reading or replying
- NEVER send emails directly -- only create drafts for the user to review
- ALWAYS reply-all by default unless Brian explicitly says otherwise
- The draft command handles reply-all automatically (fetches To/Cc, excludes Brian)
- If someone is mentioned in the body but not CC'd, look up their email via Slack and add them
- When drafting, match Brian's style: direct, short, key points up front
- Show what you drafted and confirm it is in the Drafts folder
- Prefer drive:<ID> for files already on Drive rather than re-uploading
- CLASSIFIER RULE -- reply-all threads: When a message is a reply-all and does NOT explicitly name Brian, check who the last active Jeeves-side sender was before the reply. If another Jeeves employee (e.g. Jorge Hurrle, Isabel Diaz) sent the preceding message, the reply is most likely directed at THEM -- classify as FYI_ONLY for Brian. Only flag ACTIONABLE for Brian if: (a) he is explicitly named, (b) the ask is in his exclusive domain with no other Jeeves person more directly involved, or (c) the reply is responding to a message Brian himself sent. Being CC'd for visibility ≠ being the action owner.
- **Never paste a list of more than ~5 items (customer names, account IDs, row data, etc.) inline in an email body.** Attach a file instead. Violated on 2026-07-01 (25 customers pasted inline, Brian: "that was ugly af") despite already being a standing rule -- treat as hard, not stylistic, for any draft with list-shaped content.
