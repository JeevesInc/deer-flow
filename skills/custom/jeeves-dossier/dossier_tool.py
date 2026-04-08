#!/usr/bin/env python3
"""Contact dossier tool: gather interaction data, read/write dossier JSON files.

Usage:
    python dossier_tool.py gather <email> [--days 30]
    python dossier_tool.py read <email>
    python dossier_tool.py write <email> --file <path>
    python dossier_tool.py list
    python dossier_tool.py prep <event_id|"next">

Requires env vars:
  - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN (Calendar, Gmail, Drive)
  - SLACK_USER_TOKEN (Slack search)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Load .env before anything else
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
from env_loader import load_env
load_env()


# ---------------------------------------------------------------------------
# Dossier storage path
# ---------------------------------------------------------------------------

def _dossier_dir():
    """Return the dossier storage directory, creating it if needed."""
    base = os.environ.get('DOSSIER_PATH', '')
    if not base:
        # Default: .deer-flow/dossiers/ relative to the backend dir
        backend = os.path.dirname(os.path.abspath(__file__))
        # Walk up to find .deer-flow
        candidate = os.path.join(backend, '..', '..', '..', 'backend', '.deer-flow', 'dossiers')
        base = os.path.normpath(candidate)
    os.makedirs(base, exist_ok=True)
    return base


def _dossier_path(email):
    """Return the path to a dossier JSON file for a given email."""
    safe = email.replace('@', '_at_').replace('.', '_')
    return os.path.join(_dossier_dir(), f'{safe}.json')


# ---------------------------------------------------------------------------
# Google API helpers (shared with other skills)
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
from google_auth import get_credentials as _get_google_creds_required


def _get_google_creds():
    return _get_google_creds_required(required=False)


def _get_google_service(api, version):
    creds = _get_google_creds()
    if not creds:
        return None
    from googleapiclient.discovery import build
    return build(api, version, credentials=creds)


# ---------------------------------------------------------------------------
# Slack helper
# ---------------------------------------------------------------------------

def _get_slack_client():
    token = os.environ.get('SLACK_USER_TOKEN')
    if not token:
        return None
    try:
        from slack_sdk import WebClient
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'slack_sdk'])
        from slack_sdk import WebClient
    return WebClient(token=token)


def _slack_user_id_from_email(client, email):
    """Look up Slack user ID from email. Returns None on failure."""
    try:
        result = client.users_lookupByEmail(email=email)
        return result.get('user', {}).get('id')
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Data gatherers
# ---------------------------------------------------------------------------

def gather_calendar(email, days):
    """Gather shared calendar events with this contact."""
    service = _get_google_service('calendar', 'v3')
    if not service:
        return []

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days)).isoformat()
    time_max = now.isoformat()

    try:
        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
            maxResults=100,
            q=email,  # Search by attendee email
        ).execute()
    except Exception as e:
        return [{"error": str(e)[:200]}]

    events = events_result.get('items', [])
    results = []
    for event in events:
        attendees = event.get('attendees', [])
        attendee_emails = [a.get('email', '') for a in attendees]
        # Only include if this contact is actually an attendee
        if not any(email.lower() in ae.lower() for ae in attendee_emails):
            continue

        start = event.get('start', {})
        start_dt = start.get('dateTime', start.get('date', ''))

        results.append({
            "date": start_dt[:10] if start_dt else '',
            "title": event.get('summary', '(No title)'),
            "attendees": [a.get('email', '') for a in attendees if not a.get('self')],
            "event_id": event.get('id', ''),
        })

    return results


def gather_gmail(email, days):
    """Gather email threads with this contact."""
    service = _get_google_service('gmail', 'v1')
    if not service:
        return []

    query = f"(from:{email} OR to:{email}) newer_than:{days}d"

    try:
        results = service.users().messages().list(
            userId='me', q=query, maxResults=30
        ).execute()
    except Exception as e:
        return [{"error": str(e)[:200]}]

    messages = results.get('messages', [])
    if not messages:
        return []

    output = []
    for msg_ref in messages[:30]:
        try:
            msg = service.users().messages().get(
                userId='me', id=msg_ref['id'], format='metadata',
                metadataHeaders=['From', 'To', 'Subject', 'Date']
            ).execute()
        except Exception:
            continue

        headers = msg.get('payload', {}).get('headers', [])
        header_map = {}
        for h in headers:
            header_map[h['name'].lower()] = h['value']

        output.append({
            "date": header_map.get('date', ''),
            "subject": header_map.get('subject', ''),
            "from": header_map.get('from', ''),
            "to": header_map.get('to', ''),
            "snippet": msg.get('snippet', '')[:200],
            "thread_id": msg.get('threadId', ''),
        })

    return output


def gather_slack(email, days):
    """Gather Slack messages involving this contact."""
    client = _get_slack_client()
    if not client:
        return []

    # Look up user ID from email
    user_id = _slack_user_id_from_email(client, email)
    if not user_id:
        return []

    after_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    query = f"from:<@{user_id}> after:{after_date}"

    try:
        result = client.search_messages(query=query, count=30, sort='timestamp', sort_dir='desc')
    except Exception as e:
        return [{"error": str(e)[:200]}]

    messages = result.get('messages', {}).get('matches', [])
    output = []
    for msg in messages:
        ts = msg.get('ts', '')
        try:
            dt = datetime.fromtimestamp(float(ts))
            date_str = dt.strftime('%Y-%m-%d %H:%M')
        except (ValueError, OSError):
            date_str = ts

        channel_name = msg.get('channel', {}).get('name', 'unknown')
        channel_is_im = msg.get('channel', {}).get('is_im', False)
        text = msg.get('text', '')
        if len(text) > 500:
            text = text[:500] + '...'

        output.append({
            "date": date_str,
            "channel": 'DM' if channel_is_im else f'#{channel_name}',
            "sender": msg.get('username', '') or msg.get('user', ''),
            "text": text,
        })

    return output


def gather_gemini_notes(email, days):
    """Gather Gemini meeting notes from Google Drive that mention this contact."""
    service = _get_google_service('drive', 'v3')
    if not service:
        return []

    after_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00')

    try:
        results = service.files().list(
            q=f"name contains 'Notes by Gemini' and modifiedTime > '{after_date}' and mimeType = 'application/vnd.google-apps.document'",
            fields='files(id,name,modifiedTime)',
            orderBy='modifiedTime desc',
            pageSize=50,
        ).execute()
    except Exception as e:
        return [{"error": str(e)[:200]}]

    files = results.get('files', [])
    if not files:
        return []

    # Extract the contact's name from the email (before @, capitalize)
    name_part = email.split('@')[0]
    # Try common patterns: first.last, first_last, firstlast
    name_variants = set()
    name_variants.add(name_part.lower())
    if '.' in name_part:
        parts = name_part.split('.')
        name_variants.update(p.lower() for p in parts)
        name_variants.add(' '.join(parts).lower())
    if '_' in name_part:
        parts = name_part.split('_')
        name_variants.update(p.lower() for p in parts)
        name_variants.add(' '.join(parts).lower())

    output = []
    for file in files[:20]:  # Limit to 20 docs to avoid quota issues
        try:
            content = service.files().export(fileId=file['id'], mimeType='text/plain').execute()
            text = content.decode('utf-8') if isinstance(content, bytes) else content
        except Exception:
            continue

        text_lower = text.lower()
        # Check if any name variant appears in the document
        if not any(v in text_lower for v in name_variants if len(v) > 2):
            continue

        # Extract relevant paragraphs (lines mentioning the contact)
        relevant = []
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if any(v in line.lower() for v in name_variants if len(v) > 2):
                # Include surrounding context (1 line before, 1 after)
                start = max(0, i - 1)
                end = min(len(lines), i + 2)
                snippet = '\n'.join(lines[start:end]).strip()
                if snippet and snippet not in relevant:
                    relevant.append(snippet)

        if relevant:
            output.append({
                "date": file.get('modifiedTime', '')[:10],
                "title": file.get('name', ''),
                "relevant_excerpts": relevant[:10],  # Limit excerpts
            })

    return output


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_gather(email, days=30):
    """Gather raw interaction data from all sources for one contact."""
    data = {
        "email": email,
        "gathered_at": datetime.now().isoformat(),
        "days_back": days,
        "calendar": gather_calendar(email, days),
        "gmail": gather_gmail(email, days),
        "slack": gather_slack(email, days),
        "gemini_notes": gather_gemini_notes(email, days),
    }

    # Summary stats
    stats = []
    for source in ('calendar', 'gmail', 'slack', 'gemini_notes'):
        items = data[source]
        count = len([i for i in items if 'error' not in i])
        errors = len([i for i in items if 'error' in i])
        if errors:
            stats.append(f"  {source}: {count} items ({errors} errors)")
        else:
            stats.append(f"  {source}: {count} items")

    print(f"Gathered data for {email} (last {days} days):")
    for s in stats:
        print(s)
    print()
    print(json.dumps(data, indent=2, default=str))


def cmd_read(email):
    """Read an existing dossier JSON file."""
    path = _dossier_path(email)
    if not os.path.exists(path):
        print(f"No dossier found for {email}")
        print(f"Expected path: {path}")
        return

    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print(f"Dossier for {email} (last updated: {data.get('last_updated', 'unknown')}):")
    print()
    print(json.dumps(data, indent=2, default=str))


def cmd_write(email, file_path):
    """Save a dossier JSON file."""
    if not os.path.exists(file_path):
        print(f"ERROR: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Ensure required fields
    data['email'] = email
    data['last_updated'] = datetime.now().isoformat()

    dest = _dossier_path(email)
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)

    print(f"Dossier saved for {email}")
    print(f"  Path: {dest}")
    print(f"  Updated: {data['last_updated']}")


def cmd_list():
    """List all existing dossiers."""
    dossier_dir = _dossier_dir()
    files = [f for f in os.listdir(dossier_dir) if f.endswith('.json')]

    if not files:
        print("No dossiers found.")
        print(f"  Directory: {dossier_dir}")
        return

    print(f"Found {len(files)} dossier(s):\n")
    for fname in sorted(files):
        path = os.path.join(dossier_dir, fname)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            email = data.get('email', fname.replace('.json', ''))
            name = data.get('name', '')
            updated = data.get('last_updated', 'unknown')
            health = data.get('relationship', {}).get('health_score', '?')
            trend = data.get('relationship', {}).get('trend', '?')
            label = f"{name} ({email})" if name else email
            print(f"  {label}")
            print(f"    Health: {health}/10 ({trend}) | Last updated: {updated}")
        except Exception:
            print(f"  {fname} (error reading)")
    print()


def cmd_prep(event_ref):
    """Get meeting attendees for prep. Returns list of emails to process."""
    service = _get_google_service('calendar', 'v3')
    if not service:
        print("ERROR: Google Calendar not configured.", file=sys.stderr)
        sys.exit(1)

    # The user's email — never build a dossier for themselves
    my_email = os.environ.get('GOOGLE_CALENDAR_EMAIL', 'brian.mauck@tryjeeves.com').lower()

    if event_ref.lower() == 'next':
        # Find the next upcoming event with attendees
        now = datetime.now(timezone.utc)
        time_max = (now + timedelta(days=7)).isoformat()

        events_result = service.events().list(
            calendarId='primary',
            timeMin=now.isoformat(),
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
            maxResults=20,
        ).execute()

        events = events_result.get('items', [])
        target_event = None
        for event in events:
            attendees = event.get('attendees', [])
            others = [a for a in attendees
                      if not a.get('self') and a.get('email', '').lower() != my_email]
            if len(others) >= 1:
                target_event = event
                break

        if not target_event:
            print("No upcoming meetings with attendees found in the next 7 days.")
            return
    else:
        # Fetch specific event by ID
        try:
            target_event = service.events().get(
                calendarId='primary', eventId=event_ref
            ).execute()
        except Exception as e:
            print(f"ERROR: Could not fetch event {event_ref}: {e}", file=sys.stderr)
            sys.exit(1)

    # Extract meeting info
    summary = target_event.get('summary', '(No title)')
    start = target_event.get('start', {})
    start_dt = start.get('dateTime', start.get('date', ''))
    attendees = target_event.get('attendees', [])
    # Filter out self AND the user's own email
    others = [a for a in attendees
              if not a.get('self') and a.get('email', '').lower() != my_email]

    print(f"User: Brian Mauck ({my_email})")
    print(f"Meeting: {summary}")
    print(f"Time:    {start_dt}")
    print(f"Event ID: {target_event.get('id', '')}")
    print()

    if not others:
        print("No other attendees found (only you).")
        return

    print(f"Attendees to prep ({len(others)}):")
    for a in others:
        email = a.get('email', '')
        status = a.get('responseStatus', 'unknown')
        dossier_exists = os.path.exists(_dossier_path(email))
        marker = '[has dossier]' if dossier_exists else '[new]'
        print(f"  {email} ({status}) {marker}")

    print()
    print("Emails (excluding you):")
    for a in others:
        print(a.get('email', ''))


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    if len(sys.argv) < 2:
        print("Usage: python dossier_tool.py <command> [args]")
        print("Commands: gather, read, write, list, prep")
        sys.exit(1)

    command = sys.argv[1]

    if command == 'gather':
        if len(sys.argv) < 3:
            print('Usage: python dossier_tool.py gather <email> [--days 30]', file=sys.stderr)
            sys.exit(1)
        email = sys.argv[2]
        days = 30
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == '--days' and i + 1 < len(sys.argv):
                days = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        cmd_gather(email, days=days)

    elif command == 'read':
        if len(sys.argv) < 3:
            print('Usage: python dossier_tool.py read <email>', file=sys.stderr)
            sys.exit(1)
        cmd_read(sys.argv[2])

    elif command == 'write':
        if len(sys.argv) < 3:
            print('Usage: python dossier_tool.py write <email> --file <path>', file=sys.stderr)
            sys.exit(1)
        email = sys.argv[2]
        file_path = None
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == '--file' and i + 1 < len(sys.argv):
                file_path = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        if not file_path:
            print('Usage: python dossier_tool.py write <email> --file <path>', file=sys.stderr)
            sys.exit(1)
        cmd_write(email, file_path)

    elif command == 'list':
        cmd_list()

    elif command == 'prep':
        if len(sys.argv) < 3:
            print('Usage: python dossier_tool.py prep <event_id|"next">', file=sys.stderr)
            sys.exit(1)
        cmd_prep(sys.argv[2])

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
