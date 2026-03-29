#!/usr/bin/env python3
"""Upload a local file to Google Drive.

Usage:
    python upload_to_drive.py <local_file_path> [--folder <FOLDER_ID>]

Without --folder, uploads to a 'DeerFlow Output' folder (created if needed).
With --folder, uploads directly to the specified Drive folder ID.

Returns the shareable Google Drive link on success.

Requires env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""

import mimetypes
import os
import sys


FOLDER_NAME = "DeerFlow Output"


def _get_service():
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

    creds = Credentials(
        token=None,
        refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
    )
    return build('drive', 'v3', credentials=creds)


def _get_or_create_folder(service):
    """Find or create the output folder in Drive root."""
    results = service.files().list(
        q=f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        spaces='drive',
        fields='files(id)',
        pageSize=1,
    ).execute()
    files = results.get('files', [])
    if files:
        return files[0]['id']

    # Create folder
    folder = service.files().create(
        body={
            'name': FOLDER_NAME,
            'mimeType': 'application/vnd.google-apps.folder',
        },
        fields='id',
    ).execute()
    folder_id = folder['id']
    print(f"Created Drive folder: {FOLDER_NAME}")
    return folder_id


def upload_file(local_path, target_folder_id=None):
    from googleapiclient.http import MediaFileUpload

    service = _get_service()
    folder_id = target_folder_id if target_folder_id else _get_or_create_folder(service)

    filename = os.path.basename(local_path)
    mime_type, _ = mimetypes.guess_type(local_path)
    if not mime_type:
        mime_type = 'application/octet-stream'

    media = MediaFileUpload(local_path, mimetype=mime_type)
    file_metadata = {
        'name': filename,
        'parents': [folder_id],
    }

    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id,webViewLink',
    ).execute()

    link = uploaded.get('webViewLink', '')
    file_id = uploaded.get('id', '')

    # Try to share — org policy may restrict public sharing
    for perm in [
        {'type': 'domain', 'role': 'reader', 'domain': 'tryjeeves.com'},
        {'type': 'anyone', 'role': 'reader'},
    ]:
        try:
            service.permissions().create(fileId=file_id, body=perm).execute()
            break
        except Exception:
            continue

    print(f"Uploaded: {filename}")
    print(f"Link: {link}")
    return link


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print("Usage: python upload_to_drive.py <local_file_path> [--folder <FOLDER_ID>]", file=sys.stderr)
        sys.exit(1)

    local_path = args[0]
    if not os.path.isfile(local_path):
        print(f"ERROR: File not found: {local_path}", file=sys.stderr)
        sys.exit(1)

    target_folder_id = None
    if '--folder' in args:
        idx = args.index('--folder')
        if idx + 1 < len(args):
            target_folder_id = args[idx + 1]

    upload_file(local_path, target_folder_id=target_folder_id)


if __name__ == '__main__':
    main()
