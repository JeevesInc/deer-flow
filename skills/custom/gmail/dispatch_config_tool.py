#!/usr/bin/env python3
"""Dispatch config tool — view and modify autonomous email dispatch settings.

The agent can call this from Slack to let Brian tweak dispatch behavior
without editing code.

Usage:
    python dispatch_config_tool.py show
    python dispatch_config_tool.py show counterparties
    python dispatch_config_tool.py show action-types
    python dispatch_config_tool.py toggle                          # toggle global dispatch
    python dispatch_config_tool.py toggle diligence                # toggle specific action type
    python dispatch_config_tool.py add-keyword diligence subject "portfolio update"
    python dispatch_config_tool.py add-keyword diligence body "monthly report"
    python dispatch_config_tool.py remove-keyword diligence subject "portfolio update"
    python dispatch_config_tool.py add-counterparty "Ares" --domains ares.com aresmgmt.com
    python dispatch_config_tool.py add-counterparty "Ares" --folder diligence 1abc123
    python dispatch_config_tool.py add-domain "Neuberger Berman" nbim.com
    python dispatch_config_tool.py add-action-type reporting --description "Monthly report requests"
    python dispatch_config_tool.py set max-concurrent-runs 3
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_shared'))
from env_loader import load_env
load_env()

CONFIG_PATH = str(
    Path(__file__).resolve().parent.parent.parent.parent
    / 'backend' / '.deer-flow' / 'dispatch_config.json'
)


def _load():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _save(config):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"Config saved to {CONFIG_PATH}")


def cmd_show(args):
    config = _load()

    if args.section == 'counterparties':
        print("=== Counterparties ===\n")
        for name, info in sorted(config.get('counterparties', {}).items()):
            domains = ', '.join(info.get('domains', []))
            folders = info.get('folders', {})
            folder_str = ', '.join(f'{k}={v}' for k, v in folders.items()) if folders else '(none)'
            print(f"  {name}")
            print(f"    Domains: {domains}")
            print(f"    Folders: {folder_str}")
        return

    if args.section == 'action-types':
        print("=== Action Types ===\n")
        for name, cfg in config.get('action_types', {}).items():
            enabled = cfg.get('enabled', True)
            desc = cfg.get('description', '')
            subj_kw = cfg.get('subject_keywords', [])
            body_kw = cfg.get('body_keywords', [])
            print(f"  {name} ({'ENABLED' if enabled else 'DISABLED'})")
            print(f"    Description: {desc}")
            print(f"    Subject keywords ({len(subj_kw)}): {', '.join(subj_kw[:5])}{'...' if len(subj_kw) > 5 else ''}")
            print(f"    Body keywords ({len(body_kw)}): {', '.join(body_kw[:5])}{'...' if len(body_kw) > 5 else ''}")
        return

    # Full overview
    enabled = config.get('enabled', True)
    max_runs = config.get('max_concurrent_runs', 2)
    n_types = len(config.get('action_types', {}))
    n_cp = len(config.get('counterparties', {}))
    active_types = [n for n, c in config.get('action_types', {}).items() if c.get('enabled', True)]

    print(f"=== Dispatch Config ===\n")
    print(f"  Global: {'ENABLED' if enabled else 'DISABLED'}")
    print(f"  Max concurrent runs: {max_runs}")
    print(f"  Action types: {n_types} ({', '.join(active_types)})")
    print(f"  Counterparties: {n_cp}")
    print(f"\nUse 'show counterparties' or 'show action-types' for details.")


def cmd_toggle(args):
    config = _load()
    if args.action_type:
        at = config.get('action_types', {}).get(args.action_type)
        if not at:
            print(f"ERROR: Action type '{args.action_type}' not found.", file=sys.stderr)
            print(f"Available: {', '.join(config.get('action_types', {}).keys())}")
            sys.exit(1)
        old = at.get('enabled', True)
        at['enabled'] = not old
        _save(config)
        print(f"Action type '{args.action_type}': {'ENABLED' if at['enabled'] else 'DISABLED'} (was {'ENABLED' if old else 'DISABLED'})")
    else:
        old = config.get('enabled', True)
        config['enabled'] = not old
        _save(config)
        print(f"Global dispatch: {'ENABLED' if config['enabled'] else 'DISABLED'} (was {'ENABLED' if old else 'DISABLED'})")


def cmd_add_keyword(args):
    config = _load()
    at = config.get('action_types', {}).get(args.action_type)
    if not at:
        print(f"ERROR: Action type '{args.action_type}' not found.", file=sys.stderr)
        sys.exit(1)

    key = f'{args.keyword_type}_keywords'
    keywords = at.get(key, [])
    kw_lower = args.keyword.lower()
    if kw_lower in [k.lower() for k in keywords]:
        print(f"Keyword '{args.keyword}' already exists in {args.action_type}.{key}")
        return
    keywords.append(args.keyword.lower())
    at[key] = keywords
    _save(config)
    print(f"Added '{args.keyword}' to {args.action_type}.{key} (now {len(keywords)} keywords)")


def cmd_remove_keyword(args):
    config = _load()
    at = config.get('action_types', {}).get(args.action_type)
    if not at:
        print(f"ERROR: Action type '{args.action_type}' not found.", file=sys.stderr)
        sys.exit(1)

    key = f'{args.keyword_type}_keywords'
    keywords = at.get(key, [])
    kw_lower = args.keyword.lower()
    original_len = len(keywords)
    keywords = [k for k in keywords if k.lower() != kw_lower]
    if len(keywords) == original_len:
        print(f"Keyword '{args.keyword}' not found in {args.action_type}.{key}")
        return
    at[key] = keywords
    _save(config)
    print(f"Removed '{args.keyword}' from {args.action_type}.{key} (now {len(keywords)} keywords)")


def cmd_add_counterparty(args):
    config = _load()
    cps = config.setdefault('counterparties', {})

    if args.name in cps:
        cp = cps[args.name]
    else:
        cp = {'domains': [], 'folders': {}}
        cps[args.name] = cp
        print(f"Created new counterparty: {args.name}")

    if args.domains:
        existing = set(cp.get('domains', []))
        added = []
        for d in args.domains:
            dl = d.lower()
            if dl not in existing:
                cp.setdefault('domains', []).append(dl)
                existing.add(dl)
                added.append(dl)
        if added:
            print(f"Added domains: {', '.join(added)}")

    if args.folder:
        label, folder_id = args.folder
        cp.setdefault('folders', {})[label] = folder_id
        print(f"Set folder {label}={folder_id}")

    _save(config)


def cmd_add_domain(args):
    config = _load()
    cp = config.get('counterparties', {}).get(args.counterparty)
    if not cp:
        print(f"ERROR: Counterparty '{args.counterparty}' not found.", file=sys.stderr)
        print(f"Available: {', '.join(config.get('counterparties', {}).keys())}")
        sys.exit(1)

    domains = cp.setdefault('domains', [])
    dl = args.domain.lower()
    if dl in domains:
        print(f"Domain '{dl}' already exists for {args.counterparty}")
        return
    domains.append(dl)
    _save(config)
    print(f"Added domain '{dl}' to {args.counterparty} (now {len(domains)} domains)")


def cmd_add_action_type(args):
    config = _load()
    ats = config.setdefault('action_types', {})
    if args.name in ats:
        print(f"Action type '{args.name}' already exists.")
        return

    ats[args.name] = {
        'enabled': True,
        'description': args.description or '',
        'subject_keywords': [],
        'body_keywords': [],
        'body_keyword_threshold': 2,
        'counterparty_substantive_words': [
            'provide', 'request', 'update', 'status', 'data',
            'document', 'question', 'review',
        ],
        'counterparty_substantive_threshold': 2,
        'prompt_template': args.name,
    }
    _save(config)
    print(f"Created action type '{args.name}'. Add keywords with:")
    print(f"  python dispatch_config_tool.py add-keyword {args.name} subject \"keyword\"")
    print(f"  python dispatch_config_tool.py add-keyword {args.name} body \"keyword\"")
    print(f"\nNote: you also need to add a prompt builder function in email_monitor_cron.py")


def cmd_set(args):
    config = _load()
    if args.key == 'max-concurrent-runs':
        try:
            val = int(args.value)
            if val < 1 or val > 10:
                print("ERROR: must be between 1 and 10", file=sys.stderr)
                sys.exit(1)
            config['max_concurrent_runs'] = val
            _save(config)
            print(f"max_concurrent_runs set to {val}")
        except ValueError:
            print(f"ERROR: '{args.value}' is not a valid integer", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown setting: {args.key}", file=sys.stderr)
        print("Available: max-concurrent-runs")
        sys.exit(1)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description='Dispatch Config Tool')
    sub = parser.add_subparsers(dest='command')

    # show
    p_show = sub.add_parser('show', help='Show config')
    p_show.add_argument('section', nargs='?', choices=['counterparties', 'action-types'],
                        help='Specific section to show')

    # toggle
    p_toggle = sub.add_parser('toggle', help='Toggle dispatch on/off')
    p_toggle.add_argument('action_type', nargs='?', help='Toggle specific action type')

    # add-keyword
    p_ak = sub.add_parser('add-keyword', help='Add a keyword to an action type')
    p_ak.add_argument('action_type', help='Action type name')
    p_ak.add_argument('keyword_type', choices=['subject', 'body'], help='Subject or body keyword')
    p_ak.add_argument('keyword', help='The keyword to add')

    # remove-keyword
    p_rk = sub.add_parser('remove-keyword', help='Remove a keyword from an action type')
    p_rk.add_argument('action_type', help='Action type name')
    p_rk.add_argument('keyword_type', choices=['subject', 'body'], help='Subject or body keyword')
    p_rk.add_argument('keyword', help='The keyword to remove')

    # add-counterparty
    p_cp = sub.add_parser('add-counterparty', help='Add or update a counterparty')
    p_cp.add_argument('name', help='Counterparty name')
    p_cp.add_argument('--domains', nargs='+', help='Email domains')
    p_cp.add_argument('--folder', nargs=2, metavar=('LABEL', 'ID'), help='Drive folder')

    # add-domain
    p_ad = sub.add_parser('add-domain', help='Add a domain to existing counterparty')
    p_ad.add_argument('counterparty', help='Counterparty name')
    p_ad.add_argument('domain', help='Domain to add')

    # add-action-type
    p_at = sub.add_parser('add-action-type', help='Create a new action type')
    p_at.add_argument('name', help='Action type name (lowercase, no spaces)')
    p_at.add_argument('--description', '-d', help='Description of what this type handles')

    # set
    p_set = sub.add_parser('set', help='Set a config value')
    p_set.add_argument('key', help='Setting name')
    p_set.add_argument('value', help='Setting value')

    args = parser.parse_args()

    commands = {
        'show': cmd_show,
        'toggle': cmd_toggle,
        'add-keyword': cmd_add_keyword,
        'remove-keyword': cmd_remove_keyword,
        'add-counterparty': cmd_add_counterparty,
        'add-domain': cmd_add_domain,
        'add-action-type': cmd_add_action_type,
        'set': cmd_set,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
