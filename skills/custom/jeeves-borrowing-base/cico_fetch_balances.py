#!/usr/bin/env python3
"""Fetch cash balances from the most recent CICO daily email.

Searches Gmail for the latest "CICO- Daily balances" email from Axel,
downloads all image attachments, calls Claude vision to extract account
rows, and returns a dict mapping account identifiers to local-currency
balances.

Usage:
    python cico_fetch_balances.py            # prints mapping summary
    python cico_fetch_balances.py --json     # prints raw JSON

    # Or import:
    from cico_fetch_balances import fetch_balances, build_bank_accts_df
    balances = fetch_balances()   # -> dict[str, float]
    df = build_bank_accts_df(balances)
"""
import base64
import json
import os
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, '..', 'gmail'))
sys.path.insert(0, os.path.join(SCRIPT_DIR, '..', '_shared'))


def _get_gmail_service():
    import gmail_tool
    return gmail_tool._get_service()


def _search_latest_cico_email(service):
    """Find the most recent CICO daily balances email."""
    result = service.users().messages().list(
        userId='me',
        q='subject:CICO newer_than:7d from:axel@tryjeeves.com',
        maxResults=5
    ).execute()
    messages = result.get('messages', [])
    if not messages:
        result = service.users().messages().list(
            userId='me',
            q='subject:"CICO- Daily" newer_than:14d',
            maxResults=5
        ).execute()
        messages = result.get('messages', [])
    if not messages:
        return None
    msg_id = messages[0]['id']
    msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    headers = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
    return {
        'id': msg_id,
        'subject': headers.get('Subject', ''),
        'date': headers.get('Date', ''),
        'payload': msg['payload'],
    }


def _download_images(service, msg_id, payload, tmp_dir):
    """Download all image attachments from a message to tmp_dir."""
    paths = []

    def walk(parts):
        for p in parts:
            att_id = p.get('body', {}).get('attachmentId', '')
            mime = p.get('mimeType', '')
            if att_id and mime.startswith('image/'):
                att = service.users().messages().attachments().get(
                    userId='me', messageId=msg_id, id=att_id
                ).execute()
                data = base64.urlsafe_b64decode(att['data'])
                ext = 'png' if 'png' in mime else 'jpg'
                fname = 'cico_{:02d}.{}'.format(len(paths), ext)
                path = os.path.join(tmp_dir, fname)
                with open(path, 'wb') as f:
                    f.write(data)
                paths.append(path)
            if 'parts' in p:
                walk(p['parts'])

    walk(payload.get('parts', []))
    return paths


def _call_claude_vision(image_paths):
    """Send all CICO images to Claude and extract account->balance mapping."""
    try:
        import anthropic
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'anthropic'])
        import anthropic

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise RuntimeError('ANTHROPIC_API_KEY not set')

    client = anthropic.Anthropic(api_key=api_key)

    content = []
    for path in image_paths:
        with open(path, 'rb') as f:
            img_b64 = base64.standard_b64encode(f.read()).decode('ascii')
        ext = path.rsplit('.', 1)[-1].lower()
        media_type = 'image/{}'.format(ext)
        content.append({
            'type': 'image',
            'source': {'type': 'base64', 'media_type': media_type, 'data': img_b64}
        })

    content.append({
        'type': 'text',
        'text': (
            'These are screenshots of a daily bank balance report (CICO Daily Balances). '
            'Extract EVERY account row across ALL sections (Operating, Credit/Card, '
            'Self-funded, AP, Restricted). '
            'For each row return: account identifier (the exact value in the Account column) '
            'and balance (numeric value in original currency, 0 for dash/blank/"-"). '
            'Return ONLY a JSON object mapping account_identifier -> balance (number). '
            'Use the exact Account column value as the key '
            '(e.g. "8051", "6540-1", "0158", "JSE233", "3601", "0007"). '
            'Strip leading apostrophes from account numbers. '
            'Do not include currency codes or country names in the keys. '
            'If the same account number appears in multiple sections, use the first occurrence. '
            'Example output: {"8051": 1832, "6540-1": 0, "0158": 454902}'
        )
    })

    resp = client.messages.create(
        model='claude-opus-4-5',
        max_tokens=4096,
        messages=[{'role': 'user', 'content': content}]
    )

    raw = resp.content[0].text.strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
    raw = raw.strip()

    data = json.loads(raw)
    result = {}
    for k, v in data.items():
        try:
            num = float(str(v).replace(',', '')) if v not in (None, '', '-') else 0.0
        except Exception:
            num = 0.0
        result[str(k).strip()] = num
    return result


def fetch_balances(verbose=False):
    """
    Fetch balances from the most recent CICO email.

    Returns dict: {account_identifier: float_balance}
    Also includes '__email_date__' and '__email_subject__' keys.
    """
    service = _get_gmail_service()

    if verbose:
        print('Searching for latest CICO email...')
    email = _search_latest_cico_email(service)
    if not email:
        raise RuntimeError('No CICO daily balances email found in the last 14 days')

    if verbose:
        print('  Found: {} ({})'.format(email['subject'], email['date']))

    with tempfile.TemporaryDirectory() as tmp_dir:
        if verbose:
            print('Downloading images...')
        paths = _download_images(service, email['id'], email['payload'], tmp_dir)
        if not paths:
            raise RuntimeError('No image attachments found in email {}'.format(email['id']))
        if verbose:
            print('  {} images downloaded'.format(len(paths)))
            print('Calling Claude vision to extract balances...')

        balances = _call_claude_vision(paths)
        if verbose:
            print('  Extracted {} account entries'.format(len(balances)))

    balances['__email_date__'] = email['date']
    balances['__email_subject__'] = email['subject']
    return balances


# ── Account mapping: bank_accts row key -> CICO account identifier ──────────
#
# Key:   (country_value, acct_col_value)  — exact values in the bank_accts tab
# Value: account identifier in CICO email Account column
#
# To add/change an account: update this dict and ROWS below.
#
BANK_ACCTS_TO_CICO = {
    # Brazil (col B = bank label, col C = account suffix used as fallback)
    ('Brazil',        'BS2'):    '8051',
    ('Brazil',        'BTG'):    '6540-1',
    ('Brazil',        'QiTech'): '0158',
    # Colombia
    ('Colombia',      '8116'):   '8116',
    ('Colombia',      '1681'):   '1681',
    ('Colombia',      '4511'):   '4511',
    ('Colombia',      '0337'):   '0337',
    ('Colombia',      '4765'):   '4765',
    ('Colombia',      '2865'):   '2865',
    ('Colombia',      'JSE233'): 'JSE233',
    ('Colombia',      '0850'):   '0850',
    # Mexico
    ('Mexico',        '4380'):   '4380',
    ('Mexico',        '0007'):   '0007',
    # United States
    ('United States', '3601'):   '3601',
    ('United States', '6167'):   '6167',
}

_ROWS = [
    ('Brazil',        'BS2',    'BRL'),
    ('Brazil',        'BTG',    'BRL'),
    ('Brazil',        'QiTech', 'BRL'),
    ('Colombia',      '8116',   'COP'),
    ('Colombia',      '1681',   'COP'),
    ('Colombia',      '4511',   'COP'),
    ('Colombia',      '0337',   'COP'),
    ('Colombia',      '4765',   'COP'),
    ('Colombia',      '2865',   'USD'),
    ('Colombia',      'JSE233', 'COP'),
    ('Colombia',      '0850',   'COP'),
    ('Mexico',        '4380',   'MXN'),
    ('Mexico',        '0007',   'MXN'),
    ('United States', '3601',   'USD'),
    ('United States', '6167',   'USD'),
]


def build_bank_accts_df(balances):
    try:
        import pandas as pd
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'pandas'])
        import pandas as pd

    records = []
    for country, acct, currency in _ROWS:
        cico_key = BANK_ACCTS_TO_CICO.get((country, acct))
        bal = 0.0
        if cico_key:
            if cico_key in balances:
                bal = balances[cico_key]
            else:
                for k, v in balances.items():
                    if k.startswith(cico_key) or cico_key.startswith(k):
                        bal = v
                        break
        records.append({'country': country, 'acct': acct, 'currency': currency, 'bal': bal})

    return pd.DataFrame(records)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='Fetch CICO cash balances')
    p.add_argument('--json', action='store_true', help='Emit raw JSON of all balances')
    args = p.parse_args()

    result = fetch_balances(verbose=True)
    email_date = result.pop('__email_date__', '')
    email_subject = result.pop('__email_subject__', '')

    if args.json:
        import json
        print(json.dumps(result, indent=2))
    else:
        print('\nCICO: ' + str(email_subject) + ' (' + str(email_date) + ')')
        print('Accounts extracted: ' + str(len(result)))
        print('\nbank_accts mapping:')
        for (country, acct), cico_key in BANK_ACCTS_TO_CICO.items():
            bal = result.get(cico_key, 'NOT FOUND')
            print('  ' + country.ljust(15) + ' ' + acct.ljust(8) + ' -> ' + cico_key.ljust(10) + ' = ' + str(bal))
