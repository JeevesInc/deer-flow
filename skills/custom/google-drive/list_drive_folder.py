#!/usr/bin/env python3
"""List the contents of a Google Drive folder.

Usage:
    python list_drive_folder.py <folder_id_or_url> [--recursive] [--max-depth N]

Requires env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""

import os
import re
import sys
from urllib.parse import parse_qs, urlparse


def extract_folder_id(url_or_id: str) -> str:
    """Extract folder ID from a Google Drive URL or return as-is if already an ID."""
    if re.match(r'^[a-zA-Z0-9_-]+$', url_or_id):
        return url_or_id
    # Try /folders/{ID} pattern
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', url_or_id)
    if m:
        return m.group(1)
    # Try /d/{ID}/ pattern (shared drive links)
    m = re.search(r'/d/([a-zA-Z0-9_-]+)', url_or_id)
    if m:
        return m.group(1)
    # Try ?id={ID} pattern
    parsed = urlparse(url_or_id)
    qs = parse_qs(parsed.query)
    if 'id' in qs:
        return qs['id'][0]
    raise ValueError(f"Cannot extract folder ID from: {url_or_id}")


def list_folder(service, folder_id, indent=0, recursive=False, max_depth=2, current_depth=0):
    """List contents of a folder. Optionally recurse into subfolders."""
    prefix = "  " * indent
    query = f"'{folder_id}' in parents and trashed = false"
    page_token = None
    items = []

    while True:
        results = service.files().list(
            q=query,
            spaces='drive',
            fields='nextPageToken, files(id, name, mimeType, modifiedTime, size)',
            orderBy='name',
            pageSize=100,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items.extend(results.get('files', []))
        page_token = results.get('nextPageToken')
        if not page_token:
            break

    # Separate folders and files, print folders first
    folders = [f for f in items if f['mimeType'] == 'application/vnd.google-apps.folder']
    files = [f for f in items if f['mimeType'] != 'application/vnd.google-apps.folder']

    for folder in folders:
        modified = folder.get('modifiedTime', '')[:10]
        print(f"{prefix}[folder] {folder['name']}/  (id: {folder['id']}, modified: {modified})")
        if recursive and current_depth < max_depth:
            list_folder(service, folder['id'], indent + 1, recursive, max_depth, current_depth + 1)

    for f in files:
        modified = f.get('modifiedTime', '')[:10]
        size = f.get('size', '')
        if size:
            size_kb = int(size) / 1024
            size_str = f", {size_kb:.0f} KB" if size_kb < 1024 else f", {int(size) / (1024*1024):.1f} MB"
        else:
            size_str = ""
        print(f"{prefix}{f['name']}  (id: {f['id']}, type: {f['mimeType']}, modified: {modified}{size_str})")

    if not items:
        print(f"{prefix}(empty folder)")

    return len(items)


def main():
    # Ensure UTF-8 output on Windows
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    # Parse arguments
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print("Usage: python list_drive_folder.py <folder_id_or_url> [--recursive] [--max-depth N]", file=sys.stderr)
        sys.exit(1)

    folder_input = args[0]
    recursive = '--recursive' in args
    max_depth = 2
    if '--max-depth' in args:
        idx = args.index('--max-depth')
        if idx + 1 < len(args):
            max_depth = int(args[idx + 1])

    # Check env vars
    for var in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN'):
        if not os.environ.get(var):
            print(f"ERROR: Missing environment variable {var}", file=sys.stderr)
            sys.exit(1)

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q',
                               'google-api-python-client', 'google-auth'])
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

    folder_id = extract_folder_id(folder_input)

    creds = Credentials(
        token=None,
        refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
    )
    service = build('drive', 'v3', credentials=creds)

    # Get folder metadata
    try:
        meta = service.files().get(fileId=folder_id, fields='name,mimeType', supportsAllDrives=True).execute()
    except Exception as e:
        err = str(e)
        if '404' in err:
            print(f"ERROR: Cannot access folder (ID: {folder_id}).")
            print("The OAuth account may not have access to this folder.")
        else:
            print(f"ERROR: Google API error: {err[:300]}")
        sys.exit(1)

    folder_name = meta.get('name', folder_id)
    mode = "recursive" if recursive else "top-level"
    print(f"=== {folder_name} === ({mode})\n")

    count = list_folder(service, folder_id, recursive=recursive, max_depth=max_depth)
    print(f"\n--- {count} items listed ---")


if __name__ == '__main__':
    main()
