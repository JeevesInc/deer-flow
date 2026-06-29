---
name: honcho-peers
description: Use this skill for ONA (org network analysis) queries — "who knows who", "key connectors", "path between X and Y", "most active relationships", "which LP is most connected". Also for bulk peer card operations like seeding or syncing historical data into Honcho. For individual meeting prep, use jeeves-dossier instead.
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Honcho Peer Cards — Dynamic Relationship Intelligence

Honcho is the backing store for all peer interaction history. Every email thread, Slack message, and Gemini meeting note with each person is stored as a Honcho session. The dialectic API synthesises this history into rich peer cards on demand.

**The user is Brian Mauck (brian.mauck@tryjeeves.com).**

## Setup (current deployment)

Honcho is **self-hosted** on this box — no API key. The `.env` already has:
```
HONCHO_BASE_URL=http://localhost:8000   # local Honcho server
HONCHO_APP_NAME=jeeves                   # workspace id
# HONCHO_API_KEY is intentionally unset (self-host needs none)
```

The Honcho server + its Postgres run from their own repo at `C:\Jeeves\honcho`
(not this repo's docker-compose). Start/rebuild with:
```
cd C:\Jeeves\honcho && docker compose up -d --build
```
The server listens on `localhost:8000`; its Postgres on `localhost:5433` (the
ONA Grafana export target). This stack must be running for peer cards and the
4-hour sync to work — ensure it auto-starts on boot.

To switch to Honcho cloud instead: set `HONCHO_API_KEY` (from app.honcho.dev)
and `HONCHO_BASE_URL=https://api.honcho.dev`.

## Peer Card Commands

### Read peer card (dialectic synthesis over all interactions)
```bash
python /mnt/skills/custom/honcho-peers/peer_card_tool.py read <email>
```

### Ask a specific question about a peer
```bash
python /mnt/skills/custom/honcho-peers/peer_card_tool.py query <email> "What communication style does this person prefer?"
```

### Ingest recent interactions for one peer
```bash
python /mnt/skills/custom/honcho-peers/peer_card_tool.py ingest <email> [--days 30]
```

### List all peers tracked in Honcho
```bash
python /mnt/skills/custom/honcho-peers/peer_card_tool.py list
```

### Seed from existing dossier JSONs (run once on first setup)
```bash
python /mnt/skills/custom/honcho-peers/peer_card_tool.py seed-dossiers
```

### Full historical seed — pulls raw data for all dossier contacts
```bash
python /mnt/skills/custom/honcho-peers/peer_card_tool.py seed-all [--days 365]
```

## ONA (Organisational Network Analysis) Commands

The ONA graph is built from co-occurrence: people who share meetings, email threads, or Slack channels. Edge weight = interaction frequency.

### Get the full relationship graph as JSON
```bash
python /mnt/skills/custom/honcho-peers/ona_tool.py graph [--min-weight 2]
```

Output includes nodes (people), edges (co-occurrences), and computed metrics for each node.

### Most connected people (by weighted degree)
```bash
python /mnt/skills/custom/honcho-peers/ona_tool.py top [--n 10]
```

### Shortest connection path between two people
```bash
python /mnt/skills/custom/honcho-peers/ona_tool.py path <email_a> <email_b>
```

### Bridge nodes — people who connect otherwise-separate clusters
```bash
python /mnt/skills/custom/honcho-peers/ona_tool.py bridge [--n 10]
```

### Community clusters (who forms natural groups)
```bash
python /mnt/skills/custom/honcho-peers/ona_tool.py cluster
```

### Answer a natural-language ONA question
```bash
python /mnt/skills/custom/honcho-peers/ona_tool.py query "Who are the key connectors between LP relationships and the Jeeves operations team?"
```

### Export ONA to the local Grafana (Node Graph dashboard)
Writes a readable "core network" (top tracked peers, each linked to their top
collaborators) into Honcho's local Postgres as `ona_nodes` / `ona_edges`, which
the provisioned **"ONA — Relationship Network"** Grafana dashboard renders as a
Node Graph. View at `http://localhost:3001/d/ona-network`.
```bash
python /mnt/skills/custom/honcho-peers/ona_tool.py export-postgres [--max-nodes 120] [--per-node 4]
```
The 4-hour `honcho-sync` cron refreshes this automatically after each sync.
Grafana reaches the DB via `host.docker.internal:5433` (datasource `ona-postgres`,
provisioned in `monitoring/grafana/provisioning/datasources/ona.yml`).

### Export ONA edges to Redshift (alternative, if a Redshift datasource exists)
```bash
python /mnt/skills/custom/honcho-peers/ona_tool.py export-redshift
```

## How data flows

```
Gmail threads  ──┐
Slack messages ──┼──► honcho_sync_cron.py (every 4h)
Gemini notes   ──┤         │
Calendar       ──┘    Honcho sessions/messages
                            │
                     ┌──────┴──────────────────┐
                     │                          │
              dialectic API              ONA co-occurrence
              (peer card)                (ona_graph.json)
                     │                          │
              jeeves-dossier             ona_tool.py
              prep workflow              → Grafana / Redshift
```

## ONA Edge schema (ona_graph.json)

The ONA graph is stored at `{DOSSIER_PATH}/ona_graph.json`:

```json
{
  "updated_at": "2026-06-26T10:00:00",
  "edges": [
    {
      "source": "alice@company.com",
      "target": "bob@company.com",
      "weight": 12,
      "last_interaction": "2026-06-20",
      "first_interaction": "2025-09-01",
      "sources": ["calendar", "gmail"],
      "sample_contexts": ["Q2 Review", "Deal Update Email"]
    }
  ]
}
```
