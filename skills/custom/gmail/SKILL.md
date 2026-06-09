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
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/gmail_tool.py search "search query"
```
Uses Gmail search syntax. Examples:
- `"from:someone@example.com"` -- emails from a person
- `"subject:invoice"` -- emails with "invoice" in the subject
- `"is:unread newer_than:7d"` -- unread emails from the last 7 days

### Read a specific email
```bash
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/gmail_tool.py read <message_id>
```

### Download attachments from an email
```bash
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/gmail_tool.py download <message_id>
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/gmail_tool.py download <message_id> --output-dir C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/a61530b3-9207-4c31-a2b3-997ce0f93cac/user-data/outputs
```

### Draft a reply to an email
```bash
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/gmail_tool.py draft <message_id> "Your reply text here"
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/gmail_tool.py draft <message_id> "See attached" --attach C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/a61530b3-9207-4c31-a2b3-997ce0f93cac/user-data/outputs/report.xlsx
```

### Draft a new email
```bash
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/gmail_tool.py draft-new "recipient@email.com" "Subject line" "Body text"
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/gmail_tool.py draft-new "recipient@email.com" "Q1 Report" "See attached." --attach C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/a61530b3-9207-4c31-a2b3-997ce0f93cac/user-data/outputs/report.xlsx
```

## Attachments

The --attach flag accepts:
- Local files: `C:/Jeeves/redshift-bot/deer-flow/backend/.deer-flow/threads/a61530b3-9207-4c31-a2b3-997ce0f93cac/user-data/outputs/filename.xlsx`
- Google Drive files: `drive:<FILE_ID>` (auto-exported for Sheets/Docs/Slides)

## Background Crons

Three autonomous cron scripts run on schedule:

### Email Monitor (`email_monitor_cron.py`)
Every 15 min. Watches inbound email, filters noise, alerts via Slack DM, dispatches
actionable items (diligence, data requests) to the agent automatically.

### Dreams Cron (`dreams_cron.py`)
Every 12 hours. Scheduled reflection and consolidation inspired by Anthropic's agent
"dreams" concept. Runs in two phases:

**Phase 1 -- Memory consolidation (Step 0):**
- Reads STRATEGIC_CONTEXT.md (the live memory store)
- Fetches last 10 LangGraph session transcripts
- Deduplicates, resolves contradictions, absolutizes dates, prunes stale entries,
  promotes stable facts to permanent sections, fills blank sections
- Applies the consolidated version directly to STRATEGIC_CONTEXT.md (no proposal
  step — just write it)
- Uploads a backup copy to Google Drive and posts summary + Drive link to Brian's
  Slack DM as FYI only. DO NOT ask Brian to approve or discard anything.

**Phase 2 -- Scan and patch (Steps 1-4):**
- Scans Gmail + Slack for strategic signals
- Scans Gemini meeting notes
- Patches skill gaps, logs improvement episodes
- Surfaces one latent insight

**When Brian says "approve dream" or "discard dream":**
Run immediately:
```bash
uv run python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/approve_dream.py apply    # approve
uv run python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/approve_dream.py discard  # discard
uv run python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/approve_dream.py diff     # show diff
uv run python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/approve_dream.py status   # check if pending
```
Then confirm to Brian in Slack/chat that it was applied (or discarded).

Run manually: `uv run python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/dreams_cron.py once`
Configure: DREAMS_INTERVAL_HOURS env var (default: 12)

### EOD Review Cron (`eod_review_cron.py`)
Daily at 5 PM. Proactively surfaces everything directed at Brian that is unhandled,
and drafts ready-to-send responses so he can clear the day in under 15 minutes:
- Scans Gmail for unanswered threads where Brian is last recipient
- Scans Slack for unanswered DMs and @-mentions (Brian ID: U05B5HGNCN9 — NOT U09PQTZ5DHC, which is the bot's own user_id)
- Checks calendar for open action items from today's meetings
- Flags lender pipeline items (BBVA, CIM, NB, etc.) gone quiet >3 business days
- EOD reviews should surface open items as a briefing ONLY — no Gmail drafts unless Brian explicitly asks. Brian deletes the drafts. Just tell him what's open.
- Posts structured EOD briefing (HIGH/MEDIUM/LOW triage) to Slack DM
- Saves full summary to Google Drive

Run manually: `uv run python C:/Jeeves/redshift-bot/deer-flow/skills/custom/gmail/eod_review_cron.py once`
Configure: EOD_REVIEW_HOUR env var (default: 17 = 5 PM)

All crons use autonomous_dispatch.py and respect MAX_CONCURRENT_RUNS.

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
