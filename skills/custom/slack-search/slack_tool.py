#!/usr/bin/env python3
"""Slack search tool: search workspace messages and look up users by email.

Usage:
    python slack_tool.py search "query" [--days 30] [--count 20]
    python slack_tool.py lookup <email>

Requires env var: SLACK_USER_TOKEN (xoxp-... user token with search:read, users:read, users:read.email scopes)
"""

import os
import sys
from datetime import datetime, timedelta


def _get_client():
    token = os.environ.get('SLACK_USER_TOKEN')
    if not token:
        print("ERROR: Missing SLACK_USER_TOKEN environment variable.", file=sys.stderr)
        print("Add a Slack user token (xoxp-...) with search:read, users:read, users:read.email scopes.", file=sys.stderr)
        sys.exit(1)

    try:
        from slack_sdk import WebClient
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'slack_sdk'])
        from slack_sdk import WebClient

    return WebClient(token=token)


def cmd_search(query, days=30, count=20):
    """Search Slack messages using Slack search syntax."""
    client = _get_client()

    # Build the query with date filter if not already present
    if 'after:' not in query and 'before:' not in query:
        after_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        full_query = f"{query} after:{after_date}"
    else:
        full_query = query

    try:
        result = client.search_messages(query=full_query, count=count, sort='timestamp', sort_dir='desc')
    except Exception as e:
        print(f"ERROR: Slack search failed: {e}", file=sys.stderr)
        sys.exit(1)

    messages = result.get('messages', {}).get('matches', [])
    total = result.get('messages', {}).get('total', 0)

    if not messages:
        print(f'No messages found matching: {query}')
        return

    print(f'Found {total} message(s) matching "{query}" (showing {len(messages)}):\n')

    for msg in messages:
        ts = msg.get('ts', '')
        try:
            dt = datetime.fromtimestamp(float(ts))
            date_str = dt.strftime('%Y-%m-%d %H:%M')
        except (ValueError, OSError):
            date_str = ts

        channel_name = msg.get('channel', {}).get('name', 'unknown')
        channel_is_im = msg.get('channel', {}).get('is_im', False)
        if channel_is_im:
            channel_label = 'DM'
        else:
            channel_label = f'#{channel_name}'

        username = msg.get('username', '') or msg.get('user', 'unknown')
        text = msg.get('text', '').replace('\n', '\n  ')

        # Truncate long messages
        if len(text) > 300:
            text = text[:300] + '...'

        print(f'[{date_str}] {channel_label} \u2014 {username}')
        print(f'  {text}')
        print()


def cmd_lookup(email):
    """Look up a Slack user by email address."""
    client = _get_client()

    try:
        result = client.users_lookupByEmail(email=email)
    except Exception as e:
        err = str(e)
        if 'users_not_found' in err:
            print(f'No Slack user found for email: {email}')
            return
        print(f"ERROR: Slack lookup failed: {e}", file=sys.stderr)
        sys.exit(1)

    user = result.get('user', {})
    user_id = user.get('id', 'N/A')
    real_name = user.get('real_name', user.get('name', 'N/A'))
    display_name = user.get('profile', {}).get('display_name', '')
    title = user.get('profile', {}).get('title', '')
    tz = user.get('tz_label', '')

    print(f'User found:')
    print(f'  ID:           {user_id}')
    print(f'  Name:         {real_name}')
    if display_name:
        print(f'  Display name: {display_name}')
    if title:
        print(f'  Title:        {title}')
    if tz:
        print(f'  Timezone:     {tz}')
    print(f'  Email:        {email}')


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    if len(sys.argv) < 2:
        print("Usage: python slack_tool.py <command> [args]")
        print("Commands: search, lookup")
        sys.exit(1)

    command = sys.argv[1]

    if command == 'search':
        if len(sys.argv) < 3:
            print('Usage: python slack_tool.py search "query" [--days 30] [--count 20]', file=sys.stderr)
            sys.exit(1)
        query = sys.argv[2]
        days = 30
        count = 20
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == '--days' and i + 1 < len(sys.argv):
                days = int(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == '--count' and i + 1 < len(sys.argv):
                count = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        cmd_search(query, days=days, count=count)

    elif command == 'lookup':
        if len(sys.argv) < 3:
            print('Usage: python slack_tool.py lookup <email>', file=sys.stderr)
            sys.exit(1)
        cmd_lookup(sys.argv[2])

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
