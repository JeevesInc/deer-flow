---
name: google-calendar
description: Use this skill when the user asks about their calendar, schedule, meetings, availability, or wants to create/update/delete events. Also triggers for "what's on my calendar", "am I free", "schedule a meeting", "find a time", or "block my calendar".
allowed-tools:
  - bash
---

# Google Calendar — View, Create, and Manage Events

Access the user's Google Calendar to view events, find free time, and create or manage meetings.

## Commands

### List upcoming events
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py list --days 7
```
Shows events for the next N days (default: 7), grouped by day. Use `--cal <calendar_id>` to query a specific calendar.

### Today's events
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py today
```
Shortcut to show only today's events.

### Search events
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py search "quarterly review" --days 30
```
Search events by text query within the next N days (default: 30).

### Create an event
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py create "Team Standup" \
    --start "2026-03-30T09:00" \
    --end "2026-03-30T09:30" \
    --attendees "alice@tryjeeves.com,bob@tryjeeves.com" \
    --description "Daily sync" \
    --location "Conference Room A" \
    --meet
```
Creates an event. `--meet` adds a Google Meet link. `--attendees`, `--description`, `--location`, and `--meet` are optional.

### Find your free time
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py free --date 2026-03-30 --duration 30 --start-hour 9 --end-hour 18
```
Returns available time slots on a given date. Defaults: 30-min slots, 9 AM to 6 PM.

### Find shared free time for multiple people
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py find-time \
    --attendees "alice@tryjeeves.com,bob@tryjeeves.com" \
    --date 2026-03-30 \
    --duration 30 \
    --start-hour 9 \
    --end-hour 18
```
Uses the Google Calendar FreeBusy API to find time slots where ALL attendees are free. Works for any user in the `@tryjeeves.com` Google Workspace org.

### Get event details
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py get <event_id>
```
Returns full details including attendees, RSVP status, description, and Meet link.

### Update an event
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py update <event_id> \
    --title "Updated Title" \
    --start "2026-03-30T10:00" \
    --end "2026-03-30T11:00"
```
Update any combination of title, start, end, attendees, description, or location.

### Delete an event
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py delete <event_id>
```
Deletes the event and notifies attendees.

### List all calendars
```bash
python /mnt/skills/custom/google-calendar/calendar_tool.py calendars
```
Shows all calendars the user has access to, with their IDs.

## Rules

- Default timezone is **America/Los_Angeles** (California time)
- Always **confirm with the user before creating or deleting** events
- Group events by day when listing multiple days
- Always show Google Meet links when present
- For `find-time`, all attendees must be in the same Google Workspace (`@tryjeeves.com`)
- When the user says "schedule a meeting", ask for title, attendees, and preferred time before creating
- If a search returns no results, suggest broadening the date range or trying different terms
