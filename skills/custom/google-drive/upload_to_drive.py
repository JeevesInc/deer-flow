#!/usr/bin/env python3
"""Upload a local file to Google Drive — updating in place when the file already exists.

Usage:
    python upload_to_drive.py <local_file_path> [--folder <FOLDER_ID>] [options]

Options:
    --folder <FOLDER_ID>   Target Drive folder (default: 'DeerFlow Output' folder)
    --file-id <FILE_ID>    Update this exact Drive file (skips all matching logic)
    --new                  Force creation of a new file (never update in place)

Update-in-place behavior (the default):
    Re-uploading a doc must NOT create a duplicate or change the link other
    people already hold. The script normalizes the filename (strips date
    suffixes like "- 20260610") and looks for an existing copy of the same
    document, in this order:
      1. --file-id argument
      2. The drive registry (.deer-flow/drive_registry.json) — canonical doc → file ID
      3. A scan of the target folder for a file with the same normalized name
    If found, the existing file is UPDATED (same ID, same link, name refreshed
    to the new filename). Otherwise a new file is created and registered.

    Updating works even when the Drive copy was converted to a native Google
    Sheet/Doc — Drive re-imports the content and the ID/link is preserved.

After upload the file metadata is re-fetched from Drive and printed as a
VERIFIED line. If verification fails the script exits non-zero.

Returns the shareable Google Drive link on success.

Requires env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""

import json
import mimetypes
import os
import re
import sys
import tempfile


FOLDER_NAME = "DeerFlow Output"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_SCRIPT_DIR, '..', '_shared'))
from env_loader import load_env
load_env()
from google_auth import get_credentials

# Registry mapping canonical doc keys -> Drive file IDs. Lives in backend
# runtime data so every skill/cron shares one registry.
_DEFAULT_REGISTRY = os.path.normpath(os.path.join(
    _SCRIPT_DIR, '..', '..', '..', 'backend', '.deer-flow', 'drive_registry.json'))
REGISTRY_PATH = os.environ.get('DRIVE_REGISTRY_PATH', _DEFAULT_REGISTRY)


def _get_service():
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=get_credentials())


def normalize_doc_name(filename):
    """Canonical key for a document: name without extension, date stamps, or
    punctuation noise — so 'Tracker - 20260609.xlsx' and 'Tracker — 20260610.xlsx'
    resolve to the same document."""
    name = os.path.splitext(os.path.basename(filename))[0].lower()
    name = re.sub(r'\d{4}-\d{2}-\d{2}', ' ', name)          # 2026-06-10
    name = re.sub(r'\d{8}(?:-\d{8})?', ' ', name)            # 20260610, 20240101-20260531
    name = re.sub(r'[–—]', '-', name)               # en/em dash -> hyphen
    name = re.sub(r'[^a-z0-9]+', ' ', name)                   # punctuation -> space
    return re.sub(r'\s+', ' ', name).strip()


def _load_registry():
    try:
        with open(REGISTRY_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_registry(registry):
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(REGISTRY_PATH), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)
        os.replace(tmp, REGISTRY_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def _file_alive(service, file_id):
    """Return file metadata if the file exists and is not trashed, else None."""
    try:
        meta = service.files().get(
            fileId=file_id, fields='id,name,trashed,parents,mimeType').execute()
        return None if meta.get('trashed') else meta
    except Exception:
        return None


def _find_existing(service, folder_id, doc_key, explicit_file_id=None):
    """Resolve the existing Drive file to update, or None to create new."""
    # 1. Explicit --file-id wins
    if explicit_file_id:
        meta = _file_alive(service, explicit_file_id)
        if meta:
            return meta
        print(f"ERROR: --file-id {explicit_file_id} not found or trashed in Drive", file=sys.stderr)
        sys.exit(1)

    registry = _load_registry()
    reg_key = f"{folder_id}:{doc_key}"

    # 2. Registry lookup
    file_id = registry.get(reg_key)
    if file_id:
        meta = _file_alive(service, file_id)
        if meta:
            return meta
        # Stale registry entry — fall through to folder scan
        registry.pop(reg_key, None)
        _save_registry(registry)

    # 3. Folder scan by normalized name
    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='files(id,name,mimeType,modifiedTime)',
            pageSize=200,
        ).execute()
    except Exception:
        return None

    matches = [f for f in results.get('files', [])
               if f.get('mimeType') != 'application/vnd.google-apps.folder'
               and normalize_doc_name(f['name']) == doc_key]
    if not matches:
        return None
    if len(matches) > 1:
        matches.sort(key=lambda f: f.get('modifiedTime', ''), reverse=True)
        print("WARNING: Multiple copies of this document exist in the folder:", file=sys.stderr)
        for m in matches:
            print(f"  - {m['name']} (id: {m['id']}, modified {m.get('modifiedTime', '?')})", file=sys.stderr)
        print(f"Updating the most recent ({matches[0]['id']}). Clean up the duplicates.", file=sys.stderr)
    return matches[0]


def upload_file(local_path, target_folder_id=None, file_id=None, force_new=False):
    from googleapiclient.http import MediaFileUpload

    service = _get_service()
    folder_id = target_folder_id if target_folder_id else _get_or_create_folder(service)

    filename = os.path.basename(local_path)
    mime_type, _ = mimetypes.guess_type(local_path)
    if not mime_type:
        mime_type = 'application/octet-stream'

    doc_key = normalize_doc_name(filename)
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)

    existing = None if force_new else _find_existing(service, folder_id, doc_key, file_id)

    if existing:
        # Update in place: same file ID, same link; refresh name and content.
        # If the Drive copy is a native Google file, Drive re-imports the
        # uploaded content and keeps the ID.
        update_kwargs = {
            'fileId': existing['id'],
            'body': {'name': filename},
            'media_body': media,
            'fields': 'id,webViewLink',
        }
        parents = existing.get('parents') or []
        if folder_id not in parents:
            update_kwargs['addParents'] = folder_id
            if parents:
                update_kwargs['removeParents'] = ','.join(parents)
        uploaded = service.files().update(**update_kwargs).execute()
        action = 'UPDATED existing file (ID and link unchanged)'
    else:
        uploaded = service.files().create(
            body={'name': filename, 'parents': [folder_id]},
            media_body=media,
            fields='id,webViewLink',
        ).execute()
        action = 'CREATED new file'

        # Try to share — org policy may restrict public sharing
        for perm in [
            {'type': 'domain', 'role': 'reader', 'domain': 'tryjeeves.com'},
            {'type': 'anyone', 'role': 'reader'},
        ]:
            try:
                service.permissions().create(fileId=uploaded['id'], body=perm).execute()
                break
            except Exception:
                continue

    drive_id = uploaded.get('id', '')

    # Record canonical mapping so the next upload of this doc updates in place
    if drive_id and not force_new:
        registry = _load_registry()
        registry[f"{folder_id}:{doc_key}"] = drive_id
        _save_registry(registry)

    # Verify: re-fetch from Drive and confirm the file is really there
    verified = _file_alive(service, drive_id)
    if not verified:
        print(f"ERROR: Post-upload verification failed for file ID {drive_id}", file=sys.stderr)
        sys.exit(1)

    link = uploaded.get('webViewLink', '')
    print(f"Uploaded: {filename}")
    print(f"Action: {action}")
    print(f"VERIFIED in Drive: {verified['name']} (id: {drive_id})")
    print(f"Link: {link}")
    return link


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print("Usage: python upload_to_drive.py <local_file_path> [--folder <FOLDER_ID>] [--file-id <FILE_ID>] [--new]", file=sys.stderr)
        sys.exit(1)

    local_path = args[0]
    if not os.path.isfile(local_path):
        print(f"ERROR: File not found: {local_path}", file=sys.stderr)
        sys.exit(1)

    def _opt(flag):
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
        return None

    upload_file(
        local_path,
        target_folder_id=_opt('--folder'),
        file_id=_opt('--file-id'),
        force_new='--new' in args,
    )


if __name__ == '__main__':
    main()
