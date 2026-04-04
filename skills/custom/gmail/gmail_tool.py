#!/usr/bin/env python3
"""Gmail tool: search emails, read messages, and create draft replies with attachments.

Usage:
    python gmail_tool.py search "query"           # Search emails
    python gmail_tool.py read <message_id>         # Read a specific email
    python gmail_tool.py draft <message_id> "body" [--attach file1 [--attach file2]]
    python gmail_tool.py draft-new "to" "subject" "body" [--attach file1 [--attach file2]]

Attachments can be:
    - Local file paths: /mnt/user-data/outputs/report.xlsx
    - Google Drive file IDs: drive:1LocDOgKKjQ4xs9bBRtkq_VvBTiCqmcMj

Requires env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""

import base64
import email.utils
import mimetypes
import os
import re
import sys
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
from google_auth import get_credentials as _get_creds


def _get_service():
    from googleapiclient.discovery import build
    return build('gmail', 'v1', credentials=_get_creds())


def _get_drive_service():
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=_get_creds())


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


def _resolve_attachment(path_or_id):
    """Resolve an attachment source to (filename, content_bytes, mime_type).

    Supports:
      - Local file paths (absolute or /mnt/ virtual paths)
      - Google Drive file IDs prefixed with 'drive:'
    """
    if path_or_id.startswith('drive:'):
        # Download from Google Drive
        file_id = path_or_id[6:]
        drive = _get_drive_service()
        try:
            meta = drive.files().get(fileId=file_id, fields='name,mimeType').execute()
        except Exception as e:
            print(f"ERROR: Cannot access Drive file {file_id}: {e}", file=sys.stderr)
            sys.exit(1)

        filename = meta.get('name', 'attachment')
        file_mime = meta.get('mimeType', '')

        # Google Workspace types need export
        export_map = {
            'application/vnd.google-apps.spreadsheet': ('text/csv', '.csv'),
            'application/vnd.google-apps.document': ('application/pdf', '.pdf'),
            'application/vnd.google-apps.presentation': ('application/pdf', '.pdf'),
        }

        if file_mime in export_map:
            export_mime, ext = export_map[file_mime]
            content = drive.files().export(fileId=file_id, mimeType=export_mime).execute()
            if not filename.endswith(ext):
                filename += ext
            return filename, content, export_mime
        else:
            content = drive.files().get_media(fileId=file_id).execute()
            return filename, content, file_mime or 'application/octet-stream'
    else:
        # Local file
        if not os.path.exists(path_or_id):
            print(f"ERROR: File not found: {path_or_id}", file=sys.stderr)
            sys.exit(1)
        filename = os.path.basename(path_or_id)
        with open(path_or_id, 'rb') as f:
            content = f.read()
        mime_type, _ = mimetypes.guess_type(filename)
        return filename, content, mime_type or 'application/octet-stream'


def _build_mime(body_text, from_addr, to_addr, subject, attachments=None,
                in_reply_to=None, references=None):
    """Build a MIME message, with attachments if provided."""
    if attachments:
        mime = MIMEMultipart('mixed')
        mime.attach(MIMEText(body_text))

        for att_source in attachments:
            filename, content, content_type = _resolve_attachment(att_source)
            maintype, subtype = content_type.split('/', 1) if '/' in content_type else ('application', 'octet-stream')
            part = MIMEBase(maintype, subtype)
            part.set_payload(content)
            from email.encoders import encode_base64
            encode_base64(part)
            part.add_header('Content-Disposition', 'attachment', filename=filename)
            mime.attach(part)
            print(f"  Attached: {filename} ({content_type}, {len(content) / 1024:.1f} KB)")
    else:
        mime = MIMEText(body_text)

    mime['to'] = to_addr
    mime['from'] = from_addr
    mime['subject'] = subject
    if in_reply_to:
        mime['In-Reply-To'] = in_reply_to
        mime['References'] = references or in_reply_to

    return mime


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


def cmd_draft_reply(message_id, body_text, attachments=None):
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

    profile = service.users().getProfile(userId='me').execute()
    my_email = profile['emailAddress']

    mime = _build_mime(
        body_text, my_email, reply_to, subject,
        attachments=attachments,
        in_reply_to=orig_message_id,
        references=orig_message_id,
    )

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
    if attachments:
        print(f"  Attachments: {len(attachments)}")
    print(f"\nThe draft is now in your Gmail Drafts folder. Review and send when ready.")


def cmd_draft_new(to, subject, body_text, attachments=None):
    service = _get_service()

    profile = service.users().getProfile(userId='me').execute()
    my_email = profile['emailAddress']

    mime = _build_mime(body_text, my_email, to, subject, attachments=attachments)

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode('utf-8')

    draft = service.users().drafts().create(
        userId='me',
        body={'message': {'raw': raw}}
    ).execute()

    print(f"Draft created successfully!")
    print(f"  Draft ID: {draft['id']}")
    print(f"  To:       {to}")
    print(f"  Subject:  {subject}")
    if attachments:
        print(f"  Attachments: {len(attachments)}")
    print(f"\nThe draft is now in your Gmail Drafts folder. Review and send when ready.")


def _parse_attachments(args):
    """Extract --attach values from remaining args."""
    attachments = []
    i = 0
    remaining = []
    while i < len(args):
        if args[i] == '--attach' and i + 1 < len(args):
            attachments.append(args[i + 1])
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    return remaining, attachments or None


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
            print('Usage: python gmail_tool.py draft <message_id> "reply body" [--attach file]', file=sys.stderr)
            sys.exit(1)
        _, attachments = _parse_attachments(sys.argv[4:])
        cmd_draft_reply(sys.argv[2], sys.argv[3], attachments=attachments)

    elif command == 'draft-new':
        if len(sys.argv) < 5:
            print('Usage: python gmail_tool.py draft-new "to" "subject" "body" [--attach file]', file=sys.stderr)
            sys.exit(1)
        _, attachments = _parse_attachments(sys.argv[5:])
        cmd_draft_new(sys.argv[2], sys.argv[3], sys.argv[4], attachments=attachments)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
