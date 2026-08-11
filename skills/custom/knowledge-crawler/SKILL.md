---
name: knowledge-crawler
description: Builds and maintains strategic and tactical knowledge by crawling Drive folders, Gmail threads, Slack conversations, and tracked git repos, synthesizing them into an evergreen, grep-able local wiki (.deer-flow/wiki/) plus mem0 facts and a thin STRATEGIC_CONTEXT.md pointer. Use when Brian asks to "update the knowledge base", "crawl my email", "refresh strategic context", "search the wiki", or "what do you know about [counterparty]". Also runs automatically via cron.
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

## The Wiki Layer (grep-able, evergreen, no size limit)

Inspired by LangChain's OpenWiki Brains. Solves what STRATEGIC_CONTEXT.md and
mem0 can't:

| | STRATEGIC_CONTEXT.md | mem0 | Wiki (`.deer-flow/wiki/`) |
|---|---|---|---|
| Injected every turn? | Yes (context-budget limited, must stay terse) | No (searched per-turn) | No (pulled on demand) |
| Retrieval | Always-on read | Semantic/vector (probabilistic) | Grep (exact-match, deterministic) |
| Size limit | Effectively yes | No, but recall is fuzzy | No |
| Structure | One flat file | Unstructured facts | One evergreen page per counterparty/deal/repo/topic |

**Directory layout:**
```
.deer-flow/wiki/
  index.md                    <- auto-generated table of contents
  counterparties/<slug>.md    <- one evergreen page per counterparty/lender
  deals/<slug>.md             <- one evergreen page per active facility/deal
  repos/<slug>.md             <- synthesized summary of a tracked git repo
  topics/<slug>.md            <- cross-cutting topics
```

Each page is **merged, not appended** — Claude rewrites the living body on
every update (superseding stale facts, adding new ones), with a dated
`## Update Log` tail so history isn't lost. This keeps pages current instead
of turning into ever-growing logs.

### Search the wiki

```bash
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/knowledge-crawler/wiki_search.py "obligor concentration"
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/knowledge-crawler/wiki_search.py "term sheet" --category counterparties
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/knowledge-crawler/wiki_search.py --show counterparties/neuberger_berman
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/knowledge-crawler/wiki_search.py --rebuild-index
```

Use this whenever a question needs exact-match recall (a name, a number, a
specific ask) rather than the vague-but-fast STRATEGIC_CONTEXT.md summary —
e.g. "what did Varun ask for" or "what's Jay Berger's email."

**HARD RULE — never reach into the wiki directory from bash.** Do NOT `ls`,
`cat`, `grep -r`, or glob the wiki folder by filesystem path (neither the
`.deer-flow/wiki/` host path nor its WSL-style mount equivalent). The bash
path guard rejects any token starting with a forward slash outside
`/mnt/user-data` and `/mnt/skills`, so every such attempt burns a turn and
returns "Unsafe absolute paths" — this happened 2026-07-26 while answering
"is parent guarantee mentioned in the docs," when a single
`wiki_search.py "parent guarantee"` would have returned the answer verbatim
(it did, on the next dream pass: neuberger_berman.md line 20, NB issues-list
item 6).

`wiki_search.py` resolves the wiki root internally and needs **no path
argument at all**. Search it with the query alone; use
`--show <category>/<slug>` to read a whole page. If a search returns 0 hits,
retry with a shorter or decomposed term before concluding the fact is absent.

### Git repo connector

Ingests tracked repos (seeded with `cfo-org-kb`) into `wiki/repos/<repo>.md` —
recent commits + structure, synthesized into a living summary. Does **not**
run `git pull` itself (that can hang on a credential prompt on this host) —
pull manually first if you want the latest commits reflected:

```bash
cd C:/Jeeves/redshift-bot/deer-flow/skills/custom/cfo-org-kb && git pull
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/knowledge-crawler/repo_connector.py --force
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/knowledge-crawler/repo_connector.py --add "repo-name:relative/path:description"
```

`crawler.py --source repos` also triggers this (and runs it by default in the
full crawl, gated to once per 24h like Drive).
