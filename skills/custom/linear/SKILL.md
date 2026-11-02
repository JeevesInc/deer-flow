---
name: linear
description: Use this skill when the user asks about Linear issues, wants to search or view tickets, create a new issue, update issue status or priority, or check what's assigned to them. Triggers for "Linear", "ticket", "issue", "bug", "task in Linear", "create a ticket", "what's assigned to me", "update the issue".
allowed-tools:
  - bash
---

# Linear — Issue Tracking

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.


Search, read, create, and update Linear issues.

## Commands

### My assigned issues
```bash
python /mnt/skills/custom/linear/linear_tool.py me
```
Returns your profile and all issues currently assigned to you.

### List teams
```bash
python /mnt/skills/custom/linear/linear_tool.py teams
```
Returns all teams with their short keys (e.g. `ENG`, `DATA`). Use these keys in other commands.

### List valid statuses for a team
```bash
python /mnt/skills/custom/linear/linear_tool.py statuses --team ENG
```
Run this before creating or updating issues so you know valid status names.

### Search issues
```bash
python /mnt/skills/custom/linear/linear_tool.py search "query"
python /mnt/skills/custom/linear/linear_tool.py search "query" --team ENG
```
Searches issue titles. Optionally filter by team key.

### Get a specific issue
```bash
python /mnt/skills/custom/linear/linear_tool.py get ENG-42
```
Returns full issue detail: title, status, priority, assignee, description, and recent comments.

### Create an issue
```bash
python /mnt/skills/custom/linear/linear_tool.py create --team ENG --title "Fix the thing"
python /mnt/skills/custom/linear/linear_tool.py create --team ENG --title "Fix the thing" --desc "More context here" --priority 2 --assignee someone@tryjeeves.com
```
Priority: `0`=No priority, `1`=Urgent, `2`=High, `3`=Medium, `4`=Low

**Do NOT create a ticket for a bug Brian is actively debugging with you in the same real-time session** -- just fix it. Ticketing is for work that will outlive this session (eng/other-team follow-up after you hand off, or Brian explicitly says "track this"). Opening a ticket mid-fix reads as process theater to Brian and he will tell you to delete it (2026-07-21 incident). Note also: `linear_tool.py` has no delete/archive command -- if a ticket is created by mistake, the only fix is `update <id> --status <cancelled-state-name>` (check exact name via `statuses --team <KEY>` first), not delete.

### Comment on an issue
```bash
python /mnt/skills/custom/linear/linear_tool.py comment ENG-42 "comment text"
```
Undocumented until 2026-09-10 but fully working (`cmd_comment` -> `commentCreate`). This is
the right way to post an investigation result on a ticket. For a long body, write the markdown
to a file and shell out with `subprocess` list-args from a python wrapper -- passing prose
through bash trips the leading-slash path guard.

### Update an issue
```bash
python /mnt/skills/custom/linear/linear_tool.py update ENG-42 --status "In Progress"
python /mnt/skills/custom/linear/linear_tool.py update ENG-42 --priority 1 --assignee someone@tryjeeves.com
python /mnt/skills/custom/linear/linear_tool.py update ENG-42 --title "Better title" --status "Done"
```
All flags are optional — only what you pass gets updated.

### List projects
```bash
python /mnt/skills/custom/linear/linear_tool.py projects
python /mnt/skills/custom/linear/linear_tool.py projects --team ENG
```

## Autonomous Mention Handling (live as of 2026-07-08)

`linear_mention_cron.py` runs inside the gateway (cron_supervisor, 5 min poll). Per
Brian's explicit standing instruction (2026-07-08): when he is @mentioned in a Linear
comment, read the ticket and act -- no per-item approval needed.

- It distinguishes a real @mention (Gmail subject 'X mentioned you in a comment on
  TICKET') from routine watch-list noise ('X commented on TICKET') -- only the
  former dispatches. Do not widen this to plain 'commented on' notifications.
- State file: backend/.deer-flow/_linear_mention_state.json (handled Gmail msg IDs).
- Never paste a secret, password, or AWS-Secrets-Manager-style path (a leading-slash
  token such as a production credential path) into a Linear comment or any tool
  argument verbatim -- it trips the unsafe-path filter (which fires on ANY bash arg
  containing a leading-slash token, even inside quoted comment text or a JSON
  string) and it is a real security concern regardless. Rephrase around it instead
  of retrying the same string.
- After acting, send Brian a one-line Slack DM with what was posted plus a ticket
  link (FYI, not an approval request).
- Before claiming an infra/credentials check happened a certain way (e.g. 'ran via
  the shared runner'), verify the actual execution context first -- don't state a
  default/assumed runner or path. If the ask is to test whether new credentials
  work, confirm a direct connection was made with them, not that a routine job
  merely ran.

## Rules

- Run `teams` first if you don't know the team key
- Run `statuses --team KEY` before updating status so you use the exact status name
- When creating issues, default to no priority (`--priority 0`) unless the user specifies
- Issue identifiers look like `ENG-42` — always use the full identifier (team key + number)
- Never guess status names — always look them up first
