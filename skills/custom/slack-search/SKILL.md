---
name: slack-search
description: Use this skill to search Slack messages, look up users by email, find recent conversations with someone, or send a DM as the bot. Triggers for "search slack", "what did X say", "slack messages from", "find in slack", "who said", "slack history", "DM <person>", "message <person> on Slack", "send <person> a Slack".
allowed-tools:
  - bash
---

# Slack — Search, Lookup, and Send DMs

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.


Search the Slack workspace using a user token (xoxp) with full search access.

## Commands

### Search messages
```bash
python /mnt/skills/custom/slack-search/slack_tool.py search "query" --days 30 --count 20
```
Uses Slack search syntax — same as the Slack search bar. Examples:
- `"from:@alexander budget"` — messages from a person about a topic
- `"in:#capital-markets term sheet"` — messages in a specific channel
- `"has:link fasanara"` — messages with links about a topic
- `"to:me action items"` — messages sent to you

`--days` limits how far back to search (default: 30). `--count` sets max results (default: 20).

### Look up user by email
```bash
python /mnt/skills/custom/slack-search/slack_tool.py lookup alex@tryjeeves.com
```
Returns the user's Slack ID, display name, title, and timezone. Useful for finding someone's Slack handle before searching their messages.

### Send a DM as the bot
```bash
python /mnt/skills/custom/slack-search/slack_tool.py send U02V79LCE7N "Hey Anish — NB confirmed Wed 5/20 3 pm ET."
python /mnt/skills/custom/slack-search/slack_tool.py send shalom@tryjeeves.com "Need the trial balances for RBC 8958 to close out CBIZ."
```
Sends a DM **as the bot's identity** (deerflow_analyst), not as Brian. The recipient is either a Slack user_id (UXXX/WXXX) or an email — emails are auto-resolved via `users.lookupByEmail`. Every send appends a `[Slack outbound]` line to `bot_dm_history.log` for the owner's audit dashboard, so you do NOT need to write any ad-hoc Python to call `chat_postMessage` directly — that approach bypasses the audit trail.

### Send a reply AS Brian (approval queue)
```bash
# Approve a drafted reply by its queue-card ts (reads target + draft from the proposal log):
python /mnt/skills/custom/slack-search/slack_tool.py send-as-owner --from-proposal 1782845760.433329
# Or send explicit text as Brian to a conversation:
python /mnt/skills/custom/slack-search/slack_tool.py send-as-owner --channel D0123ABCD "Sounds good — Friday works."
```
Sends **as Brian's own identity** (user token), not the bot. This is how the analyst executes an approved Slack reply that the `owner-slack-draft` cron drafted.

**Approval-queue convention:** the cron posts reply-draft cards to the pool channel; each card carries its own approval one-liner (`send-as-owner --from-proposal <ts>`). When Brian replies **"send it"** (or equivalent) in a card's thread, run that one-liner to send the draft verbatim as him. If he gives edits instead, compose the revised reply and send with `send-as-owner --channel <target> "<revised text>"` (the target id is in the card footer). Only ever send after Brian's explicit approval in the thread.

## Rules

- Search and lookup use the **user token** (xoxp); send uses the **bot token** (xoxb); **send-as-owner** uses the user token to post as Brian.
- Always try `lookup` first if you need to search by a person's name and only have their email.
- Slack search syntax supports: `from:`, `to:`, `in:`, `has:`, `before:`, `after:`, `during:`, and boolean operators. Results are sorted newest-first by default.
- **Do not write your own `chat_postMessage` scripts.** Always use `slack_tool.py send` — it logs every send to the owner's audit log. Ad-hoc Python skips the log and makes the dashboard miss real activity.
- The `send` command will refuse an empty message body. Quote messages that contain shell special characters.
- When the user asks you to send a draft, send it verbatim — do not paraphrase. If you need to clarify the tone or wording, ask the user before sending.

## Inbound DM allowlist (who gets responses)

The DM monitor only responds to senders on the allowlist (`backend/.deer-flow/_slack_dm_allowlist.json`).
Anyone NOT on the list gets **no reply and no agent dispatch** — Brian gets a Slack notification instead
and decides whether to allow them. The loader fails closed (owner-only) if the file is unreadable.

```bash
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/slack-search/slack_tool.py allowlist list
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/slack-search/slack_tool.py allowlist add shalom@tryjeeves.com
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/slack-search/slack_tool.py allowlist remove U037LV70CHY
```

**Rules:**
- When Brian tells you to message someone new for substantive work, add them to the allowlist as part of that task.
- Do NOT auto-add on every `send` — e.g., sending someone a one-off note (like telling Narendra to route requests through Brian) does not authorize them.
- Only Brian authorizes additions. If a gated sender's message looks important, surface it to Brian — never reply directly.
