#!/usr/bin/env python3
"""Google Calendar tool: list events, search, create, update, delete, find free time.

Usage:
    python calendar_tool.py list [--days N] [--cal ID]
    python calendar_tool.py today
    python calendar_tool.py search "query" [--days N]
    python calendar_tool.py create "Title" --start "YYYY-MM-DDTHH:MM" --end "YYYY-MM-DDTHH:MM" [--attendees a@x.com,b@x.com] [--description "..."] [--location "..."] [--meet]
    python calendar_tool.py free --date YYYY-MM-DD [--duration 30] [--start-hour 9] [--end-hour 18]
    python calendar_tool.py find-time --attendees a@x.com,b@x.com --date YYYY-MM-DD [--duration 30] [--start-hour 9] [--end-hour 18]
    python calendar_tool.py get <event_id>
    python calendar_tool.py delete <event_id>
    python calendar_tool.py update <event_id> [--title "..."] [--start "..."] [--end "..."] [--attendees "..."] [--description "..."] [--location "..."]
    python calendar_tool.py calendars

Requires env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone


TIMEZONE = "America/Los_Angeles"


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
from google_auth import get_credentials as _get_creds


def _get_service():
    from googleapiclient.discovery import build
    return build('calendar', 'v3', credentials=_get_creds())


def _format_dt(dt_str, all_day=False):
    """Format an event datetime string for display."""
    if all_day:
        dt = datetime.strptime(dt_str, "%Y-%m-%d")
        return dt.strftime("%A, %B %d"), "All day"
    dt = datetime.fromisoformat(dt_str)
    return dt.strftime("%A, %B %d"), dt.strftime("%H:%M")


def _format_event(event, show_date=True):
    """Format a single event for display."""
    start = event.get('start', {})
    end = event.get('end', {})
    all_day = 'date' in start

    start_str = start.get('dateTime', start.get('date', ''))
    end_str = end.get('dateTime', end.get('date', ''))

    if all_day:
        day_label, time_label = _format_dt(start_str, all_day=True)
        time_range = "All day"
    else:
        day_label, start_time = _format_dt(start_str)
        _, end_time = _format_dt(end_str)
        time_range = f"{start_time}-{end_time}"

    summary = event.get('summary', '(No title)')
    lines = []

    lines.append(f"{time_range}  {summary}")

    # Location
    location = event.get('location')
    if location:
        lines.append(f"             Location: {location}")

    # Google Meet link
    hangout = event.get('hangoutLink')
    if hangout:
        lines.append(f"             Google Meet: {hangout}")

    # Conference data (for newer meet links)
    conf = event.get('conferenceData', {})
    for ep in conf.get('entryPoints', []):
        if ep.get('entryPointType') == 'video' and not hangout:
            lines.append(f"             Google Meet: {ep.get('uri', '')}")

    # Attendees
    attendees = event.get('attendees', [])
    if attendees:
        names = [a.get('email', '') for a in attendees if not a.get('self')]
        if names:
            lines.append(f"             Attendees: {', '.join(names)}")

    return day_label, '\n'.join(lines)


def cmd_list(args):
    service = _get_service()
    days = args.days or 7
    cal_id = args.cal or 'primary'

    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(TIMEZONE)
    except ImportError:
        tz = timezone(timedelta(hours=-7))

    now = datetime.now(tz)
    time_max = now + timedelta(days=days)

    events_result = service.events().list(
        calendarId=cal_id,
        timeMin=now.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy='startTime',
        maxResults=100,
    ).execute()

    events = events_result.get('items', [])
    if not events:
        print(f"No events in the next {days} day(s).")
        return

    print(f"Events for the next {days} day(s):\n")
    current_day = None
    for event in events:
        day_label, formatted = _format_event(event)
        if day_label != current_day:
            current_day = day_label
            print(f"=== {day_label} ===")
        print(formatted)
    print()


def cmd_today(args):
    service = _get_service()

    # Compute today's boundaries in the local timezone, not UTC.
    # Without this, UTC midnight != local midnight, and events from
    # yesterday evening (local) bleed into "today."
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(TIMEZONE)
    except ImportError:
        tz = timezone(timedelta(hours=-7))

    local_now = datetime.now(tz)
    start_of_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    events_result = service.events().list(
        calendarId='primary',
        timeMin=start_of_day.isoformat(),
        timeMax=end_of_day.isoformat(),
        singleEvents=True,
        orderBy='startTime',
        maxResults=50,
    ).execute()

    events = events_result.get('items', [])
    if not events:
        print("No events today.")
        return

    today_label = local_now.strftime("%A, %B %d")
    print(f"=== {today_label} ===")
    for event in events:
        _, formatted = _format_event(event, show_date=False)
        print(formatted)
    print()


def cmd_search(args):
    service = _get_service()
    query = args.query
    days = args.days or 30

    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(TIMEZONE)
    except ImportError:
        tz = timezone(timedelta(hours=-7))

    now = datetime.now(tz)
    time_max = now + timedelta(days=days)

    events_result = service.events().list(
        calendarId='primary',
        q=query,
        timeMin=now.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy='startTime',
        maxResults=50,
    ).execute()

    events = events_result.get('items', [])
    if not events:
        print(f"No events matching '{query}' in the next {days} day(s).")
        return

    print(f"Found {len(events)} event(s) matching '{query}':\n")
    current_day = None
    for event in events:
        day_label, formatted = _format_event(event)
        if day_label != current_day:
            current_day = day_label
            print(f"=== {day_label} ===")
        print(formatted)
    print()


def cmd_create(args):
    service = _get_service()

    event_body = {
        'summary': args.title,
        'start': {
            'dateTime': _parse_local_dt(args.start),
            'timeZone': TIMEZONE,
        },
        'end': {
            'dateTime': _parse_local_dt(args.end),
            'timeZone': TIMEZONE,
        },
    }

    if args.description:
        event_body['description'] = args.description
    if args.location:
        event_body['location'] = args.location
    if args.attendees:
        event_body['attendees'] = [{'email': e.strip()} for e in args.attendees.split(',')]

    conf_version = 0
    if args.meet:
        import uuid
        event_body['conferenceData'] = {
            'createRequest': {
                'requestId': str(uuid.uuid4()),
                'conferenceSolutionKey': {'type': 'hangoutsMeet'},
            }
        }
        conf_version = 1

    event = service.events().insert(
        calendarId='primary',
        body=event_body,
        conferenceDataVersion=conf_version,
        sendUpdates='all',
    ).execute()

    print("Event created successfully!")
    print(f"  Title:    {event.get('summary')}")
    print(f"  Start:    {event['start'].get('dateTime', event['start'].get('date'))}")
    print(f"  End:      {event['end'].get('dateTime', event['end'].get('date'))}")
    print(f"  Event ID: {event['id']}")
    if event.get('hangoutLink'):
        print(f"  Meet:     {event['hangoutLink']}")
    print(f"  Link:     {event.get('htmlLink', '')}")


def _parse_local_dt(dt_str):
    """Parse a datetime string and return ISO format with timezone."""
    # If already has timezone info, return as-is
    if '+' in dt_str or dt_str.endswith('Z'):
        return dt_str
    # Otherwise, assume local (LA) time — append offset placeholder
    # The API will interpret it with the timeZone field
    return dt_str + ":00"


def cmd_free(args):
    service = _get_service()
    date = args.date
    duration = args.duration or 30
    start_hour = args.start_hour or 9
    end_hour = args.end_hour or 18

    time_min = f"{date}T{start_hour:02d}:00:00"
    time_max = f"{date}T{end_hour:02d}:00:00"

    body = {
        "timeMin": _to_utc_iso(time_min, TIMEZONE),
        "timeMax": _to_utc_iso(time_max, TIMEZONE),
        "timeZone": TIMEZONE,
        "items": [{"id": "primary"}],
    }

    result = service.freebusy().query(body=body).execute()
    busy_periods = result.get('calendars', {}).get('primary', {}).get('busy', [])

    free_slots = _compute_free_slots(busy_periods, date, start_hour, end_hour, duration)

    if not free_slots:
        print(f"No free slots of {duration}+ minutes on {date} between {start_hour:02d}:00 and {end_hour:02d}:00.")
        return

    print(f"Free slots on {date} ({duration}+ min):\n")
    for slot_start, slot_end in free_slots:
        print(f"  {slot_start} - {slot_end}")
    print()


def cmd_find_time(args):
    service = _get_service()
    attendees = [e.strip() for e in args.attendees.split(',')]
    date = args.date
    duration = args.duration or 30
    start_hour = args.start_hour or 9
    end_hour = args.end_hour or 18

    time_min = f"{date}T{start_hour:02d}:00:00"
    time_max = f"{date}T{end_hour:02d}:00:00"

    body = {
        "timeMin": _to_utc_iso(time_min, TIMEZONE),
        "timeMax": _to_utc_iso(time_max, TIMEZONE),
        "timeZone": TIMEZONE,
        "items": [{"id": email} for email in attendees],
    }

    result = service.freebusy().query(body=body).execute()

    # Merge all busy periods across all attendees
    all_busy = []
    calendars = result.get('calendars', {})
    for email in attendees:
        cal_data = calendars.get(email, {})
        errors = cal_data.get('errors', [])
        if errors:
            print(f"Warning: Could not check availability for {email}: {errors[0].get('reason', 'unknown')}")
            continue
        all_busy.extend(cal_data.get('busy', []))

    free_slots = _compute_free_slots(all_busy, date, start_hour, end_hour, duration)

    if not free_slots:
        print(f"No shared free slots of {duration}+ minutes on {date} between {start_hour:02d}:00 and {end_hour:02d}:00.")
        print(f"Attendees checked: {', '.join(attendees)}")
        return

    print(f"Shared free slots on {date} ({duration}+ min):")
    print(f"Attendees: {', '.join(attendees)}\n")
    for slot_start, slot_end in free_slots:
        print(f"  {slot_start} - {slot_end}")
    print()


def _to_utc_iso(local_dt_str, tz_name):
    """Convert a local datetime string to UTC ISO format for the API."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name)
    except ImportError:
        # Fallback: just append a fixed offset for LA
        # PT is UTC-7 (PDT) or UTC-8 (PST)
        return local_dt_str + "-07:00"

    dt = datetime.strptime(local_dt_str, "%Y-%m-%dT%H:%M:%S")
    dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).isoformat()


def _compute_free_slots(busy_periods, date, start_hour, end_hour, min_duration):
    """Compute free time slots given a list of busy periods."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(TIMEZONE)
    except ImportError:
        tz = timezone(timedelta(hours=-7))

    day_start = datetime.strptime(f"{date}T{start_hour:02d}:00:00", "%Y-%m-%dT%H:%M:%S")
    day_end = datetime.strptime(f"{date}T{end_hour:02d}:00:00", "%Y-%m-%dT%H:%M:%S")

    if hasattr(tz, 'key'):
        day_start = day_start.replace(tzinfo=tz)
        day_end = day_end.replace(tzinfo=tz)
    else:
        day_start = day_start.replace(tzinfo=tz)
        day_end = day_end.replace(tzinfo=tz)

    # Parse and sort busy periods
    busy = []
    for period in busy_periods:
        b_start = datetime.fromisoformat(period['start'])
        b_end = datetime.fromisoformat(period['end'])
        busy.append((b_start, b_end))

    busy.sort(key=lambda x: x[0])

    # Merge overlapping busy periods
    merged = []
    for b_start, b_end in busy:
        if merged and b_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b_end))
        else:
            merged.append((b_start, b_end))

    # Find free slots
    free_slots = []
    current = day_start

    for b_start, b_end in merged:
        if current < b_start:
            gap_minutes = (b_start - current).total_seconds() / 60
            if gap_minutes >= min_duration:
                free_slots.append((current.strftime("%H:%M"), b_start.strftime("%H:%M")))
        current = max(current, b_end)

    # Check remaining time after last busy period
    if current < day_end:
        gap_minutes = (day_end - current).total_seconds() / 60
        if gap_minutes >= min_duration:
            free_slots.append((current.strftime("%H:%M"), day_end.strftime("%H:%M")))

    return free_slots


def cmd_get(args):
    service = _get_service()
    event = service.events().get(calendarId='primary', eventId=args.event_id).execute()

    start = event.get('start', {})
    end = event.get('end', {})

    print(f"Title:       {event.get('summary', '(No title)')}")
    print(f"Start:       {start.get('dateTime', start.get('date', 'N/A'))}")
    print(f"End:         {end.get('dateTime', end.get('date', 'N/A'))}")
    print(f"Status:      {event.get('status', 'N/A')}")
    print(f"Event ID:    {event.get('id')}")
    print(f"Organizer:   {event.get('organizer', {}).get('email', 'N/A')}")

    if event.get('location'):
        print(f"Location:    {event['location']}")
    if event.get('hangoutLink'):
        print(f"Google Meet: {event['hangoutLink']}")
    if event.get('description'):
        print(f"Description: {event['description'][:500]}")

    attendees = event.get('attendees', [])
    if attendees:
        print(f"Attendees:")
        for a in attendees:
            status = a.get('responseStatus', 'unknown')
            print(f"  {a.get('email', 'N/A')} ({status})")

    print(f"Link:        {event.get('htmlLink', 'N/A')}")


def cmd_delete(args):
    service = _get_service()
    service.events().delete(
        calendarId='primary',
        eventId=args.event_id,
        sendUpdates='all',
    ).execute()
    print(f"Event {args.event_id} deleted successfully.")


def cmd_update(args):
    service = _get_service()

    # Fetch current event
    event = service.events().get(calendarId='primary', eventId=args.event_id).execute()

    if args.title:
        event['summary'] = args.title
    if args.description:
        event['description'] = args.description
    if args.location:
        event['location'] = args.location
    if args.start:
        event['start'] = {'dateTime': _parse_local_dt(args.start), 'timeZone': TIMEZONE}
    if args.end:
        event['end'] = {'dateTime': _parse_local_dt(args.end), 'timeZone': TIMEZONE}
    if args.attendees:
        event['attendees'] = [{'email': e.strip()} for e in args.attendees.split(',')]

    updated = service.events().update(
        calendarId='primary',
        eventId=args.event_id,
        body=event,
        sendUpdates='all',
    ).execute()

    print(f"Event updated successfully!")
    print(f"  Title:    {updated.get('summary')}")
    print(f"  Start:    {updated['start'].get('dateTime', updated['start'].get('date'))}")
    print(f"  End:      {updated['end'].get('dateTime', updated['end'].get('date'))}")
    print(f"  Event ID: {updated['id']}")
    print(f"  Link:     {updated.get('htmlLink', '')}")


def cmd_calendars(args):
    service = _get_service()
    result = service.calendarList().list().execute()
    calendars = result.get('items', [])

    if not calendars:
        print("No calendars found.")
        return

    print(f"Found {len(calendars)} calendar(s):\n")
    for cal in calendars:
        primary = " (primary)" if cal.get('primary') else ""
        print(f"  {cal.get('summary', '(unnamed)')}{primary}")
        print(f"    ID: {cal['id']}")
        if cal.get('description'):
            print(f"    Description: {cal['description'][:100]}")
        print()


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description='Google Calendar tool')
    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # list
    p_list = subparsers.add_parser('list', help='List upcoming events')
    p_list.add_argument('--days', type=int, default=7, help='Number of days to look ahead (default: 7)')
    p_list.add_argument('--cal', type=str, default=None, help='Calendar ID (default: primary)')

    # today
    subparsers.add_parser('today', help="List today's events")

    # search
    p_search = subparsers.add_parser('search', help='Search events')
    p_search.add_argument('query', help='Search query')
    p_search.add_argument('--days', type=int, default=30, help='Days to search ahead (default: 30)')

    # create
    p_create = subparsers.add_parser('create', help='Create an event')
    p_create.add_argument('title', help='Event title')
    p_create.add_argument('--start', required=True, help='Start time (YYYY-MM-DDTHH:MM)')
    p_create.add_argument('--end', required=True, help='End time (YYYY-MM-DDTHH:MM)')
    p_create.add_argument('--attendees', help='Comma-separated email addresses')
    p_create.add_argument('--description', help='Event description')
    p_create.add_argument('--location', help='Event location')
    p_create.add_argument('--meet', action='store_true', help='Add a Google Meet link')

    # free
    p_free = subparsers.add_parser('free', help='Find free time slots')
    p_free.add_argument('--date', required=True, help='Date to check (YYYY-MM-DD)')
    p_free.add_argument('--duration', type=int, default=30, help='Minimum slot duration in minutes (default: 30)')
    p_free.add_argument('--start-hour', type=int, default=9, help='Start of working day (default: 9)')
    p_free.add_argument('--end-hour', type=int, default=18, help='End of working day (default: 18)')

    # find-time
    p_find = subparsers.add_parser('find-time', help='Find shared free time for multiple attendees')
    p_find.add_argument('--attendees', required=True, help='Comma-separated email addresses')
    p_find.add_argument('--date', required=True, help='Date to check (YYYY-MM-DD)')
    p_find.add_argument('--duration', type=int, default=30, help='Minimum slot duration in minutes (default: 30)')
    p_find.add_argument('--start-hour', type=int, default=9, help='Start of working day (default: 9)')
    p_find.add_argument('--end-hour', type=int, default=18, help='End of working day (default: 18)')

    # get
    p_get = subparsers.add_parser('get', help='Get event details')
    p_get.add_argument('event_id', help='Event ID')

    # delete
    p_del = subparsers.add_parser('delete', help='Delete an event')
    p_del.add_argument('event_id', help='Event ID')

    # update
    p_update = subparsers.add_parser('update', help='Update an event')
    p_update.add_argument('event_id', help='Event ID')
    p_update.add_argument('--title', help='New title')
    p_update.add_argument('--start', help='New start time (YYYY-MM-DDTHH:MM)')
    p_update.add_argument('--end', help='New end time (YYYY-MM-DDTHH:MM)')
    p_update.add_argument('--attendees', help='New comma-separated attendee emails')
    p_update.add_argument('--description', help='New description')
    p_update.add_argument('--location', help='New location')

    # calendars
    subparsers.add_parser('calendars', help='List all calendars')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        'list': cmd_list,
        'today': cmd_today,
        'search': cmd_search,
        'create': cmd_create,
        'free': cmd_free,
        'find-time': cmd_find_time,
        'get': cmd_get,
        'delete': cmd_delete,
        'update': cmd_update,
        'calendars': cmd_calendars,
    }

    try:
        commands[args.command](args)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
