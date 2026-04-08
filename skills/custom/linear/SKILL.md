---
name: linear
description: Use this skill when the user asks about Linear issues, wants to search or view tickets, create a new issue, update issue status or priority, or check what's assigned to them. Triggers for "Linear", "ticket", "issue", "bug", "task in Linear", "create a ticket", "what's assigned to me", "update the issue".
allowed-tools:
  - bash
---

# Linear — Issue Tracking

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

## Rules

- Run `teams` first if you don't know the team key
- Run `statuses --team KEY` before updating status so you use the exact status name
- When creating issues, default to no priority (`--priority 0`) unless the user specifies
- Issue identifiers look like `ENG-42` — always use the full identifier (team key + number)
- Never guess status names — always look them up first
