#!/usr/bin/env python3
"""Document redlining tool — compare docs, read/write track changes, add comments.

Usage:
    python redline_tool.py compare <file1> <file2> [--output redline.docx] [--track-changes]
    python redline_tool.py read-changes <file.docx>
    python redline_tool.py suggest <file.docx> <changes.json> [--output suggested.docx]
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
from datetime import datetime, timezone
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
        content = service.files().export(
            fileId=file_id,
            mimeType='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        ).execute()
    else:
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


# ---------------------------------------------------------------------------
# Word XML namespace constants
# ---------------------------------------------------------------------------

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
W_TAG = lambda tag: f'{{{W_NS}}}{tag}'


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# read-changes: Parse track changes from an existing document
# ---------------------------------------------------------------------------

def read_changes(file_path: str):
    """Read and display track changes (revisions) from a Word document."""
    import docx
    from lxml import etree

    local_file = _resolve_file(file_path)
    doc = docx.Document(local_file)

    body = doc.element.body
    changes = []

    # Find all insertion and deletion elements in document order
    for elem in body.iter():
        tag = etree.QName(elem.tag).localname if '}' in elem.tag else elem.tag

        if tag == 'ins':
            author = elem.get(W_TAG('author'), 'Unknown')
            date = elem.get(W_TAG('date'), '')
            # Collect inserted text from child runs
            text_parts = []
            for t in elem.iter(W_TAG('t')):
                if t.text:
                    text_parts.append(t.text)
            text = ''.join(text_parts)
            if text.strip():
                changes.append({
                    'type': 'insertion',
                    'text': text,
                    'author': author,
                    'date': date,
                })

        elif tag == 'del':
            author = elem.get(W_TAG('author'), 'Unknown')
            date = elem.get(W_TAG('date'), '')
            # Deletions use <w:delText> instead of <w:t>
            text_parts = []
            for dt in elem.iter(W_TAG('delText')):
                if dt.text:
                    text_parts.append(dt.text)
            text = ''.join(text_parts)
            if text.strip():
                changes.append({
                    'type': 'deletion',
                    'text': text,
                    'author': author,
                    'date': date,
                })

        elif tag == 'rPrChange':
            # Formatting change — note but less critical
            author = elem.get(W_TAG('author'), 'Unknown')
            date = elem.get(W_TAG('date'), '')
            changes.append({
                'type': 'format_change',
                'text': '(formatting change)',
                'author': author,
                'date': date,
            })

    # Also read comments for full context
    comments = _read_comments(doc)

    if not changes and not comments:
        print(f"No track changes or comments found in {os.path.basename(local_file)}")
        # Fall back to showing the document text so the agent has context
        print("\nDocument text:")
        for p in doc.paragraphs:
            if p.text.strip():
                print(p.text)
        return

    if changes:
        print(f"Found {len(changes)} tracked change(s):\n")
        for i, c in enumerate(changes, 1):
            marker = '+' if c['type'] == 'insertion' else '-' if c['type'] == 'deletion' else '~'
            date_str = c['date'][:10] if c['date'] else ''
            print(f"  [{marker}] {c['type'].upper()} by {c['author']} ({date_str})")
            # Show text with context
            text = c['text']
            if len(text) > 200:
                text = text[:200] + '...'
            print(f"      {text}")
            print()

    if comments:
        print(f"\nFound {len(comments)} comment(s):\n")
        for c in comments:
            print(f"  [{c['author']}] {c['text']}")
            print()

    # Output as JSON for agent processing
    print("\n--- JSON ---")
    print(json.dumps({
        'changes': changes,
        'comments': comments,
        'total_changes': len(changes),
        'total_comments': len(comments),
        'insertions': len([c for c in changes if c['type'] == 'insertion']),
        'deletions': len([c for c in changes if c['type'] == 'deletion']),
    }, indent=2))

    if local_file.startswith(tempfile.gettempdir()):
        os.unlink(local_file)


def _read_comments(doc):
    """Extract comments from a document."""
    comments = []
    for rel in doc.part.rels.values():
        if 'comments' in rel.reltype:
            from lxml import etree
            comments_xml = etree.fromstring(rel.target_part.blob)
            for comment_el in comments_xml.iter(W_TAG('comment')):
                author = comment_el.get(W_TAG('author'), 'Unknown')
                date = comment_el.get(W_TAG('date'), '')
                text_parts = []
                for t in comment_el.iter(W_TAG('t')):
                    if t.text:
                        text_parts.append(t.text)
                text = ''.join(text_parts)
                if text.strip():
                    comments.append({
                        'author': author,
                        'date': date,
                        'text': text,
                    })
            break
    return comments


# ---------------------------------------------------------------------------
# suggest: Apply changes as proper Word track changes (w:ins / w:del)
# ---------------------------------------------------------------------------

def suggest(file_path: str, changes_json_path: str, output_path: str):
    """Apply suggested changes to a document as Word tracked revisions.

    The changes JSON should be an array of objects:
    [
      {
        "find": "original text to replace",
        "replace": "new suggested text",
        "author": "Jeeves"
      },
      {
        "find": "text to delete",
        "replace": "",
        "author": "Jeeves"
      }
    ]

    Produces a .docx with proper <w:del>/<w:ins> markup that shows up
    in Word's Track Changes review pane and can be accepted/rejected.
    """
    import docx
    from lxml import etree

    local_file = _resolve_file(file_path)

    with open(changes_json_path, 'r', encoding='utf-8') as f:
        changes_data = json.load(f)

    doc = docx.Document(local_file)
    now = _now_iso()
    applied = 0
    rev_id = 100  # Starting revision ID

    for change in changes_data:
        find_text = change.get('find', '')
        replace_text = change.get('replace', '')
        author = change.get('author', 'Jeeves')

        if not find_text:
            continue

        for para in doc.paragraphs:
            if find_text not in para.text:
                continue

            para_xml = para._element

            # Find the run(s) containing the target text
            # We need to handle text that may span multiple runs
            full_text = ''
            runs_with_positions = []
            for run_el in para_xml.iter(W_TAG('r')):
                for t_el in run_el.iter(W_TAG('t')):
                    if t_el.text:
                        start_pos = len(full_text)
                        full_text += t_el.text
                        runs_with_positions.append((run_el, t_el, start_pos, len(full_text)))

            find_start = full_text.find(find_text)
            if find_start == -1:
                continue

            find_end = find_start + len(find_text)

            # Build new XML elements to replace the affected runs
            # Strategy: rebuild the paragraph's runs with del/ins markup
            new_elements = []
            processed_up_to = 0

            for run_el, t_el, r_start, r_end in runs_with_positions:
                # Get run properties (formatting) to preserve
                rpr = run_el.find(W_TAG('rPr'))
                rpr_copy = None
                if rpr is not None:
                    rpr_copy = etree.tostring(rpr)

                text = t_el.text or ''
                local_start = max(find_start - r_start, 0)
                local_end = min(find_end - r_start, len(text))

                # Text before the change in this run
                before = text[:local_start] if r_start < find_start and local_start > 0 else ''
                # Text that's being changed in this run
                middle = text[max(local_start, 0):max(local_end, 0)] if r_start < find_end and r_end > find_start else ''
                # Text after the change in this run
                after = text[local_end:] if r_end > find_end and local_end < len(text) else ''

                # No overlap with change region — keep run as-is
                if r_end <= find_start or r_start >= find_end:
                    new_elements.append(('keep', run_el))
                    continue

                # Before text — keep as normal run
                if before:
                    r = etree.Element(W_TAG('r'))
                    if rpr_copy:
                        r.append(etree.fromstring(rpr_copy))
                    t = etree.SubElement(r, W_TAG('t'))
                    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    t.text = before
                    new_elements.append(('new', r))

                # Middle text — wrap in <w:del>
                if middle:
                    del_el = etree.Element(W_TAG('del'))
                    del_el.set(W_TAG('id'), str(rev_id))
                    del_el.set(W_TAG('author'), author)
                    del_el.set(W_TAG('date'), now)
                    rev_id += 1

                    r = etree.SubElement(del_el, W_TAG('r'))
                    if rpr_copy:
                        r.append(etree.fromstring(rpr_copy))
                    dt = etree.SubElement(r, W_TAG('delText'))
                    dt.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    dt.text = middle
                    new_elements.append(('new', del_el))

                    # Insert replacement text right after the deletion (only once, on last affected run)
                    if replace_text and r_end >= find_end:
                        ins_el = etree.Element(W_TAG('ins'))
                        ins_el.set(W_TAG('id'), str(rev_id))
                        ins_el.set(W_TAG('author'), author)
                        ins_el.set(W_TAG('date'), now)
                        rev_id += 1

                        r = etree.SubElement(ins_el, W_TAG('r'))
                        if rpr_copy:
                            r.append(etree.fromstring(rpr_copy))
                        t = etree.SubElement(r, W_TAG('t'))
                        t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                        t.text = replace_text
                        new_elements.append(('new', ins_el))

                # After text — keep as normal run
                if after:
                    r = etree.Element(W_TAG('r'))
                    if rpr_copy:
                        r.append(etree.fromstring(rpr_copy))
                    t = etree.SubElement(r, W_TAG('t'))
                    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    t.text = after
                    new_elements.append(('new', r))

            # Now replace the runs in the paragraph XML
            # Remove old runs
            for run_el, _, _, _ in runs_with_positions:
                try:
                    para_xml.remove(run_el)
                except ValueError:
                    pass

            # Insert new elements (preserve non-run children like bookmarks, comments)
            insert_point = None
            for child in list(para_xml):
                tag = etree.QName(child.tag).localname if '}' in child.tag else child.tag
                if tag == 'pPr':
                    insert_point = child
                    break

            idx = list(para_xml).index(insert_point) + 1 if insert_point is not None else 0
            for elem_type, elem in new_elements:
                if elem_type == 'keep':
                    para_xml.insert(idx, elem)
                else:
                    para_xml.insert(idx, elem)
                idx += 1

            applied += 1
            break  # Only apply once per change entry

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc.save(output_path)

    print(f"Document with tracked changes saved to: {output_path}")
    print(f"Applied {applied} of {len(changes_data)} suggested change(s)")
    print("Open in Word and go to Review > Track Changes to accept/reject each suggestion.")

    if local_file.startswith(tempfile.gettempdir()):
        os.unlink(local_file)


# ---------------------------------------------------------------------------
# compare: Paragraph-level diff (visual or tracked changes)
# ---------------------------------------------------------------------------

def compare(file1_path: str, file2_path: str, output_path: str, track_changes: bool = False):
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

    if track_changes:
        _compare_with_track_changes(doc1, paras1, paras2, output_path)
    else:
        _compare_visual(local1, local2, paras1, paras2, output_path)

    # Clean up temp files
    for f in [local1, local2]:
        if f.startswith(tempfile.gettempdir()):
            os.unlink(f)


def _compare_with_track_changes(base_doc, paras1, paras2, output_path):
    """Produce a document with proper Word track changes markup."""
    from lxml import etree

    now = _now_iso()
    author = 'Jeeves'
    rev_id = 100

    matcher = difflib.SequenceMatcher(None, paras1, paras2)
    changes = {'equal': 0, 'delete': 0, 'insert': 0, 'replace': 0}

    body = base_doc.element.body

    # Build a mapping from paragraph text to XML elements
    para_elements = list(body.iter(W_TAG('p')))

    # We'll build a new body content list
    new_body_children = []

    # Preserve non-paragraph elements (like sectPr) at the end
    non_para = []
    for child in list(body):
        tag = etree.QName(child.tag).localname if '}' in child.tag else child.tag
        if tag != 'p':
            non_para.append(child)

    # Process opcodes
    para_idx = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for k in range(i1, i2):
                if para_idx + (k - i1) < len(para_elements):
                    new_body_children.append(para_elements[para_idx + (k - i1)])
            para_idx += (i2 - i1)
            changes['equal'] += (i2 - i1)

        elif tag == 'delete':
            for k in range(i1, i2):
                if para_idx + (k - i1) < len(para_elements):
                    p_el = para_elements[para_idx + (k - i1)]
                    # Wrap all runs in <w:del>
                    _wrap_para_runs_in_del(p_el, rev_id, author, now)
                    rev_id += 1
                    new_body_children.append(p_el)
            para_idx += (i2 - i1)
            changes['delete'] += (i2 - i1)

        elif tag == 'insert':
            for text in paras2[j1:j2]:
                # Create new paragraph with <w:ins> markup
                p_el = _make_ins_paragraph(text, rev_id, author, now)
                rev_id += 1
                new_body_children.append(p_el)
            changes['insert'] += (j2 - j1)

        elif tag == 'replace':
            # For 1:1 paragraph replacements, do inline (word-level) diff
            # so only the changed words are marked, not the entire paragraph.
            if (i2 - i1) == 1 and (j2 - j1) == 1 and para_idx < len(para_elements):
                p_el = para_elements[para_idx]
                rev_id = _inline_diff_paragraph(p_el, paras1[i1], paras2[j1], rev_id, author, now)
                new_body_children.append(p_el)
                para_idx += 1
            else:
                # Multi-paragraph replace: fall back to delete old + insert new
                for k in range(i1, i2):
                    if para_idx + (k - i1) < len(para_elements):
                        p_el = para_elements[para_idx + (k - i1)]
                        _wrap_para_runs_in_del(p_el, rev_id, author, now)
                        rev_id += 1
                        new_body_children.append(p_el)
                para_idx += (i2 - i1)
                for text in paras2[j1:j2]:
                    p_el = _make_ins_paragraph(text, rev_id, author, now)
                    rev_id += 1
                    new_body_children.append(p_el)
            changes['replace'] += max(i2 - i1, j2 - j1)

    # Rebuild body
    for child in list(body):
        body.remove(child)
    for child in new_body_children:
        body.append(child)
    for child in non_para:
        body.append(child)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    base_doc.save(output_path)

    print(f"Redline (with track changes) saved to: {output_path}")
    print(f"Changes: {changes['delete']} deletions, {changes['insert']} insertions, "
          f"{changes['replace']} replacements, {changes['equal']} unchanged paragraphs")
    print("Open in Word and enable Review > Track Changes to see all revisions.")


def _wrap_para_runs_in_del(para_el, rev_id, author, date):
    """Wrap all runs in a paragraph inside a <w:del> element."""
    from lxml import etree

    runs = list(para_el.iter(W_TAG('r')))
    if not runs:
        return

    del_el = etree.Element(W_TAG('del'))
    del_el.set(W_TAG('id'), str(rev_id))
    del_el.set(W_TAG('author'), author)
    del_el.set(W_TAG('date'), date)

    for run in runs:
        # Convert <w:t> to <w:delText>
        for t in run.iter(W_TAG('t')):
            new_dt = etree.Element(W_TAG('delText'))
            new_dt.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            new_dt.text = t.text
            t.getparent().replace(t, new_dt)
        run.getparent().remove(run)
        del_el.append(run)

    # Insert del_el after pPr (or at start)
    ppr = para_el.find(W_TAG('pPr'))
    if ppr is not None:
        ppr.addnext(del_el)
    else:
        para_el.insert(0, del_el)


def _inline_diff_paragraph(para_el, old_text, new_text, rev_id, author, date):
    """Replace a paragraph's content with word-level tracked changes.

    Instead of deleting the entire paragraph and inserting a new one
    (which makes everything blue in Word), this does a word-level diff
    so only the changed words are marked as deletions/insertions.
    Unchanged words stay in their original formatting (black).
    """
    from lxml import etree

    old_words = old_text.split()
    new_words = new_text.split()
    matcher = difflib.SequenceMatcher(None, old_words, new_words)

    # Remove existing runs from the paragraph (keep pPr)
    ppr = para_el.find(W_TAG('pPr'))
    for child in list(para_el):
        if child.tag != W_TAG('pPr'):
            para_el.remove(child)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            # Unchanged words — plain run
            r = etree.SubElement(para_el, W_TAG('r'))
            t = etree.SubElement(r, W_TAG('t'))
            t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            t.text = ' '.join(old_words[i1:i2]) + ' '

        elif tag == 'delete':
            # Deleted words — wrap in <w:del>
            del_el = etree.SubElement(para_el, W_TAG('del'))
            del_el.set(W_TAG('id'), str(rev_id))
            del_el.set(W_TAG('author'), author)
            del_el.set(W_TAG('date'), date)
            rev_id += 1
            r = etree.SubElement(del_el, W_TAG('r'))
            dt = etree.SubElement(r, W_TAG('delText'))
            dt.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            dt.text = ' '.join(old_words[i1:i2]) + ' '

        elif tag == 'insert':
            # Inserted words — wrap in <w:ins>
            ins_el = etree.SubElement(para_el, W_TAG('ins'))
            ins_el.set(W_TAG('id'), str(rev_id))
            ins_el.set(W_TAG('author'), author)
            ins_el.set(W_TAG('date'), date)
            rev_id += 1
            r = etree.SubElement(ins_el, W_TAG('r'))
            t = etree.SubElement(r, W_TAG('t'))
            t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            t.text = ' '.join(new_words[j1:j2]) + ' '

        elif tag == 'replace':
            # Changed words — delete old, insert new
            del_el = etree.SubElement(para_el, W_TAG('del'))
            del_el.set(W_TAG('id'), str(rev_id))
            del_el.set(W_TAG('author'), author)
            del_el.set(W_TAG('date'), date)
            rev_id += 1
            r = etree.SubElement(del_el, W_TAG('r'))
            dt = etree.SubElement(r, W_TAG('delText'))
            dt.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            dt.text = ' '.join(old_words[i1:i2]) + ' '

            ins_el = etree.SubElement(para_el, W_TAG('ins'))
            ins_el.set(W_TAG('id'), str(rev_id))
            ins_el.set(W_TAG('author'), author)
            ins_el.set(W_TAG('date'), date)
            rev_id += 1
            r = etree.SubElement(ins_el, W_TAG('r'))
            t = etree.SubElement(r, W_TAG('t'))
            t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            t.text = ' '.join(new_words[j1:j2]) + ' '

    return rev_id


def _make_ins_paragraph(text, rev_id, author, date):
    """Create a new paragraph element with <w:ins> wrapped run."""
    from lxml import etree

    p = etree.Element(W_TAG('p'))
    ins_el = etree.SubElement(p, W_TAG('ins'))
    ins_el.set(W_TAG('id'), str(rev_id))
    ins_el.set(W_TAG('author'), author)
    ins_el.set(W_TAG('date'), date)

    r = etree.SubElement(ins_el, W_TAG('r'))
    t = etree.SubElement(r, W_TAG('t'))
    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    t.text = text
    return p


def _compare_visual(local1, local2, paras1, paras2, output_path):
    """Original visual comparison — red strikethrough / blue underline."""
    import docx
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_UNDERLINE

    redline = docx.Document()

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
    redline.add_paragraph()

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
            for text in paras1[i1:i2]:
                p = redline.add_paragraph()
                run = p.add_run(text)
                run.font.color.rgb = RGBColor(255, 0, 0)
                run.font.strikethrough = True
            for text in paras2[j1:j2]:
                p = redline.add_paragraph()
                run = p.add_run(text)
                run.font.color.rgb = RGBColor(0, 0, 255)
                run.font.underline = WD_UNDERLINE.SINGLE
            changes['replace'] += max(i2 - i1, j2 - j1)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    redline.save(output_path)

    print(f"Redline saved to: {output_path}")
    print(f"Changes: {changes['delete']} deletions, {changes['insert']} insertions, "
          f"{changes['replace']} replacements, {changes['equal']} unchanged paragraphs")


# ---------------------------------------------------------------------------
# comment: Add Word comments (unchanged from before)
# ---------------------------------------------------------------------------

def comment(file_path: str, comments_json_path: str, output_path: str):
    """Add Word comments to a .docx at matching paragraphs."""
    import docx
    from lxml import etree

    local_file = _resolve_file(file_path)

    with open(comments_json_path, 'r', encoding='utf-8') as f:
        comments_data = json.load(f)

    doc = docx.Document(local_file)

    nsmap = {'w': W_NS}

    from docx.opc.part import Part
    from docx.opc.packuri import PackURI

    comments_part = None
    comments_element = None

    for rel in doc.part.rels.values():
        if 'comments' in rel.reltype:
            comments_part = rel.target_part
            comments_element = etree.fromstring(comments_part.blob)
            break

    if comments_element is None:
        comments_element = etree.Element(f'{{{W_NS}}}comments', nsmap=nsmap)

    comment_id = 0
    matched = 0
    now = _now_iso()

    for entry in comments_data:
        para_match = entry.get('paragraph_match', '')
        comment_text = entry.get('comment', '')
        author = entry.get('author', 'Jeeves')

        for para in doc.paragraphs:
            if para_match.lower() in para.text.lower():
                comment_el = etree.SubElement(comments_element, f'{{{W_NS}}}comment')
                comment_el.set(f'{{{W_NS}}}id', str(comment_id))
                comment_el.set(f'{{{W_NS}}}author', author)
                comment_el.set(f'{{{W_NS}}}date', now)

                cp = etree.SubElement(comment_el, f'{{{W_NS}}}p')
                cr = etree.SubElement(cp, f'{{{W_NS}}}r')
                ct = etree.SubElement(cr, f'{{{W_NS}}}t')
                ct.text = comment_text

                para_xml = para._element
                range_start = etree.Element(f'{{{W_NS}}}commentRangeStart')
                range_start.set(f'{{{W_NS}}}id', str(comment_id))
                para_xml.insert(0, range_start)

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

    if comments_part is not None:
        comments_part._blob = etree.tostring(comments_element, xml_declaration=True, encoding='UTF-8', standalone=True)
    else:
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

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc.save(output_path)

    print(f"Commented document saved to: {output_path}")
    print(f"Comments added: {matched} of {len(comments_data)}")

    if local_file.startswith(tempfile.gettempdir()):
        os.unlink(local_file)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print("Usage:", file=sys.stderr)
        print("  python redline_tool.py compare <file1> <file2> [--track-changes] [--output redline.docx]", file=sys.stderr)
        print("  python redline_tool.py read-changes <file.docx>", file=sys.stderr)
        print("  python redline_tool.py suggest <file.docx> <changes.json> [--output suggested.docx]", file=sys.stderr)
        print("  python redline_tool.py comment <file.docx> <comments.json> [--output commented.docx]", file=sys.stderr)
        sys.exit(1)

    _ensure_deps()

    command = args[0]

    if command == 'compare':
        if len(args) < 3:
            print("ERROR: compare requires two file arguments", file=sys.stderr)
            sys.exit(1)
        output = _get_output_path(args, 'redline_output.docx')
        tc = '--track-changes' in args
        compare(args[1], args[2], output, track_changes=tc)

    elif command == 'read-changes':
        if len(args) < 2:
            print("ERROR: read-changes requires a .docx file", file=sys.stderr)
            sys.exit(1)
        read_changes(args[1])

    elif command == 'suggest':
        if len(args) < 3:
            print("ERROR: suggest requires a .docx file and a changes JSON file", file=sys.stderr)
            sys.exit(1)
        output = _get_output_path(args, 'suggested_output.docx')
        suggest(args[1], args[2], output)

    elif command == 'comment':
        if len(args) < 3:
            print("ERROR: comment requires a .docx file and a comments JSON file", file=sys.stderr)
            sys.exit(1)
        output = _get_output_path(args, 'commented_output.docx')
        comment(args[1], args[2], output)

    else:
        print(f"ERROR: Unknown command '{command}'. Use 'compare', 'read-changes', 'suggest', or 'comment'.", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
