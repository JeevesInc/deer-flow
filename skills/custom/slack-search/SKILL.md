---
name: slack-search
description: Use this skill to search Slack messages, look up users by email, or find recent conversations with someone. Triggers for "search slack", "what did X say", "slack messages from", "find in slack", "who said", "slack history".
allowed-tools:
  - bash
---

# Slack Search — Find Messages and Users

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

## Rules

- This tool uses a **user token** (xoxp), not a bot token — it can search all messages the user has access to
- Always try `lookup` first if you need to search by a person's name and only have their email
- Slack search syntax supports: `from:`, `to:`, `in:`, `has:`, `before:`, `after:`, `during:`, and boolean operators
- Results are sorted newest-first by default
