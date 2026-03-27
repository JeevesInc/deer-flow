#!/usr/bin/env python3
"""Gmail tool: search emails, read messages, and create draft replies.

Usage:
    python gmail_tool.py search "query"           # Search emails
    python gmail_tool.py read <message_id>         # Read a specific email
    python gmail_tool.py draft <message_id> "body" # Draft a reply to an email
    python gmail_tool.py draft-new "to" "subject" "body"  # Draft a new email

Requires env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""

import base64
import email.utils
import os
import re
import sys
from email.mime.text import MIMEText


def _get_creds():
    for var in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN'):
        if not os.environ.get(var):
            print(f"ERROR: Missing environment variable {var}", file=sys.stderr)
            sys.exit(1)

    try:
        from google.oauth2.credentials import Credentials
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q',
                               'google-api-python-client', 'google-auth'])
        from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
    )


def _get_service():
    from googleapiclient.discovery import build
    return build('gmail', 'v1', credentials=_get_creds())


def _clean_html(html):
    """Strip HTML tags for plain-text display."""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?p[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _get_body(payload):
    """Extract plain text body from message payload."""
    if payload.get('mimeType') == 'text/plain' and payload.get('body', {}).get('data'):
        return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')

    if payload.get('mimeType') == 'text/html' and payload.get('body', {}).get('data'):
        html = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
        return _clean_html(html)

    for part in payload.get('parts', []):
        result = _get_body(part)
        if result:
            return result
    return ''


def _header(headers, name):
    for h in headers:
        if h['name'].lower() == name.lower():
            return h['value']
    return ''


def cmd_search(query, max_results=10):
    service = _get_service()
    results = service.users().messages().list(
        userId='me', q=query, maxResults=max_results
    ).execute()

    messages = results.get('messages', [])
    if not messages:
        print(f"No emails found for: {query}")
        return

    print(f"Found {len(messages)} email(s) for: {query}\n")

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId='me', id=msg_ref['id'], format='metadata',
            metadataHeaders=['From', 'To', 'Subject', 'Date']
        ).execute()
        headers = msg.get('payload', {}).get('headers', [])
        snippet = msg.get('snippet', '')
        print(f"ID: {msg_ref['id']}")
        print(f"  From:    {_header(headers, 'From')}")
        print(f"  To:      {_header(headers, 'To')}")
        print(f"  Subject: {_header(headers, 'Subject')}")
        print(f"  Date:    {_header(headers, 'Date')}")
        print(f"  Preview: {snippet[:120]}")
        print()


def cmd_read(message_id):
    service = _get_service()
    msg = service.users().messages().get(userId='me', id=message_id, format='full').execute()
    headers = msg.get('payload', {}).get('headers', [])

    print(f"From:    {_header(headers, 'From')}")
    print(f"To:      {_header(headers, 'To')}")
    print(f"Subject: {_header(headers, 'Subject')}")
    print(f"Date:    {_header(headers, 'Date')}")
    print(f"Thread:  {msg.get('threadId', 'N/A')}")
    print(f"---")

    body = _get_body(msg.get('payload', {}))
    if body:
        print(body[:5000])
    else:
        print("(no text body found)")


def cmd_draft_reply(message_id, body_text):
    service = _get_service()

    # Get the original message for threading
    orig = service.users().messages().get(userId='me', id=message_id, format='metadata',
                                          metadataHeaders=['From', 'To', 'Subject', 'Message-ID']).execute()
    orig_headers = orig.get('payload', {}).get('headers', [])
    thread_id = orig.get('threadId')

    reply_to = _header(orig_headers, 'From')
    subject = _header(orig_headers, 'Subject')
    if not subject.lower().startswith('re:'):
        subject = f"Re: {subject}"
    orig_message_id = _header(orig_headers, 'Message-ID')

    # Get authenticated user's email
    profile = service.users().getProfile(userId='me').execute()
    my_email = profile['emailAddress']

    # Build the MIME message
    mime = MIMEText(body_text)
    mime['to'] = reply_to
    mime['from'] = my_email
    mime['subject'] = subject
    if orig_message_id:
        mime['In-Reply-To'] = orig_message_id
        mime['References'] = orig_message_id

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode('utf-8')

    draft = service.users().drafts().create(
        userId='me',
        body={
            'message': {
                'raw': raw,
                'threadId': thread_id,
            }
        }
    ).execute()

    print(f"Draft created successfully!")
    print(f"  Draft ID: {draft['id']}")
    print(f"  Thread:   {thread_id}")
    print(f"  To:       {reply_to}")
    print(f"  Subject:  {subject}")
    print(f"\nThe draft is now in your Gmail Drafts folder. Review and send when ready.")


def cmd_draft_new(to, subject, body_text):
    service = _get_service()

    profile = service.users().getProfile(userId='me').execute()
    my_email = profile['emailAddress']

    mime = MIMEText(body_text)
    mime['to'] = to
    mime['from'] = my_email
    mime['subject'] = subject

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode('utf-8')

    draft = service.users().drafts().create(
        userId='me',
        body={'message': {'raw': raw}}
    ).execute()

    print(f"Draft created successfully!")
    print(f"  Draft ID: {draft['id']}")
    print(f"  To:       {to}")
    print(f"  Subject:  {subject}")
    print(f"\nThe draft is now in your Gmail Drafts folder. Review and send when ready.")


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    if len(sys.argv) < 2:
        print("Usage: python gmail_tool.py <command> [args]")
        print("Commands: search, read, draft, draft-new")
        sys.exit(1)

    command = sys.argv[1]

    if command == 'search':
        if len(sys.argv) < 3:
            print("Usage: python gmail_tool.py search \"query\"", file=sys.stderr)
            sys.exit(1)
        cmd_search(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 10)

    elif command == 'read':
        if len(sys.argv) < 3:
            print("Usage: python gmail_tool.py read <message_id>", file=sys.stderr)
            sys.exit(1)
        cmd_read(sys.argv[2])

    elif command == 'draft':
        if len(sys.argv) < 4:
            print("Usage: python gmail_tool.py draft <message_id> \"reply body\"", file=sys.stderr)
            sys.exit(1)
        cmd_draft_reply(sys.argv[2], sys.argv[3])

    elif command == 'draft-new':
        if len(sys.argv) < 5:
            print("Usage: python gmail_tool.py draft-new \"to@email\" \"subject\" \"body\"", file=sys.stderr)
            sys.exit(1)
        cmd_draft_new(sys.argv[2], sys.argv[3], sys.argv[4])

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
