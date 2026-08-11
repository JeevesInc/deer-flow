#!/usr/bin/env python3
"""Download ANY Google Drive file to a local path (binary or Google-native).

This exists so that inspecting a Drive workbook/PDF never requires writing a
new ad-hoc workspace script -- which is how the recurring
`ModuleNotFoundError: No module named google_auth` keeps happening (most
recently 2026-08-07, inspect_nb.py, NB borrowing base tape thread).

Usage:
    python download_file.py <file_id_or_url> [--output-dir DIR] [--name NAME]

Google-native types are exported:  Sheets -> .xlsx, Docs -> .docx, Slides -> .pptx
Everything else (xlsx, pdf, csv, zip, png ...) downloads byte-for-byte.
Prints the absolute local path and byte size of the downloaded file.
"""

import io
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_shared'))
from google_auth import get_credentials  # google_auth lives in skills custom _shared, NOT here
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

EXPORT_MAP = {
    'application/vnd.google-apps.spreadsheet': (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx'),
    'application/vnd.google-apps.document': (
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx'),
    'application/vnd.google-apps.presentation': (
        'application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx'),
}


def extract_id(url_or_id):
    if re.match(r'^[a-zA-Z0-9_-]{20,}$', url_or_id):
        return url_or_id
    m = re.search(r'[/]d[/]([a-zA-Z0-9_-]+)', url_or_id)
    if m:
        return m.group(1)
    qs = parse_qs(urlparse(url_or_id).query)
    if 'id' in qs:
        return qs['id'][0]
    raise ValueError('Cannot extract file id from: %s' % url_or_id)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    file_id = extract_id(sys.argv[1])
    out_dir = None
    name_override = None
    if '--output-dir' in sys.argv:
        out_dir = sys.argv[sys.argv.index('--output-dir') + 1]
    if '--name' in sys.argv:
        name_override = sys.argv[sys.argv.index('--name') + 1]
    if not out_dir:
        out_dir = os.environ.get('OUTPUTS_PATH') or os.environ.get('WORKSPACE_PATH') or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)

    svc = build('drive', 'v3', credentials=get_credentials())
    meta = svc.files().get(fileId=file_id, fields='name,mimeType,size',
                           supportsAllDrives=True).execute()
    name = name_override or meta['name']
    mime = meta.get('mimeType', '')

    if mime in EXPORT_MAP:
        export_mime, ext = EXPORT_MAP[mime]
        if not name.lower().endswith(ext):
            name += ext
        req = svc.files().export_media(fileId=file_id, mimeType=export_mime)
    elif mime.startswith('application/vnd.google-apps'):
        print('Unsupported Google-native type: %s' % mime, file=sys.stderr)
        sys.exit(1)
    else:
        req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    dest = os.path.join(out_dir, name)
    with open(dest, 'wb') as fh:
        fh.write(buf.getvalue())

    print('Downloaded: %s' % dest)
    print('Bytes: %d' % os.path.getsize(dest))
    print('Source mimeType: %s' % mime)


if __name__ == '__main__':
    main()
