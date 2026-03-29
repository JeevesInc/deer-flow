#!/usr/bin/env python3
"""Merge data tabs from a borrowing base workbook into a template.

Downloads the latest template from Google Drive, replaces data tabs with
fresh data, preserves formula/summary tabs, and saves the result.

Usage:
    python merge_template.py <data_workbook> <template_drive_id> [--output <path>]

The script auto-detects which tabs to replace based on the data workbook's
sheet names (tape_start, tape_end, rollforward, eligibility_summary, tape_combined).
"""

import os
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlparse


def _ensure_deps():
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'openpyxl'])


def _extract_id(url_or_id):
    if re.match(r'^[a-zA-Z0-9_-]+$', url_or_id):
        return url_or_id
    m = re.search(r'/d/([a-zA-Z0-9_-]+)', url_or_id)
    if m:
        return m.group(1)
    parsed = urlparse(url_or_id)
    qs = parse_qs(parsed.query)
    if 'id' in qs:
        return qs['id'][0]
    raise ValueError(f"Cannot extract file ID from: {url_or_id}")


def _download_template(drive_id):
    """Download an xlsx from Drive to a temp file."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    for var in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN'):
        if not os.environ.get(var):
            print(f"ERROR: Missing env var {var}", file=sys.stderr)
            sys.exit(1)

    file_id = _extract_id(drive_id)
    creds = Credentials(
        token=None,
        refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
    )
    service = build('drive', 'v3', credentials=creds)

    meta = service.files().get(fileId=file_id, fields='name,mimeType').execute()
    name = meta.get('name', 'template')
    print(f"Downloading template: {name}", file=sys.stderr)

    content = service.files().get_media(fileId=file_id).execute()
    tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', prefix='template_', delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name, name


def _copy_sheet_data(source_ws, target_ws):
    """Copy cell values from one worksheet to another."""
    for row in source_ws.iter_rows():
        for cell in row:
            target_ws[cell.coordinate].value = cell.value
            if cell.number_format:
                target_ws[cell.coordinate].number_format = cell.number_format

    # Copy column widths
    for col_letter, dim in source_ws.column_dimensions.items():
        target_ws.column_dimensions[col_letter].width = dim.width


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import argparse
    parser = argparse.ArgumentParser(description='Merge data into template')
    parser.add_argument('data_workbook', help='Local path to data workbook')
    parser.add_argument('template_drive_id', help='Drive ID or URL of template')
    parser.add_argument('--output', default=None, help='Output path (default: $OUTPUTS_PATH)')
    args = parser.parse_args()

    _ensure_deps()
    import openpyxl

    if not os.path.isfile(args.data_workbook):
        print(f"ERROR: Data workbook not found: {args.data_workbook}", file=sys.stderr)
        sys.exit(1)

    # Download template
    template_path, template_name = _download_template(args.template_drive_id)

    try:
        # Open both workbooks
        print("Opening template (preserving formulas)...")
        template_wb = openpyxl.load_workbook(template_path)

        print("Opening data workbook...")
        data_wb = openpyxl.load_workbook(args.data_workbook)

        data_sheet_names = data_wb.sheetnames
        print(f"Data tabs to merge: {data_sheet_names}")

        # Track which template sheets are formula tabs (not in data workbook)
        formula_tabs = [s for s in template_wb.sheetnames if s not in data_sheet_names]
        print(f"Template formula tabs (preserved): {formula_tabs}")

        # Delete old data tabs from template
        for name in data_sheet_names:
            if name in template_wb.sheetnames:
                print(f"  Removing old '{name}' from template...")
                del template_wb[name]

        # Copy fresh data tabs into template
        for name in data_sheet_names:
            if name in data_wb.sheetnames:
                print(f"  Adding fresh '{name}' to template...")
                source_ws = data_wb[name]
                target_ws = template_wb.create_sheet(name)
                _copy_sheet_data(source_ws, target_ws)

        # Reorder sheets: put data tabs first, then formula tabs
        desired_order = data_sheet_names + formula_tabs
        actual = template_wb.sheetnames
        for i, name in enumerate(desired_order):
            if name in actual:
                current_idx = actual.index(name)
                template_wb.move_sheet(name, offset=i - current_idx)
                actual = template_wb.sheetnames  # refresh after move

        # Determine output path
        if args.output:
            output_path = args.output
        else:
            output_dir = os.environ.get('OUTPUTS_PATH', '/mnt/user-data/outputs')
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, template_name)

        print(f"Saving merged workbook: {output_path}")
        template_wb.save(output_path)
        print(f"\nDone! Output: {output_path}")
        print("Open in Excel to let formulas recalculate.")

    finally:
        # Cleanup temp file
        if os.path.exists(template_path):
            os.unlink(template_path)


if __name__ == '__main__':
    main()
