#!/usr/bin/env python3
"""
setup_gmail_watch.py — Configure Gmail push notifications via Google Pub/Sub

Run this ONCE after your webhook receiver is live and you have a Pub/Sub topic.

Prerequisites:
  1. GCP project with Pub/Sub API enabled
  2. A Pub/Sub topic created:
       gcloud pubsub topics create gmail-push
  3. Grant Gmail publish rights to the topic:
       gcloud pubsub topics add-iam-policy-binding gmail-push \
         --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
         --role="roles/pubsub.publisher"
  4. Create a push subscription pointing at your webhook:
       gcloud pubsub subscriptions create gmail-push-sub \
         --topic gmail-push \
         --push-endpoint https://YOUR-TUNNEL/webhook/gmail \
         --ack-deadline 30
  5. Run this script to activate Gmail watch:
       python setup_gmail_watch.py --topic projects/YOUR-PROJECT/topics/gmail-push

The watch expires after 7 days — re-run to renew, or set up a cron:
  # Renew every 6 days
  0 9 */6 * * cd /path/to/webhook && python setup_gmail_watch.py --topic projects/YOUR-PROJECT/topics/gmail-push
"""

import argparse
import os
import sys
from pathlib import Path

# Load env and the shared google_auth helper (skills live one level up from backend/)
_shared = Path(__file__).resolve().parent.parent / 'skills' / 'custom' / '_shared'
if _shared.exists():
    sys.path.insert(0, str(_shared))
    try:
        from env_loader import load_env
        load_env()
    except ImportError:
        pass

def watch_gmail(topic_name: str, label_ids: list[str] = None):
    import json
    from google_auth import get_credentials
    from googleapiclient.discovery import build

    creds = get_credentials(required=True)
    service = build('gmail', 'v1', credentials=creds)

    request_body = {
        'topicName': topic_name,
        'labelIds': label_ids or ['INBOX'],
        'labelFilterAction': 'include',
    }

    print(f'Setting up Gmail watch...')
    print(f'  Topic: {topic_name}')
    print(f'  Labels: {request_body["labelIds"]}')

    result = service.users().watch(userId='me', body=request_body).execute()
    history_id = int(result.get('historyId', 0))

    print(f'\nSuccess!')
    print(f'  History ID: {history_id}')
    print(f'  Expiration: {result.get("expiration")} (Unix ms)')

    import datetime
    exp_ts = int(result.get('expiration', 0)) / 1000
    exp_dt = datetime.datetime.fromtimestamp(exp_ts)
    print(f'  Expires at: {exp_dt.strftime("%Y-%m-%d %H:%M:%S")} local time')

    # Seed the webhook's state file so the first push has a valid history starting point.
    state_path = Path(__file__).resolve().parent / '.webhook_gmail_state.json'
    existing = {}
    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text())
        except Exception:
            existing = {}
    # Only overwrite last_history_id if missing or rewinding (e.g. fresh watch).
    if not existing.get('last_history_id') or existing.get('last_history_id', 0) > history_id:
        existing['last_history_id'] = history_id
        existing.setdefault('seen_ids', [])
        state_path.write_text(json.dumps(existing, indent=2))
        print(f'  Wrote initial state to {state_path.name} (last_history_id={history_id})')

    print(f'\nRemember to re-run this before it expires (set a 6-day cron).')

    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Set up Gmail push notifications')
    parser.add_argument('--topic', required=True,
                        help='Pub/Sub topic name (e.g. projects/my-project/topics/gmail-push)')
    parser.add_argument('--labels', nargs='+', default=['INBOX'],
                        help='Gmail label IDs to watch (default: INBOX)')
    args = parser.parse_args()

    watch_gmail(args.topic, args.labels)
