#!/usr/bin/env python3
"""Linear tool: search issues, read details, create, and update issues.

Usage:
    python linear_tool.py me                                # My profile + assigned issues
    python linear_tool.py teams                             # List all teams
    python linear_tool.py search "query" [--team TEAM_KEY] # Search issues
    python linear_tool.py get <ISSUE-ID>                    # Get issue by identifier (e.g. ENG-42)
    python linear_tool.py create --team TEAM_KEY --title "..." [--desc "..."] [--priority 0-4] [--assignee email]
    python linear_tool.py update <ISSUE-ID> [--status "..."] [--priority 0-4] [--assignee email] [--title "..."]
    python linear_tool.py statuses --team TEAM_KEY          # List valid statuses for a team
    python linear_tool.py projects [--team TEAM_KEY]        # List projects

Priority levels: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low

Requires env var: LINEAR_API_KEY
"""

import json
import os
import sys


GRAPHQL_URL = "https://api.linear.app/graphql"


def _api_key():
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        print("ERROR: Missing LINEAR_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    return key


def _gql(query, variables=None):
    try:
        import urllib.request
    except ImportError:
        pass  # stdlib

    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": _api_key(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"ERROR: Linear API request failed: {e}", file=sys.stderr)
        sys.exit(1)

    if "errors" in data:
        for err in data["errors"]:
            print(f"ERROR: {err.get('message', err)}", file=sys.stderr)
        sys.exit(1)

    return data.get("data", {})


PRIORITY_LABELS = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


def _fmt_issue(issue, verbose=False):
    pri = PRIORITY_LABELS.get(issue.get("priority", 0), "Unknown")
    assignee = (issue.get("assignee") or {}).get("name", "Unassigned")
    state = (issue.get("state") or {}).get("name", "Unknown")
    team = (issue.get("team") or {}).get("key", "")
    identifier = issue.get("identifier", "")
    title = issue.get("title", "")
    url = issue.get("url", "")

    lines = [
        f"[{identifier}] {title}",
        f"  Status:   {state}",
        f"  Priority: {pri}",
        f"  Assignee: {assignee}",
        f"  Team:     {team}",
        f"  URL:      {url}",
    ]

    if verbose:
        desc = (issue.get("description") or "").strip()
        if desc:
            lines.append(f"  ---")
            for line in desc.splitlines()[:30]:
                lines.append(f"  {line}")

        comments = (issue.get("comments") or {}).get("nodes", [])
        if comments:
            lines.append(f"  --- Comments ({len(comments)}) ---")
            for c in comments[:5]:
                author = (c.get("user") or {}).get("name", "?")
                body = (c.get("body") or "").strip()[:300]
                lines.append(f"  [{author}]: {body}")

    return "\n".join(lines)


def cmd_me():
    data = _gql("""
        query {
            viewer {
                id
                name
                email
                assignedIssues(first: 20, orderBy: updatedAt) {
                    nodes {
                        identifier title priority url
                        state { name }
                        team { key }
                        assignee { name }
                    }
                }
            }
        }
    """)
    viewer = data.get("viewer", {})
    print(f"Name:  {viewer.get('name')}")
    print(f"Email: {viewer.get('email')}")

    issues = (viewer.get("assignedIssues") or {}).get("nodes", [])
    print(f"\nAssigned issues ({len(issues)}):\n")
    for issue in issues:
        print(_fmt_issue(issue))
        print()


def cmd_teams():
    data = _gql("""
        query {
            teams(first: 50) {
                nodes { id key name description }
            }
        }
    """)
    teams = data.get("teams", {}).get("nodes", [])
    if not teams:
        print("No teams found.")
        return
    for t in teams:
        desc = f" — {t['description']}" if t.get("description") else ""
        print(f"[{t['key']}] {t['name']}{desc}")


def cmd_statuses(team_key):
    data = _gql("""
        query($teamKey: String!) {
            teams(filter: { key: { eq: $teamKey } }) {
                nodes {
                    states(first: 50) {
                        nodes { name type }
                    }
                }
            }
        }
    """, {"teamKey": team_key})
    teams = data.get("teams", {}).get("nodes", [])
    if not teams:
        print(f"Team not found: {team_key}", file=sys.stderr)
        sys.exit(1)
    states = teams[0].get("states", {}).get("nodes", [])
    for s in states:
        print(f"  {s['name']} ({s['type']})")


def cmd_search(query, team_key=None, limit=20):
    filter_parts = [f'title: {{ containsIgnoreCase: "{query}" }}']
    if team_key:
        filter_parts.append(f'team: {{ key: {{ eq: "{team_key}" }} }}')

    filter_str = ", ".join(filter_parts)

    data = _gql(f"""
        query {{
            issues(first: {limit}, filter: {{ {filter_str} }}, orderBy: updatedAt) {{
                nodes {{
                    identifier title priority url
                    state {{ name }}
                    team {{ key }}
                    assignee {{ name }}
                }}
            }}
        }}
    """)
    issues = data.get("issues", {}).get("nodes", [])
    if not issues:
        print(f"No issues found for: {query}")
        return
    print(f"Found {len(issues)} issue(s):\n")
    for issue in issues:
        print(_fmt_issue(issue))
        print()


def cmd_get(identifier):
    data = _gql("""
        query($id: String!) {
            issue(id: $id) {
                identifier title priority url description
                state { name }
                team { key }
                assignee { name }
                comments(first: 10, orderBy: createdAt) {
                    nodes {
                        body
                        user { name }
                    }
                }
            }
        }
    """, {"id": identifier})
    issue = data.get("issue")
    if not issue:
        print(f"Issue not found: {identifier}", file=sys.stderr)
        sys.exit(1)
    print(_fmt_issue(issue, verbose=True))


def _resolve_team_id(team_key):
    data = _gql("""
        query($key: String!) {
            teams(filter: { key: { eq: $key } }) {
                nodes { id key }
            }
        }
    """, {"key": team_key})
    teams = data.get("teams", {}).get("nodes", [])
    if not teams:
        print(f"ERROR: Team not found: {team_key}", file=sys.stderr)
        sys.exit(1)
    return teams[0]["id"]


def _resolve_user_id(email):
    data = _gql("""
        query($email: String!) {
            users(filter: { email: { eq: $email } }) {
                nodes { id name email }
            }
        }
    """, {"email": email})
    users = data.get("users", {}).get("nodes", [])
    if not users:
        print(f"ERROR: User not found: {email}", file=sys.stderr)
        sys.exit(1)
    return users[0]["id"]


def _resolve_state_id(team_key, status_name):
    data = _gql("""
        query($key: String!) {
            teams(filter: { key: { eq: $key } }) {
                nodes {
                    states(first: 50) {
                        nodes { id name }
                    }
                }
            }
        }
    """, {"key": team_key})
    teams = data.get("teams", {}).get("nodes", [])
    if not teams:
        print(f"ERROR: Team not found: {team_key}", file=sys.stderr)
        sys.exit(1)
    states = teams[0].get("states", {}).get("nodes", [])
    for s in states:
        if s["name"].lower() == status_name.lower():
            return s["id"]
    names = [s["name"] for s in states]
    print(f"ERROR: Status '{status_name}' not found. Available: {', '.join(names)}", file=sys.stderr)
    sys.exit(1)


def cmd_create(team_key, title, desc=None, priority=None, assignee_email=None):
    team_id = _resolve_team_id(team_key)

    inp = {"teamId": team_id, "title": title}
    if desc:
        inp["description"] = desc
    if priority is not None:
        inp["priority"] = int(priority)
    if assignee_email:
        inp["assigneeId"] = _resolve_user_id(assignee_email)

    data = _gql("""
        mutation($input: IssueCreateInput!) {
            issueCreate(input: $input) {
                success
                issue {
                    identifier title url
                    state { name }
                    assignee { name }
                }
            }
        }
    """, {"input": inp})

    result = data.get("issueCreate", {})
    if not result.get("success"):
        print("ERROR: Issue creation failed.", file=sys.stderr)
        sys.exit(1)

    issue = result["issue"]
    print(f"Issue created: [{issue['identifier']}] {issue['title']}")
    print(f"  Status: {(issue.get('state') or {}).get('name', 'Unknown')}")
    print(f"  URL:    {issue['url']}")


def cmd_update(identifier, status=None, priority=None, assignee_email=None, title=None):
    # Get current issue to find team key for status resolution
    data = _gql("""
        query($id: String!) {
            issue(id: $id) {
                id identifier
                team { key }
            }
        }
    """, {"id": identifier})
    issue = data.get("issue")
    if not issue:
        print(f"ERROR: Issue not found: {identifier}", file=sys.stderr)
        sys.exit(1)

    issue_id = issue["id"]
    team_key = (issue.get("team") or {}).get("key")

    inp = {}
    if title:
        inp["title"] = title
    if priority is not None:
        inp["priority"] = int(priority)
    if assignee_email:
        inp["assigneeId"] = _resolve_user_id(assignee_email)
    if status:
        inp["stateId"] = _resolve_state_id(team_key, status)

    if not inp:
        print("ERROR: No updates specified.", file=sys.stderr)
        sys.exit(1)

    data = _gql("""
        mutation($id: ID!, $input: IssueUpdateInput!) {
            issueUpdate(id: $id, input: $input) {
                success
                issue {
                    identifier title url
                    state { name }
                    assignee { name }
                    priority
                }
            }
        }
    """, {"id": issue_id, "input": inp})

    result = data.get("issueUpdate", {})
    if not result.get("success"):
        print("ERROR: Issue update failed.", file=sys.stderr)
        sys.exit(1)

    updated = result["issue"]
    pri = PRIORITY_LABELS.get(updated.get("priority", 0), "Unknown")
    print(f"Updated [{updated['identifier']}] {updated['title']}")
    print(f"  Status:   {(updated.get('state') or {}).get('name', 'Unknown')}")
    print(f"  Priority: {pri}")
    print(f"  Assignee: {(updated.get('assignee') or {}).get('name', 'Unassigned')}")
    print(f"  URL:      {updated['url']}")


def cmd_projects(team_key=None):
    if team_key:
        filter_str = f'teams: {{ some: {{ key: {{ eq: "{team_key}" }} }} }}'
    else:
        filter_str = ""

    query = f"""
        query {{
            projects(first: 50{"," if filter_str else ""} {"filter: { " + filter_str + " }" if filter_str else ""}) {{
                nodes {{
                    name state slugId
                    teams {{ nodes {{ key }} }}
                }}
            }}
        }}
    """
    data = _gql(query)
    projects = data.get("projects", {}).get("nodes", [])
    if not projects:
        print("No projects found.")
        return
    for p in projects:
        teams = ", ".join(t["key"] for t in (p.get("teams") or {}).get("nodes", []))
        print(f"  {p['name']} [{p['state']}] — teams: {teams or 'none'}")


def _parse_flags(args, *flags):
    """Extract named flag values from args list. Returns (remaining_args, {flag: value})."""
    values = {f: None for f in flags}
    remaining = []
    i = 0
    while i < len(args):
        matched = False
        for f in flags:
            if args[i] == f and i + 1 < len(args):
                values[f] = args[i + 1]
                i += 2
                matched = True
                break
        if not matched:
            remaining.append(args[i])
            i += 1
    return remaining, values


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd == "me":
        cmd_me()

    elif cmd == "teams":
        cmd_teams()

    elif cmd == "statuses":
        _, flags = _parse_flags(rest, "--team")
        if not flags["--team"]:
            print("Usage: python linear_tool.py statuses --team TEAM_KEY", file=sys.stderr)
            sys.exit(1)
        cmd_statuses(flags["--team"])

    elif cmd == "search":
        if not rest:
            print('Usage: python linear_tool.py search "query" [--team TEAM_KEY]', file=sys.stderr)
            sys.exit(1)
        query = rest[0]
        _, flags = _parse_flags(rest[1:], "--team")
        cmd_search(query, team_key=flags["--team"])

    elif cmd == "get":
        if not rest:
            print("Usage: python linear_tool.py get <ISSUE-ID>", file=sys.stderr)
            sys.exit(1)
        cmd_get(rest[0])

    elif cmd == "create":
        _, flags = _parse_flags(rest, "--team", "--title", "--desc", "--priority", "--assignee")
        if not flags["--team"] or not flags["--title"]:
            print('Usage: python linear_tool.py create --team TEAM_KEY --title "..." [--desc "..."] [--priority 0-4] [--assignee email]', file=sys.stderr)
            sys.exit(1)
        cmd_create(
            flags["--team"],
            flags["--title"],
            desc=flags["--desc"],
            priority=flags["--priority"],
            assignee_email=flags["--assignee"],
        )

    elif cmd == "update":
        if not rest:
            print("Usage: python linear_tool.py update <ISSUE-ID> [--status ...] [--priority 0-4] [--assignee email] [--title ...]", file=sys.stderr)
            sys.exit(1)
        identifier = rest[0]
        _, flags = _parse_flags(rest[1:], "--status", "--priority", "--assignee", "--title")
        cmd_update(
            identifier,
            status=flags["--status"],
            priority=flags["--priority"],
            assignee_email=flags["--assignee"],
            title=flags["--title"],
        )

    elif cmd == "projects":
        _, flags = _parse_flags(rest, "--team")
        cmd_projects(team_key=flags["--team"])

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: me, teams, statuses, search, get, create, update, projects")
        sys.exit(1)


if __name__ == "__main__":
    main()
