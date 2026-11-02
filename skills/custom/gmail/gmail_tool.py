#!/usr/bin/env python3
"""Gmail tool: search emails, read messages, download attachments, and create draft replies.

Usage:
    python gmail_tool.py search "query"           # Search emails
    python gmail_tool.py read <message_id>         # Read a specific email
    python gmail_tool.py download <message_id> [--output-dir /path]  # Download attachments
    python gmail_tool.py draft <message_id> "body" [--to addr] [--cc "addr, addr"|--cc none] [--attach file1]
    python gmail_tool.py draft-new "to" "subject" "body" [--attach file1 [--attach file2]]
    python gmail_tool.py delete-draft <draft_id>   # Delete a draft

Recipient control on threaded replies (draft):
    By default a reply inherits ALL recipients of the original message
    (reply-all). On external/counterparty threads this can leak internal
    notes to the whole group. Pass --to/--cc to set recipients explicitly:
        --to "alexander@tryjeeves.com"     reply only to Alex
        --cc "shalom@tryjeeves.com"        explicit cc list
        --cc none                          no cc at all
    The draft stays on the same thread either way. ALWAYS check the
    RECIPIENTS line printed after creation before telling anyone it's ready.

Inline images (charts in the BODY, Brian's standing preference for anything
visual) — repeatable, optional ':cid' suffix, placed with [[IMAGE:<cid>]] in the
body text or appended at the end if the marker is absent:
    python gmail_tool.py draft <id> --body-file b.txt --inline /path/Chart.png
    ... where b.txt contains a line reading  [[IMAGE:Chart]]

Attachments can be:
    - Local file paths: /mnt/user-data/outputs/report.xlsx
    - Google Drive file IDs: drive:1LocDOgKKjQ4xs9bBRtkq_VvBTiCqmcMj

Requires env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""

import base64
import email.utils
import html as _html
import mimetypes
import os
import re
import sys
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
from google_auth import get_credentials as _get_creds

# Placeholder gate on outbound attachments. Imported unguarded and on purpose:
# a control that silently disables itself when its module goes missing is the
# exact failure mode it exists to prevent. If this import ever breaks, every
# attachment send fails loudly (and GMAIL_ATTACH_GATE=off is the escape hatch)
# rather than quietly shipping a half-filled instruction letter. See
# attachment_gate.py for the 2026-09-14 Anexo J incident that prompted it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from attachment_gate import AttachmentGateError, assert_sendable


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


def _text_to_html(body_text):
    """Render a plain-text body as simple HTML so mail clients display it at
    normal width with paragraph spacing preserved.

    Gmail renders a bare text/plain part in a narrow, fixed column and collapses
    blank lines. Sending an HTML alternative avoids that. Blank lines separate
    paragraphs; single newlines within a paragraph become <br>.
    """
    body_text = body_text.replace('\r\n', '\n').replace('\r', '\n')
    paragraphs = re.split(r'\n[ \t]*\n', body_text)
    blocks = []
    for para in paragraphs:
        if not para.strip('\n'):
            continue
        lines = [_html.escape(line) for line in para.split('\n')]
        blocks.append('<p style="margin:0 0 1em 0;">' + '<br>'.join(lines) + '</p>')
    inner = '\n'.join(blocks) if blocks else '<p style="margin:0;"></p>'
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'line-height:1.5;color:#222;white-space:normal;">\n'
        + inner + '\n</div>'
    )


# Inline-image placement marker. Put [[IMAGE:name]] on its own line in the body
# where the chart should appear; anything not referenced is appended at the end.
_IMG_MARKER = re.compile(r'\[\[IMAGE:([A-Za-z0-9._-]+)\]\]')


def _resolve_inline_images(specs):
    """specs: ['/path/chart.png', '/path/chart.png:forecast'] -> parts list.

    Returns [(cid, filename, content, subtype), ...]. The cid defaults to the
    filename stem, so [[IMAGE:<stem>]] in the body needs no extra arguments.
    """
    resolved = []
    for spec in specs or []:
        path, cid = spec, None
        # Only treat a trailing ':label' as a cid, never a Windows drive colon.
        if spec.count(':') and not spec.rsplit(':', 1)[1].startswith(('\\', '/')):
            head, tail = spec.rsplit(':', 1)
            if head and len(tail) > 1 and not tail[0].isdigit() and len(head) > 2:
                path, cid = head, tail
        with open(path, 'rb') as fh:
            content = fh.read()
        filename = os.path.basename(path)
        subtype = (mimetypes.guess_type(filename)[0] or 'image/png').split('/', 1)[1]
        cid = cid or re.sub(r'[^A-Za-z0-9._-]', '_', os.path.splitext(filename)[0])
        resolved.append((cid, filename, content, subtype))
    return resolved


def _build_body_part(body_text, inline_images=None):
    """multipart/alternative body; wrapped in multipart/related if inline images.

    Charts belong IN the body, not as attachments Brian has to open (standing
    preference, re-stated 2026-09-21: "Put the visuals in the body of the
    email"). Before this existed every inline-image email was a hand-rolled
    MIME script, which is how two of them crashed on 2026-09-21.
    """
    images = _resolve_inline_images(inline_images)

    text_body = _IMG_MARKER.sub(lambda m: f'[chart: {m.group(1)} — see HTML view]', body_text)
    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(text_body, 'plain', 'utf-8'))

    html = _text_to_html(body_text)
    if not images:
        alt.attach(MIMEText(html, 'html', 'utf-8'))
        return alt

    def _img_tag(cid):
        return (f'<img src="cid:{cid}" style="max-width:100%;height:auto;'
                'display:block;margin:12px 0;border:1px solid #e0e0e0;">')

    used = set()

    def _sub(match):
        cid = match.group(1)
        used.add(cid)
        return _img_tag(cid)

    html = _IMG_MARKER.sub(_sub, html)
    trailing = ''.join(_img_tag(cid) for cid, _, _, _ in images if cid not in used)
    if trailing:
        html = html.replace('</div>', trailing + '\n</div>') if html.endswith('</div>') else html + trailing
    alt.attach(MIMEText(html, 'html', 'utf-8'))

    related = MIMEMultipart('related')
    related.attach(alt)
    for cid, filename, content, subtype in images:
        part = MIMEImage(content, _subtype=subtype)
        part.add_header('Content-ID', f'<{cid}>')
        part.add_header('Content-Disposition', 'inline', filename=filename)
        related.attach(part)
        print(f"  Inline image: {filename} (cid:{cid}, {len(content) / 1024:.1f} KB)")
    return related


def _build_mime(body_text, from_addr, to_addr, subject, attachments=None,
                in_reply_to=None, references=None, cc=None, inline_images=None):
    """Build a MIME message, with attachments and/or inline images if provided."""
    if attachments:
        mime = MIMEMultipart('mixed')
        mime.attach(_build_body_part(body_text, inline_images=inline_images))

        for att_source in attachments:
            filename, content, content_type = _resolve_attachment(att_source)
            # Gate every attachment on every draft/send path, before any of it
            # is encoded into the message.
            assert_sendable(filename, content)
            maintype, subtype = content_type.split('/', 1) if '/' in content_type else ('application', 'octet-stream')
            part = MIMEBase(maintype, subtype)
            part.set_payload(content)
            from email.encoders import encode_base64
            encode_base64(part)
            part.add_header('Content-Disposition', 'attachment', filename=filename)
            mime.attach(part)
            print(f"  Attached: {filename} ({content_type}, {len(content) / 1024:.1f} KB)")
    else:
        mime = _build_body_part(body_text, inline_images=inline_images)

    # NOTE: Header names MUST be canonical-case (To/From/Subject/Cc). Gmail's
    # drafts().create silently DROPS lowercase header names ('to','subject',...)
    # when a threadId is attached, producing a blank, recipient-less draft that
    # is effectively invisible in the Drafts folder. Verified 2026-06-24.
    mime['To'] = to_addr
    mime['From'] = from_addr
    mime['Subject'] = subject
    if cc:
        mime['Cc'] = cc
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

    # HARD RULE: a request's attachments ARE part of the request. Surface them here so
    # a caller reading an email can never miss an attached template/workbook.
    # (2026-08-14: the Mastercard aging deliverable was built off a slide image while
    # Alex's Excel template sat attached to the same request email -> two corrections.)
    def _walk_atts(part, found):
        fn = part.get('filename') or ''
        body_meta = part.get('body', {}) or {}
        if fn and body_meta.get('attachmentId'):
            found.append((fn, body_meta.get('size', 0), part.get('mimeType', '')))
        for sub in part.get('parts', []) or []:
            _walk_atts(sub, found)
        return found

    atts = _walk_atts(msg.get('payload', {}), [])
    if atts:
        print(f"Attachments ({len(atts)}) -- download with: gmail_tool.py download {message_id}")
        for fn, size, mt in atts:
            print(f"  - {fn}  ({size} bytes, {mt})")
        print("---")

    body = _get_body(msg.get('payload', {}))
    if body:
        print(body[:5000])
    else:
        print("(no text body found)")


def cmd_draft_reply(message_id, body_text, attachments=None, to_override=None, cc_override=None,
                    inline_images=None):
    service = _get_service()

    # Get the original message for threading — fetch Cc too for reply-all
    orig = service.users().messages().get(userId='me', id=message_id, format='metadata',
                                          metadataHeaders=['From', 'To', 'Cc', 'Subject', 'Message-ID']).execute()
    orig_headers = orig.get('payload', {}).get('headers', [])
    thread_id = orig.get('threadId')

    orig_from = _header(orig_headers, 'From')
    orig_to   = _header(orig_headers, 'To')
    orig_cc   = _header(orig_headers, 'Cc')
    subject   = _header(orig_headers, 'Subject')
    if not subject.lower().startswith('re:'):
        subject = f"Re: {subject}"
    orig_message_id = _header(orig_headers, 'Message-ID')

    profile = service.users().getProfile(userId='me').execute()
    my_email = profile['emailAddress']

    def _parse_addrs(field):
        """Parse an RFC 2822 address list, handling quoted names with commas."""
        if not field:
            return []
        return [email.utils.formataddr(pair) for pair in email.utils.getaddresses([field]) if pair[1]]

    explicit = to_override is not None or cc_override is not None
    dropped = []
    if explicit:
        # Compute who the reply-all default would have included but the override drops.
        _inh = _parse_addrs(orig_from) + _parse_addrs(orig_to) + _parse_addrs(orig_cc)
        _kept = ' '.join([to_override or orig_from, cc_override or '']).lower()
        for a in _inh:
            addr = email.utils.parseaddr(a)[1].lower()
            if addr and addr != my_email.lower() and addr not in _kept and a not in dropped:
                dropped.append(a)
    if explicit:
        # Explicit recipients: --to/--cc fully control who receives the reply.
        to_str = to_override if to_override else orig_from
        if cc_override is None or cc_override.strip().lower() in ('', 'none'):
            cc_str = None
        else:
            cc_str = cc_override
        inherited = False
    else:
        # Reply-all default:
        # To = original From (always reply to sender)
        # Cc = everyone else on original To + Cc, minus ourselves
        to_str = orig_from
        all_recipients = _parse_addrs(orig_to) + _parse_addrs(orig_cc)
        cc_addrs = [a for a in all_recipients
                    if email.utils.parseaddr(a)[1].lower() != my_email.lower()
                    and email.utils.parseaddr(a)[1].lower() != email.utils.parseaddr(orig_from)[1].lower()]
        cc_str = ', '.join(cc_addrs) if cc_addrs else None
        inherited = True

    mime = _build_mime(
        body_text, my_email, to_str, subject,
        attachments=attachments,
        in_reply_to=orig_message_id,
        references=orig_message_id,
        cc=cc_str,
        inline_images=inline_images,
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
    print(f"  Subject:  {subject}")
    if attachments:
        print(f"  Attachments: {len(attachments)}")
    print()
    print("=" * 60)
    print("RECIPIENTS" + ("  (INHERITED from thread — every To/Cc on the" if inherited else "  (explicit via --to/--cc)"))
    if inherited:
        print("            original message gets this reply. This is the")
        print("            DEFAULT Brian wants -- only narrow it if he named recipients.)")
    print(f"  To: {to_str}")
    print(f"  Cc: {cc_str if cc_str else '(none)'}")
    if dropped:
        print("!! NARROWED REPLY -- these thread participants were DROPPED by --to/--cc:")
        for a in dropped:
            print(f"     - {a}")
        print("!! Brian's standing rule is reply-all. Unless he explicitly named these")
        print("!! recipients, delete-draft and redo WITHOUT --to/--cc. Report this list.")
    print("=" * 60)
    print(f"\nThe draft is now in your Gmail Drafts folder. Review and send when ready.")


def cmd_draft_new(to, subject, body_text, attachments=None, inline_images=None):
    service = _get_service()

    profile = service.users().getProfile(userId='me').execute()
    my_email = profile['emailAddress']

    mime = _build_mime(body_text, my_email, to, subject, attachments=attachments,
                       inline_images=inline_images)

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


def cmd_delete_draft(draft_id):
    service = _get_service()
    try:
        service.users().drafts().delete(userId='me', id=draft_id).execute()
        print(f"Draft {draft_id} deleted.")
    except Exception as e:
        if '404' in str(e):
            print(f"Draft {draft_id} not found (already deleted?).")
        else:
            print(f"ERROR deleting draft {draft_id}: {e}", file=sys.stderr)
            sys.exit(1)


def cmd_download(message_id, output_dir=None):
    if output_dir is None:
        output_dir = os.environ.get('WORKSPACE_PATH', '/mnt/user-data/workspace')
    os.makedirs(output_dir, exist_ok=True)

    service = _get_service()
    msg = service.users().messages().get(userId='me', id=message_id, format='full').execute()
    payload = msg.get('payload', {})

    def _find_attachments(part):
        """Recursively find parts that have an attachmentId."""
        found = []
        body = part.get('body', {})
        if body.get('attachmentId'):
            headers = {h['name'].lower(): h['value'] for h in part.get('headers', [])}
            disposition = headers.get('content-disposition', '')
            content_id = headers.get('content-id', '')
            # Skip inline images with a Content-ID (signature images, etc.)
            if 'inline' in disposition and content_id:
                return found
            found.append(part)
        for sub in part.get('parts', []):
            found.extend(_find_attachments(sub))
        return found

    parts = _find_attachments(payload)

    if not parts:
        print(f"No attachments found in message {message_id}")
        return

    for part in parts:
        filename = part.get('filename') or 'attachment'
        att_id = part['body']['attachmentId']
        att = service.users().messages().attachments().get(
            userId='me', id=att_id, messageId=message_id
        ).execute()
        data = base64.urlsafe_b64decode(att['data'])
        # Windows-safe destination name. Gmail returns the raw MIME filename,
        # which can carry path separators or characters illegal on NTFS
        # (colon, quote, pipe, question mark, asterisk, angle brackets, control
        # chars). Passing it straight to open() raised OSError twice on
        # 2026-09-04 (thread fe35484d), losing the attachment and forcing the
        # work to be done off screenshots instead.
        _raw = filename.replace(chr(92), '/').split('/')[-1]
        safe = re.sub(r'[<>:"|?*\x00-\x1f]', '_', _raw).strip().strip('.')
        if not safe:
            safe = 'attachment'
        dest = os.path.join(output_dir, safe)
        with open(dest, 'wb') as f:
            f.write(data)
        print(f"Saved: {dest} ({len(data)} bytes)")

    print(f"\nDownloaded {len(parts)} attachment(s) from message {message_id}")


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


def _parse_inline(args):
    """Extract --inline values (image embedded in the body) from remaining args."""
    inline = []
    i = 0
    remaining = []
    while i < len(args):
        if args[i] in ('--inline', '--inline-image') and i + 1 < len(args):
            inline.append(args[i + 1])
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    return remaining, inline or None


def _pop_flag(args, flag):
    """Extract a single '--flag value' pair from args. Returns (remaining, value|None)."""
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            value = args[idx + 1]
            return args[:idx] + args[idx + 2:], value
    return args, None


def main():
    # Owner-gated capability (2026-08-28). Non-owner Slack users may ask
    # data questions and work with files, but never touch Brian's private
    # surfaces. See skills/custom/_shared/actor_guard.py.
    import os as _og_os, sys as _og_sys
    _og_sys.path.insert(0, _og_os.path.join(_og_os.path.dirname(
        _og_os.path.dirname(_og_os.path.abspath(__file__))), '_shared'))
    from actor_guard import require_owner as _require_owner
    _require_owner("Gmail (Brian's mailbox)")

    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    if len(sys.argv) < 2:
        print("Usage: python gmail_tool.py <command> [args]")
        print("Commands: search, read, draft, draft-new, delete-draft, download")
        sys.exit(1)

    command = sys.argv[1]

    if command == 'search':
        if len(sys.argv) < 3:
            print("Usage: python gmail_tool.py search \"query\"", file=sys.stderr)
            sys.exit(1)
        _rest = sys.argv[3:]
        _n = 10
        for _i, _a in enumerate(_rest):
            if _a in ('--max', '-n', '--limit'):
                if _i + 1 < len(_rest) and _rest[_i + 1].lstrip('-').isdigit():
                    _n = int(_rest[_i + 1])
                break
            if _a.startswith('--max=') or _a.startswith('--limit='):
                _tail = _a.split('=', 1)[1]
                if _tail.isdigit():
                    _n = int(_tail)
                break
            if _a.isdigit():
                _n = int(_a)
                break
        cmd_search(sys.argv[2], _n)

    elif command == 'read':
        if len(sys.argv) < 3:
            print("Usage: python gmail_tool.py read <message_id>", file=sys.stderr)
            sys.exit(1)
        cmd_read(sys.argv[2])

    elif command == 'draft':
        if len(sys.argv) < 4:
            print('Usage: python gmail_tool.py draft <message_id> "reply body"|--body-file <path> [--to addr] [--cc "addrs"|--cc none] [--attach file]', file=sys.stderr)
            sys.exit(1)
        body_arg = sys.argv[3]
        if body_arg == '--body-file':
            if len(sys.argv) < 5:
                print('--body-file requires a path argument', file=sys.stderr)
                sys.exit(1)
            with open(sys.argv[4], 'r', encoding='utf-8') as _bf:
                body_text_arg = _bf.read()
            rest = sys.argv[5:]
        else:
            body_text_arg = body_arg
            rest = sys.argv[4:]
        rest, attachments = _parse_attachments(rest)
        rest, inline_images = _parse_inline(rest)
        rest, to_override = _pop_flag(rest, '--to')
        rest, cc_override = _pop_flag(rest, '--cc')
        cmd_draft_reply(sys.argv[2], body_text_arg, attachments=attachments,
                        to_override=to_override, cc_override=cc_override,
                        inline_images=inline_images)

    elif command == 'draft-new':
        if len(sys.argv) < 5:
            print('Usage: python gmail_tool.py draft-new "to" "subject" "body"|--body-file <path> [--attach file]', file=sys.stderr)
            sys.exit(1)
        body_arg = sys.argv[4]
        if body_arg == '--body-file':
            if len(sys.argv) < 6:
                print('--body-file requires a path argument', file=sys.stderr)
                sys.exit(1)
            with open(sys.argv[5], 'r', encoding='utf-8') as _bf:
                body_text_arg = _bf.read()
            _rest_new, attachments = _parse_attachments(sys.argv[6:])
        else:
            body_text_arg = body_arg
            _rest_new, attachments = _parse_attachments(sys.argv[5:])
        _rest_new, inline_images = _parse_inline(_rest_new)
        cmd_draft_new(sys.argv[2], sys.argv[3], body_text_arg, attachments=attachments,
                      inline_images=inline_images)

    elif command == 'delete-draft':
        if len(sys.argv) < 3:
            print("Usage: python gmail_tool.py delete-draft <draft_id>", file=sys.stderr)
            sys.exit(1)
        cmd_delete_draft(sys.argv[2])

    elif command == 'download':
        if len(sys.argv) < 3:
            print("Usage: python gmail_tool.py download <message_id> [--output-dir /path/to/dir]", file=sys.stderr)
            sys.exit(1)
        msg_id = sys.argv[2]
        out_dir = None
        if '--output-dir' in sys.argv:
            idx = sys.argv.index('--output-dir')
            if idx + 1 < len(sys.argv):
                out_dir = sys.argv[idx + 1]
        cmd_download(msg_id, output_dir=out_dir)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except AttachmentGateError as _gate_exc:
        # Exit non-zero with the findings on stderr so the failure is impossible
        # to read as success. Nothing was drafted and nothing was sent.
        print(str(_gate_exc), file=sys.stderr)
        sys.exit(1)
