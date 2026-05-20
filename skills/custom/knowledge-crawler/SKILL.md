---
name: knowledge-crawler
description: Builds and maintains strategic and tactical knowledge by crawling Drive folders, Gmail threads, and Slack conversations with key counterparties. Use when Brian asks to "update the knowledge base", "crawl my email", "refresh strategic context", or "what do you know about [counterparty]". Also runs automatically via cron.
allowed-tools:
  - bash
  - read_file
  - write_file
  - str_replace
---

# Knowledge Crawler — Strategic Intelligence Builder

Incrementally crawls Brian's Drive, Gmail, and Slack to build a living knowledge base of deals, relationships, priorities, and business context.

## Manual Run

```bash
# Full crawl (all sources, respects last-crawled timestamps)
python /mnt/skills/custom/knowledge-crawler/crawler.py

# Crawl specific source only
python /mnt/skills/custom/knowledge-crawler/crawler.py --source drive
python /mnt/skills/custom/knowledge-crawler/crawler.py --source email
python /mnt/skills/custom/knowledge-crawler/crawler.py --source slack

# Force re-crawl (ignore last-crawled timestamps)
python /mnt/skills/custom/knowledge-crawler/crawler.py --force

# Crawl a specific counterparty
python /mnt/skills/custom/knowledge-crawler/crawler.py --counterparty "Neuberger Berman"

# Show crawl status
python /mnt/skills/custom/knowledge-crawler/crawler.py --status
```

## What It Produces

1. **Updates `STRATEGIC_CONTEXT.md`** — deal status, stakeholder info, priorities, open items
2. **Stores facts in mem0** — searchable long-term memory for the agent
3. **Writes crawl summaries** to `.deer-flow/knowledge/` — per-counterparty intelligence briefs

## Cron Schedule

Runs automatically via gateway cron:
- **Email + Slack**: Every 6 hours (lightweight, recent messages only)
- **Drive folders**: Daily at 6 AM (heavier, document scanning)

## What It Crawls

### Drive (counterparty folders)
- Recent/modified documents in active counterparty folders
- Term sheets, credit agreements, DDQ responses, correspondence
- Extracts: deal terms, open items, document status

### Email (counterparty domains)
- Last 7 days of email threads with each counterparty
- Extracts: action items, deal status changes, relationship signals, requests

### Slack (counterparty mentions)
- Last 7 days of Slack messages mentioning counterparties
- Extracts: internal discussions, decisions, context Brian shared with team
