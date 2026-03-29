#!/usr/bin/env python3
"""Document redlining tool — compare two Word documents or add negotiation comments.

Usage:
    python redline_tool.py compare <file1> <file2> [--output redline.docx]
    python redline_tool.py comment <file.docx> <comments.json> [--output commented.docx]

Files can be local paths, Google Drive file IDs, or Google Drive URLs.

Requires: python-docx, lxml (auto-installed if missing)
For Drive access: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""

import difflib
import json
import os
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlparse


def _ensure_deps():
    """Auto-install python-docx and lxml if missing."""
    try:
        import docx  # noqa: F401
        import lxml  # noqa: F401
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q',
                               'python-docx', 'lxml'])


def _is_drive_ref(path: str) -> bool:
    """Check if a path looks like a Drive ID or URL rather than a local file."""
    if os.path.isfile(path):
        return False
    if 'drive.google.com' in path or 'docs.google.com' in path:
        return True
    if re.match(r'^[a-zA-Z0-9_-]{20,}$', path):
        return True
    return False


def _extract_id(url_or_id: str) -> str:
    """Extract file ID from a Google Drive URL or return as-is."""
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


def _download_from_drive(file_ref: str) -> str:
    """Download a .docx from Google Drive to a temp file. Returns the local path."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    for var in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN'):
        if not os.environ.get(var):
            print(f"ERROR: Missing environment variable {var}", file=sys.stderr)
            sys.exit(1)

    file_id = _extract_id(file_ref)
    creds = Credentials(
        token=None,
        refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
    )
    service = build('drive', 'v3', credentials=creds)

    meta = service.files().get(fileId=file_id, fields='name,mimeType').execute()
    mime = meta['mimeType']
    name = meta.get('name', 'document')

    if mime == 'application/vnd.google-apps.document':
        # Google Doc — export as docx
        content = service.files().export(
            fileId=file_id,
            mimeType='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        ).execute()
    else:
        # Binary file — download directly
        content = service.files().get_media(fileId=file_id).execute()

    suffix = '.docx'
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, prefix=f"{name}_", delete=False)
    tmp.write(content)
    tmp.close()
    print(f"Downloaded: {name} -> {tmp.name}", file=sys.stderr)
    return tmp.name


def _resolve_file(path: str) -> str:
    """Resolve a file argument to a local path, downloading from Drive if needed."""
    if _is_drive_ref(path):
        return _download_from_drive(path)
    if not os.path.isfile(path):
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    return path


def _get_output_path(args: list, default_name: str) -> str:
    """Extract --output path from args, or use OUTPUTS_PATH env var."""
    if '--output' in args:
        idx = args.index('--output')
        if idx + 1 < len(args):
            return args[idx + 1]
    output_dir = os.environ.get('OUTPUTS_PATH', '/mnt/user-data/outputs')
    return os.path.join(output_dir, default_name)


def compare(file1_path: str, file2_path: str, output_path: str):
    """Compare two .docx files and produce a redlined document."""
    import docx
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_UNDERLINE

    local1 = _resolve_file(file1_path)
    local2 = _resolve_file(file2_path)

    doc1 = docx.Document(local1)
    doc2 = docx.Document(local2)

    paras1 = [p.text for p in doc1.paragraphs]
    paras2 = [p.text for p in doc2.paragraphs]

    # Build the redline document
    redline = docx.Document()

    # Add header
    header_para = redline.add_paragraph()
    run = header_para.add_run("REDLINE COMPARISON")
    run.bold = True
    run.font.size = Pt(14)
    header_para = redline.add_paragraph()
    run = header_para.add_run(f"Base: {os.path.basename(local1)}")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(128, 128, 128)
    header_para = redline.add_paragraph()
    run = header_para.add_run(f"Revised: {os.path.basename(local2)}")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(128, 128, 128)
    redline.add_paragraph()  # spacer

    matcher = difflib.SequenceMatcher(None, paras1, paras2)
    changes = {'equal': 0, 'delete': 0, 'insert': 0, 'replace': 0}

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for text in paras1[i1:i2]:
                p = redline.add_paragraph()
                run = p.add_run(text)
                run.font.color.rgb = RGBColor(0, 0, 0)
            changes['equal'] += (i2 - i1)

        elif tag == 'delete':
            for text in paras1[i1:i2]:
                p = redline.add_paragraph()
                run = p.add_run(text)
                run.font.color.rgb = RGBColor(255, 0, 0)
                run.font.strikethrough = True
            changes['delete'] += (i2 - i1)

        elif tag == 'insert':
            for text in paras2[j1:j2]:
                p = redline.add_paragraph()
                run = p.add_run(text)
                run.font.color.rgb = RGBColor(0, 0, 255)
                run.font.underline = WD_UNDERLINE.SINGLE
            changes['insert'] += (j2 - j1)

        elif tag == 'replace':
            # Show deleted (old) paragraphs
            for text in paras1[i1:i2]:
                p = redline.add_paragraph()
                run = p.add_run(text)
                run.font.color.rgb = RGBColor(255, 0, 0)
                run.font.strikethrough = True
            # Show inserted (new) paragraphs
            for text in paras2[j1:j2]:
                p = redline.add_paragraph()
                run = p.add_run(text)
                run.font.color.rgb = RGBColor(0, 0, 255)
                run.font.underline = WD_UNDERLINE.SINGLE
            changes['replace'] += max(i2 - i1, j2 - j1)

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    redline.save(output_path)

    print(f"Redline saved to: {output_path}")
    print(f"Changes: {changes['delete']} deletions, {changes['insert']} insertions, "
          f"{changes['replace']} replacements, {changes['equal']} unchanged paragraphs")

    # Clean up temp files
    for f in [local1, local2]:
        if f.startswith(tempfile.gettempdir()):
            os.unlink(f)


def comment(file_path: str, comments_json_path: str, output_path: str):
    """Add Word comments to a .docx at matching paragraphs."""
    import docx
    from lxml import etree

    local_file = _resolve_file(file_path)

    with open(comments_json_path, 'r', encoding='utf-8') as f:
        comments_data = json.load(f)

    doc = docx.Document(local_file)

    # Word comment XML namespace
    W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    nsmap = {'w': W_NS}

    # Access or create the comments part
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.part import Part
    from docx.opc.packuri import PackURI

    comments_part = None
    comments_element = None

    # Check if comments part already exists
    for rel in doc.part.rels.values():
        if 'comments' in rel.reltype:
            comments_part = rel.target_part
            comments_element = etree.fromstring(comments_part.blob)
            break

    if comments_element is None:
        # Create a new comments XML element
        comments_element = etree.Element(f'{{{W_NS}}}comments', nsmap=nsmap)

    comment_id = 0
    matched = 0

    for entry in comments_data:
        para_match = entry.get('paragraph_match', '')
        comment_text = entry.get('comment', '')
        author = entry.get('author', 'Jeeves')

        # Find matching paragraph
        for para in doc.paragraphs:
            if para_match.lower() in para.text.lower():
                # Create the comment element
                comment_el = etree.SubElement(comments_element, f'{{{W_NS}}}comment')
                comment_el.set(f'{{{W_NS}}}id', str(comment_id))
                comment_el.set(f'{{{W_NS}}}author', author)
                comment_el.set(f'{{{W_NS}}}date', '2026-03-28T00:00:00Z')

                # Add comment paragraph
                cp = etree.SubElement(comment_el, f'{{{W_NS}}}p')
                cr = etree.SubElement(cp, f'{{{W_NS}}}r')
                ct = etree.SubElement(cr, f'{{{W_NS}}}t')
                ct.text = comment_text

                # Add comment reference to the paragraph's XML
                para_xml = para._element
                # Add commentRangeStart before first run
                range_start = etree.Element(f'{{{W_NS}}}commentRangeStart')
                range_start.set(f'{{{W_NS}}}id', str(comment_id))
                para_xml.insert(0, range_start)

                # Add commentRangeEnd and commentReference after last run
                range_end = etree.SubElement(para_xml, f'{{{W_NS}}}commentRangeEnd')
                range_end.set(f'{{{W_NS}}}id', str(comment_id))

                ref_run = etree.SubElement(para_xml, f'{{{W_NS}}}r')
                ref_rpr = etree.SubElement(ref_run, f'{{{W_NS}}}rPr')
                ref_style = etree.SubElement(ref_rpr, f'{{{W_NS}}}rStyle')
                ref_style.set(f'{{{W_NS}}}val', 'CommentReference')
                comment_ref = etree.SubElement(ref_run, f'{{{W_NS}}}commentReference')
                comment_ref.set(f'{{{W_NS}}}id', str(comment_id))

                comment_id += 1
                matched += 1
                break
        else:
            print(f"WARNING: No paragraph match for: '{para_match[:60]}...'", file=sys.stderr)

    # Write comments part back
    if comments_part is not None:
        comments_part._blob = etree.tostring(comments_element, xml_declaration=True, encoding='UTF-8', standalone=True)
    else:
        # Add new comments part to the document
        comments_blob = etree.tostring(comments_element, xml_declaration=True, encoding='UTF-8', standalone=True)
        comments_part_uri = PackURI('/word/comments.xml')
        content_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml'
        comments_part = Part(
            comments_part_uri,
            content_type,
            comments_blob,
            doc.part.package,
        )
        doc.part.relate_to(comments_part, 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments')

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc.save(output_path)

    print(f"Commented document saved to: {output_path}")
    print(f"Comments added: {matched} of {len(comments_data)}")

    # Clean up temp files
    if local_file.startswith(tempfile.gettempdir()):
        os.unlink(local_file)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print("Usage:", file=sys.stderr)
        print("  python redline_tool.py compare <file1> <file2> [--output redline.docx]", file=sys.stderr)
        print("  python redline_tool.py comment <file.docx> <comments.json> [--output commented.docx]", file=sys.stderr)
        sys.exit(1)

    _ensure_deps()

    command = args[0]

    if command == 'compare':
        if len(args) < 3:
            print("ERROR: compare requires two file arguments", file=sys.stderr)
            sys.exit(1)
        output = _get_output_path(args, 'redline_output.docx')
        compare(args[1], args[2], output)

    elif command == 'comment':
        if len(args) < 3:
            print("ERROR: comment requires a .docx file and a comments JSON file", file=sys.stderr)
            sys.exit(1)
        output = _get_output_path(args, 'commented_output.docx')
        comment(args[1], args[2], output)

    else:
        print(f"ERROR: Unknown command '{command}'. Use 'compare' or 'comment'.", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
