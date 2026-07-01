#!/usr/bin/env python3
"""
Knowledge Crawler — Strategic Intelligence Builder
===================================================
Incrementally crawls Drive, Gmail, and Slack to build and maintain
strategic context about deals, relationships, and business priorities.

Designed to run as a cron job (via gateway cron_supervisor) and also
callable manually by the agent or from the command line.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
# Support both container path (/mnt/skills) and native Windows path
DEER_FLOW_DIR = Path(os.environ.get(
    "DEER_FLOW_DATA_DIR",
    str(SCRIPT_DIR.parent.parent.parent / "backend" / ".deer-flow"),
))
KNOWLEDGE_DIR = DEER_FLOW_DIR / "knowledge"
STATE_FILE = KNOWLEDGE_DIR / "_crawl_state.json"
STRATEGIC_CONTEXT = DEER_FLOW_DIR / "STRATEGIC_CONTEXT.md"

# Add shared skills to path
_shared = SCRIPT_DIR.parent / "_shared"
if _shared.exists():
    sys.path.insert(0, str(_shared))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("knowledge-crawler")

# ---------------------------------------------------------------------------
# Counterparty registry — lives on disk, seeded with known parties
# ---------------------------------------------------------------------------

_SEED_COUNTERPARTIES = {
    "CIM": {
        "domains": ["cim-llc.com"],
        "drive_folders": {
            "diligence": "1bmZJORaHbvxqYeWAE-KCx4_cy4hZdtsE",
            "legal": "1bdqcBmngeKXBkUf5x5QR6zcggTA5Abuc",
        },
        "status": "active_facility",
        "source": "seed",
    },
    "Neuberger Berman": {
        "domains": ["nb.com", "neuberger.com", "nbpensions.com"],
        "drive_folders": {
            "diligence": "19fmtr7f3714EGe9j8fYFBUHmZ7_aWRz0",
            "legal": "18uJghRNqHmPLklxrRcMFl3as_JOB4Ss3",
        },
        "status": "active_facility",
        "source": "seed",
    },
    "BBVA": {
        "domains": ["bbva.com"],
        "drive_folders": {
            "diligence": "1pA5_GOqtHMTatJE5vIIYCwm-p742d5yT",
            "root": "12ns4FGnFiA6K3jH3h6cECJ2S8TD8irEf",
        },
        "status": "active_diligence",
        "source": "seed",
    },
    "Francisco Partners": {
        "domains": ["franciscopartners.com", "fp.com"],
        "drive_folders": {
            "diligence": "1Z82iHprfIyXKdxNeuvwMUSiYXeOCH67X",
            "root": "1LdmMpCmQQ5Y1UUDoxNnAZ1toWIrytJp4",
        },
        "status": "early_stage",
        "source": "seed",
    },
    "Vista Credit": {
        "domains": ["vistacredit.com", "vistaequity.com"],
        "drive_folders": {
            "root": "1ah1x2cD_wIBQrRku7xuLelS52-D0L3I8",
        },
        "status": "ddq_in_progress",
        "source": "seed",
    },
    "Covalto": {
        "domains": ["covalto.com"],
        "drive_folders": {
            "root": "11v7G67k_XSGVXn7igUTRJlVNeojmcpZO",
        },
        "status": "term_sheet",
        "source": "seed",
    },
}

WORKSPACE_FOLDERS = {
    "Portfolio Reporting": "1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC",
    "Debt Root": "1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU",
}

# Domains to ignore during discovery (internal, services, newsletters, etc.)
IGNORE_DOMAINS = {
    "tryjeeves.com", "gmail.com", "google.com", "googlemail.com",
    "outlook.com", "hotmail.com", "yahoo.com", "live.com",
    "slack.com", "notion.so", "linear.app", "github.com",
    "docusign.net", "docusign.com", "calendly.com",
    "zoom.us", "zoom.com", "microsoft.com", "office365.com",
    "anthropic.com", "openai.com", "stripe.com", "brex.com",
    "linkedin.com", "twitter.com", "x.com",
    "noreply", "no-reply", "notifications",
    "mailchimp.com", "sendgrid.net", "mailgun.org",
    "amazonaws.com", "aws.amazon.com",
    "machadomeyer.com.br", "whitecase.com",  # law firms — tracked separately
    "monex.com.mx",  # service provider
}

REGISTRY_FILE = KNOWLEDGE_DIR / "_counterparty_registry.json"


def load_counterparties() -> dict:
    """Load the counterparty registry from disk, merging with seeds."""
    registry = dict(_SEED_COUNTERPARTIES)
    if REGISTRY_FILE.exists():
        try:
            disk = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            # Merge — disk entries override seeds, but seeds fill gaps
            for name, info in disk.items():
                if name in registry:
                    # Merge: keep seed drive_folders but update everything else
                    seed_folders = registry[name].get("drive_folders", {})
                    registry[name].update(info)
                    if not registry[name].get("drive_folders"):
                        registry[name]["drive_folders"] = seed_folders
                else:
                    registry[name] = info
        except Exception as e:
            log.warning(f"Failed to load counterparty registry: {e}")
    return registry


def save_counterparties(registry: dict):
    """Save the counterparty registry to disk."""
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Counterparty discovery — scan email for new external relationships
# ---------------------------------------------------------------------------

def discover_counterparties(state: dict, force: bool = False) -> list[str]:
    """
    Scan Brian's recent sent email to discover new external domains
    he's actively corresponding with. Returns list of newly discovered names.
    """
    if not force and not should_crawl(state, "discovery", interval_hours=24):
        log.info("Discovery not due yet (24h interval)")
        return []

    log.info("Running counterparty discovery...")
    gmail = get_gmail_service()
    registry = load_counterparties()
    known_domains = set()
    for cp in registry.values():
        known_domains.update(cp.get("domains", []))

    # Scan Brian's SENT mail from the last 30 days — his sent mail reveals
    # who he's actively engaging with
    domain_contacts = {}  # domain → {count, names, subjects, first_seen}

    try:
        resp = gmail.users().messages().list(
            userId="me",
            q="in:sent newer_than:30d",
            maxResults=200,
        ).execute()
        messages = resp.get("messages", [])
        log.info(f"  Scanning {len(messages)} sent emails...")

        for msg_ref in messages:
            try:
                msg = gmail.users().messages().get(
                    userId="me", id=msg_ref["id"], format="metadata",
                    metadataHeaders=["To", "Cc", "Subject", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

                # Extract all recipient domains
                recipients = (headers.get("To", "") + "," + headers.get("Cc", "")).lower()
                subject = headers.get("Subject", "")
                date = headers.get("Date", "")

                for part in recipients.split(","):
                    part = part.strip()
                    if "@" not in part:
                        continue
                    # Extract email from "Name <email>" format
                    if "<" in part and ">" in part:
                        email = part[part.index("<") + 1:part.index(">")]
                    else:
                        email = part.split()[-1]

                    if "@" not in email:
                        continue
                    domain = email.split("@")[1].strip().lower()

                    # Skip known/ignored domains
                    if domain in IGNORE_DOMAINS or domain in known_domains:
                        continue
                    # Skip obvious noise
                    if any(x in domain for x in ["unsubscribe", "bounce", "mailer"]):
                        continue

                    if domain not in domain_contacts:
                        domain_contacts[domain] = {
                            "count": 0,
                            "names": set(),
                            "subjects": [],
                            "first_seen": date,
                        }
                    domain_contacts[domain]["count"] += 1
                    domain_contacts[domain]["names"].add(email.split("@")[0])
                    if subject and len(domain_contacts[domain]["subjects"]) < 5:
                        domain_contacts[domain]["subjects"].append(subject[:80])

                time.sleep(0.1)  # rate limit
            except Exception:
                continue

    except Exception as e:
        log.warning(f"Discovery email scan failed: {e}")
        return []

    # Filter: only domains Brian sent 3+ emails to (meaningful relationship)
    significant = {
        domain: info for domain, info in domain_contacts.items()
        if info["count"] >= 3
    }

    if not significant:
        log.info("No new counterparties discovered")
        mark_crawled(state, "discovery", 0)
        return []

    # Use Claude to classify which domains are likely counterparties vs noise
    new_parties = _classify_domains(significant)

    if new_parties:
        for name, info in new_parties.items():
            if name not in registry:
                registry[name] = info
                log.info(f"  NEW COUNTERPARTY: {name} ({', '.join(info['domains'])})")
        save_counterparties(registry)

        # Notify via Slack
        _notify_new_counterparties(new_parties)

    mark_crawled(state, "discovery", len(significant))
    return list(new_parties.keys())


def _classify_domains(domain_info: dict) -> dict:
    """Use Claude to classify discovered domains as counterparties or noise."""
    try:
        import anthropic
        client = anthropic.Anthropic()

        domain_list = "\n".join(
            f"- {domain}: {info['count']} emails, contacts: {', '.join(list(info['names'])[:5])}, "
            f"subjects: {'; '.join(info['subjects'][:3])}"
            for domain, info in sorted(domain_info.items(), key=lambda x: x[1]["count"], reverse=True)
        )

        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": f"""You are analyzing email domains from a Capital Markets professional's sent mail to identify potential counterparties (lenders, investors, fund managers, financial institutions).

DOMAINS FOUND (sorted by email frequency):
{domain_list}

For each domain that appears to be a financial counterparty, investment firm, or business relationship (NOT a service provider, law firm, vendor, or personal contact), output a JSON object:

```json
{{
  "CounterpartyName": {{
    "domains": ["domain.com"],
    "status": "discovered",
    "source": "email_discovery",
    "discovered": "{datetime.now().strftime('%Y-%m-%d')}",
    "notes": "brief description of apparent relationship"
  }}
}}
```

Only include entities that look like financial counterparties or deal-related contacts. Output ONLY the JSON, no other text. If none qualify, output `{{}}`."""}],
        )

        text = response.content[0].text.strip()
        # Extract JSON from possible markdown code block
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        return result

    except Exception as e:
        log.warning(f"Domain classification failed: {e}")
        return {}


def _notify_new_counterparties(new_parties: dict):
    """Send Slack notification about newly discovered counterparties."""
    try:
        import requests
        from env_loader import load_env
        load_env()
        token = os.environ.get("SLACK_BOT_TOKEN")
        owner = os.environ.get("SLACK_OWNER_USER_ID")
        if not token or not owner:
            return

        names = ", ".join(new_parties.keys())
        details = "\n".join(
            f"  - *{name}*: {', '.join(info.get('domains', []))} — {info.get('notes', 'no details')}"
            for name, info in new_parties.items()
        )
        message = (
            f":mag: *Knowledge crawler discovered new counterparties:*\n{details}\n"
            f"They've been added to the registry and will be included in future crawls."
        )

        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": owner, "text": message},
            timeout=10,
        )
    except Exception as e:
        log.debug(f"Slack notification failed: {e}")

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"sources": {}, "last_full_crawl": None}


def save_state(state: dict):
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def should_crawl(state: dict, source: str, interval_hours: int) -> bool:
    last = state.get("sources", {}).get(source, {}).get("last_crawled")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return datetime.now(timezone.utc) - last_dt > timedelta(hours=interval_hours)


def mark_crawled(state: dict, source: str, items_processed: int):
    if "sources" not in state:
        state["sources"] = {}
    state["sources"][source] = {
        "last_crawled": datetime.now(timezone.utc).isoformat(),
        "items_processed": items_processed,
    }


# ---------------------------------------------------------------------------
# Google API helpers
# ---------------------------------------------------------------------------

_drive_service = None
_gmail_service = None


def get_drive_service():
    global _drive_service
    if _drive_service:
        return _drive_service
    from google_auth import get_credentials
    from googleapiclient.discovery import build
    creds = get_credentials()
    _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service


def get_gmail_service():
    global _gmail_service
    if _gmail_service:
        return _gmail_service
    from google_auth import get_credentials
    from googleapiclient.discovery import build
    creds = get_credentials()
    _gmail_service = build("gmail", "v1", credentials=creds)
    return _gmail_service


def get_slack_client():
    from slack_sdk import WebClient
    token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        from env_loader import load_env
        load_env()
        token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        raise ValueError("SLACK_USER_TOKEN not set")
    return WebClient(token=token)


# ---------------------------------------------------------------------------
# Drive crawler
# ---------------------------------------------------------------------------

def crawl_drive_folder(folder_id: str, folder_name: str, max_files: int = 30) -> list[dict]:
    """List recent/modified files in a Drive folder (non-recursive, top-level only)."""
    drive = get_drive_service()
    results = []
    try:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name,mimeType,modifiedTime,size,webViewLink)",
            orderBy="modifiedTime desc",
            pageSize=max_files,
        ).execute()
        for f in resp.get("files", []):
            results.append({
                "id": f["id"],
                "name": f["name"],
                "mime": f.get("mimeType", ""),
                "modified": f.get("modifiedTime", ""),
                "link": f.get("webViewLink", ""),
                "folder": folder_name,
            })
    except Exception as e:
        log.warning(f"Drive crawl failed for {folder_name} ({folder_id}): {e}")
    return results


def crawl_drive(state: dict, force: bool = False, counterparty: str = None) -> list[dict]:
    """Crawl all counterparty Drive folders for recent documents."""
    if not force and not should_crawl(state, "drive", interval_hours=24):
        log.info("Drive crawl not due yet (24h interval)")
        return []

    log.info("Starting Drive crawl...")
    all_files = []

    targets = load_counterparties()
    if counterparty:
        targets = {k: v for k, v in targets.items() if k.lower() == counterparty.lower()}

    for cp_name, cp_info in targets.items():
        for folder_label, folder_id in cp_info.get("drive_folders", {}).items():
            label = f"{cp_name}/{folder_label}"
            log.info(f"  Scanning {label}...")
            files = crawl_drive_folder(folder_id, label, max_files=20)
            all_files.extend(files)
            time.sleep(0.5)  # rate limit

    # Also scan workspace folders
    if not counterparty:
        for name, fid in WORKSPACE_FOLDERS.items():
            log.info(f"  Scanning {name}...")
            files = crawl_drive_folder(fid, name, max_files=10)
            all_files.extend(files)
            time.sleep(0.5)

    mark_crawled(state, "drive", len(all_files))
    log.info(f"Drive crawl complete: {len(all_files)} files found")
    return all_files


# ---------------------------------------------------------------------------
# Email crawler
# ---------------------------------------------------------------------------

def crawl_email_for_domain(domains: list[str], days: int = 7, max_threads: int = 15) -> list[dict]:
    """Search Gmail for recent threads with specific domains."""
    gmail = get_gmail_service()
    results = []

    domain_query = " OR ".join(f"from:{d} OR to:{d}" for d in domains)
    query = f"({domain_query}) newer_than:{days}d"

    try:
        resp = gmail.users().messages().list(
            userId="me", q=query, maxResults=max_threads,
        ).execute()
        messages = resp.get("messages", [])

        for msg_ref in messages:
            try:
                msg = gmail.users().messages().get(
                    userId="me", id=msg_ref["id"], format="metadata",
                    metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                results.append({
                    "id": msg_ref["id"],
                    "thread_id": msg.get("threadId"),
                    "from": headers.get("From", ""),
                    "to": headers.get("To", ""),
                    "cc": headers.get("Cc", ""),
                    "subject": headers.get("Subject", ""),
                    "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                })
                time.sleep(0.2)
            except Exception as e:
                log.debug(f"  Failed to read message {msg_ref['id']}: {e}")

    except Exception as e:
        log.warning(f"Email search failed for domains {domains}: {e}")

    return results


def crawl_email(state: dict, force: bool = False, counterparty: str = None) -> dict[str, list]:
    """Crawl Gmail for recent threads with each counterparty."""
    if not force and not should_crawl(state, "email", interval_hours=6):
        log.info("Email crawl not due yet (6h interval)")
        return {}

    log.info("Starting email crawl...")
    all_threads = {}

    targets = load_counterparties()
    if counterparty:
        targets = {k: v for k, v in targets.items() if k.lower() == counterparty.lower()}

    for cp_name, cp_info in targets.items():
        domains = cp_info.get("domains", [])
        if not domains:
            continue
        log.info(f"  Searching email for {cp_name} ({', '.join(domains)})...")
        threads = crawl_email_for_domain(domains, days=7, max_threads=15)
        if threads:
            all_threads[cp_name] = threads
        time.sleep(0.5)

    total = sum(len(v) for v in all_threads.values())
    mark_crawled(state, "email", total)
    log.info(f"Email crawl complete: {total} messages across {len(all_threads)} counterparties")
    return all_threads


# ---------------------------------------------------------------------------
# Slack crawler
# ---------------------------------------------------------------------------

def crawl_slack(state: dict, force: bool = False, counterparty: str = None) -> dict[str, list]:
    """Search Slack for recent mentions of counterparties."""
    if not force and not should_crawl(state, "slack", interval_hours=6):
        log.info("Slack crawl not due yet (6h interval)")
        return {}

    log.info("Starting Slack crawl...")
    all_messages = {}

    try:
        client = get_slack_client()
    except Exception as e:
        log.warning(f"Slack client init failed: {e}")
        return {}

    targets = load_counterparties()
    if counterparty:
        targets = {k: v for k, v in targets.items() if k.lower() == counterparty.lower()}

    for cp_name in targets:
        # Search for counterparty name in recent messages
        query = f'"{cp_name}" after:{(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")}'
        try:
            resp = client.search_messages(query=query, count=15, sort="timestamp", sort_dir="desc")
            matches = resp.get("messages", {}).get("matches", [])
            if matches:
                all_messages[cp_name] = [
                    {
                        "text": m.get("text", "")[:500],
                        "user": m.get("username", ""),
                        "channel": m.get("channel", {}).get("name", ""),
                        "ts": m.get("ts", ""),
                        "permalink": m.get("permalink", ""),
                    }
                    for m in matches
                ]
            time.sleep(0.5)
        except Exception as e:
            log.warning(f"Slack search failed for {cp_name}: {e}")

    total = sum(len(v) for v in all_messages.values())
    mark_crawled(state, "slack", total)
    log.info(f"Slack crawl complete: {total} messages across {len(all_messages)} counterparties")
    return all_messages


# ---------------------------------------------------------------------------
# Synthesis — extract strategic intelligence
# ---------------------------------------------------------------------------

def synthesize(drive_files: list, email_threads: dict, slack_messages: dict) -> str:
    """Use Claude to synthesize raw crawl data into strategic intelligence."""
    import anthropic

    client = anthropic.Anthropic()

    # Build the input document
    sections = []

    if drive_files:
        recent = sorted(drive_files, key=lambda f: f.get("modified", ""), reverse=True)[:40]
        doc_list = "\n".join(
            f"- [{f['folder']}] {f['name']} (modified: {f['modified'][:10]})"
            for f in recent
        )
        sections.append(f"## Recent Drive Documents\n{doc_list}")

    for cp_name, threads in email_threads.items():
        thread_list = "\n".join(
            f"- {t['date'][:16]} | {t['subject']} | from: {t['from'][:50]} | {t['snippet'][:120]}"
            for t in threads[:15]
        )
        sections.append(f"## Email — {cp_name} (last 7 days)\n{thread_list}")

    for cp_name, msgs in slack_messages.items():
        msg_list = "\n".join(
            f"- #{m['channel']} @{m['user']}: {m['text'][:200]}"
            for m in msgs[:10]
        )
        sections.append(f"## Slack — {cp_name} (last 7 days)\n{msg_list}")

    if not sections:
        return ""

    raw_data = "\n\n".join(sections)

    # Read existing strategic context for comparison
    existing = ""
    if STRATEGIC_CONTEXT.exists():
        existing = STRATEGIC_CONTEXT.read_text(encoding="utf-8")

    prompt = f"""You are a capital markets intelligence analyst. Analyze the raw data below from Brian Mauck's Drive, email, and Slack, and extract strategic intelligence.

EXISTING STRATEGIC CONTEXT (for reference — update or add to this, don't repeat what's already known):
{existing[:3000]}

RAW DATA FROM CRAWL:
{raw_data[:12000]}

Extract and organize:

1. **Deal Status Updates** — Any changes to active deals, new counterparties, deal milestones, documents exchanged
2. **Stakeholder Intelligence** — Who is active, what they're asking for, relationship signals, new contacts
3. **Action Items & Open Requests** — Things Brian needs to do or follow up on, requests from counterparties
4. **Priority Signals** — What areas are getting the most activity, what seems urgent
5. **Strategic Insights** — Patterns, emerging opportunities, risks, or context that would help the agent serve Brian better

Format as structured markdown sections. Be specific — include names, dates, document names. Only include genuinely new or updated information, not things already in the existing context.

If the raw data doesn't contain meaningful new intelligence, just say "No significant updates from this crawl."
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        log.error(f"Synthesis failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Output — write intelligence to files
# ---------------------------------------------------------------------------

def write_intelligence(synthesis: str, drive_files: list, email_threads: dict, slack_messages: dict):
    """Write crawl results and synthesis to knowledge directory."""
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")

    # Write the synthesis
    if synthesis and "No significant updates" not in synthesis:
        brief_path = KNOWLEDGE_DIR / f"intel_{ts}.md"
        # Append to today's file (multiple crawls per day)
        with open(brief_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n---\n## Crawl at {datetime.now().strftime('%H:%M')}\n\n")
            f.write(synthesis)
        log.info(f"Intelligence brief written to {brief_path}")

        # Also update STRATEGIC_CONTEXT.md — append to Recent Learnings
        if STRATEGIC_CONTEXT.exists():
            ctx = STRATEGIC_CONTEXT.read_text(encoding="utf-8")
            # Find the Recent Learnings section and append
            marker = "## Recent Learnings"
            if marker in ctx:
                insert_point = ctx.index(marker) + len(marker)
                # Find the next line after the marker
                next_newline = ctx.index("\n", insert_point)
                # Extract first 2-3 key points from synthesis for the context file
                lines = [l.strip() for l in synthesis.split("\n") if l.strip().startswith("- ")][:3]
                if lines:
                    new_entries = f"\n*Updated {ts}:*\n" + "\n".join(lines) + "\n"
                    ctx = ctx[:next_newline] + "\n" + new_entries + ctx[next_newline:]
                    STRATEGIC_CONTEXT.write_text(ctx, encoding="utf-8")
                    log.info("Updated STRATEGIC_CONTEXT.md with new learnings")

    # Write per-counterparty summaries
    for cp_name in set(list(email_threads.keys()) + list({f["folder"].split("/")[0] for f in drive_files if "/" in f.get("folder", "")})):
        cp_safe = cp_name.lower().replace(" ", "_")
        cp_path = KNOWLEDGE_DIR / f"counterparty_{cp_safe}.json"
        existing = {}
        if cp_path.exists():
            existing = json.loads(cp_path.read_text(encoding="utf-8"))

        existing["last_updated"] = datetime.now().isoformat()
        existing["name"] = cp_name
        if cp_name in email_threads:
            existing["recent_email_subjects"] = [
                t["subject"] for t in email_threads[cp_name][:10]
            ]
        cp_files = [f for f in drive_files if cp_name in f.get("folder", "")]
        if cp_files:
            existing["recent_drive_files"] = [
                {"name": f["name"], "modified": f["modified"][:10]}
                for f in cp_files[:10]
            ]

        cp_path.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")

    log.info("Knowledge files updated")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_crawl(source: str = None, force: bool = False, counterparty: str = None):
    """Run the full or partial knowledge crawl."""
    # Load env if not already loaded
    try:
        from env_loader import load_env
        load_env()
    except Exception:
        pass

    state = load_state()
    drive_files = []
    email_threads = {}
    slack_messages = {}

    # Discovery phase — scan for new counterparties before crawling
    if not source and not counterparty:
        new_parties = discover_counterparties(state, force=force)
        if new_parties:
            log.info(f"Discovered {len(new_parties)} new counterparties: {', '.join(new_parties)}")
        save_state(state)

    sources = [source] if source else ["drive", "email", "slack"]

    if "drive" in sources:
        drive_files = crawl_drive(state, force=force, counterparty=counterparty)

    if "email" in sources:
        email_threads = crawl_email(state, force=force, counterparty=counterparty)

    if "slack" in sources:
        slack_messages = crawl_slack(state, force=force, counterparty=counterparty)

    save_state(state)

    # Synthesize if we got new data
    total_items = len(drive_files) + sum(len(v) for v in email_threads.values()) + sum(len(v) for v in slack_messages.values())
    if total_items > 0:
        log.info(f"Synthesizing {total_items} items...")
        synthesis = synthesize(drive_files, email_threads, slack_messages)
        write_intelligence(synthesis, drive_files, email_threads, slack_messages)
        if synthesis:
            try:
                print("\n" + synthesis)
            except UnicodeEncodeError:
                print("\n" + synthesis.encode("ascii", errors="replace").decode("ascii"))
    else:
        log.info("No new data to synthesize")

    return total_items


def show_status():
    """Show current crawl state."""
    state = load_state()
    print("=== Knowledge Crawler Status ===\n")
    print("Crawl sources:")
    for source, info in state.get("sources", {}).items():
        last = info.get("last_crawled", "never")
        items = info.get("items_processed", 0)
        print(f"  {source:12s}: last crawled {last[:19]}, {items} items")

    # Show counterparty registry
    registry = load_counterparties()
    seed_count = sum(1 for v in registry.values() if v.get("source") == "seed")
    discovered = sum(1 for v in registry.values() if v.get("source") != "seed")
    print(f"\nCounterparty registry: {len(registry)} total ({seed_count} seed, {discovered} discovered)")
    for name, info in sorted(registry.items()):
        domains = ", ".join(info.get("domains", []))
        source = info.get("source", "seed")
        tag = " [discovered]" if source != "seed" else ""
        print(f"  {name:25s} {domains:40s}{tag}")

    # Show knowledge files
    if KNOWLEDGE_DIR.exists():
        files = sorted(KNOWLEDGE_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        print(f"\nKnowledge files: {len(files)}")
        for f in files[:5]:
            print(f"  {f.name} ({f.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(description="Knowledge crawler — strategic intelligence builder")
    parser.add_argument("--source", choices=["drive", "email", "slack"], help="Crawl specific source only")
    parser.add_argument("--force", action="store_true", help="Ignore last-crawled timestamps")
    parser.add_argument("--counterparty", help="Crawl specific counterparty only")
    parser.add_argument("--status", action="store_true", help="Show crawl status")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    items = run_crawl(source=args.source, force=args.force, counterparty=args.counterparty)
    log.info(f"Crawl complete: {items} total items processed")


if __name__ == "__main__":
    main()
