#!/usr/bin/env python3
"""ONA (Organisational Network Analysis) tool.

Reads the local ona_graph.json built by peer_card_tool.py ingest/seed
and answers graph queries. Also exports edges to Redshift for Grafana.

Commands:
  graph [--min-weight N] [--format json|dot]   Full adjacency graph + metrics
  top [--n 10]                                  Most connected nodes
  path <email_a> <email_b>                      Shortest connection path
  bridge [--n 10]                               Bridge nodes (betweenness centrality)
  cluster                                        Community detection
  query "<question>"                            Natural-language ONA query
  export-redshift                               Push edges table to Redshift

Env vars:
  DOSSIER_PATH                Path to dossier/ONA directory
  REDSHIFT_HOST / REDSHIFT_PORT / REDSHIFT_DATABASE / REDSHIFT_USER / REDSHIFT_SCHEMA
  REDSHIFT_PASSWORD
  ANTHROPIC_API_KEY           For NL query synthesis (optional)
  OPENAI_API_KEY              Alternative LLM for NL query
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _SKILL_DIR.parent / '_shared'
sys.path.insert(0, str(_SHARED_DIR))

from env_loader import load_env  # noqa: E402
load_env()

MY_EMAIL = os.environ.get('GOOGLE_CALENDAR_EMAIL', 'brian.mauck@tryjeeves.com').lower()


# ---------------------------------------------------------------------------
# Graph storage
# ---------------------------------------------------------------------------

def _dossier_dir() -> Path:
    base = os.environ.get('DOSSIER_PATH', '')
    if not base:
        candidate = _SKILL_DIR.parent.parent.parent / 'backend' / '.deer-flow' / 'dossiers'
        base = str(candidate.resolve())
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_graph() -> dict:
    path = _dossier_dir() / 'ona_graph.json'
    if not path.exists():
        print("ERROR: ona_graph.json not found. Run `peer_card_tool.py seed-all` first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text())


def _is_person_email(email: str) -> bool:
    """Exclude non-person addresses (calendar resources, room/group mailboxes,
    automated senders) that pollute an org-network view."""
    e = email.lower()
    bad = ('calendar.google.com', 'resource.calendar.google.com', 'noreply',
           'no-reply', 'donotreply', 'notifications@', 'mailer-daemon')
    return '@' in e and not any(b in e for b in bad)


def _tracked_peer_emails() -> set[str]:
    """Lowercased emails of all tracked dossier peers (the people Brian
    interacts with). Empty set if no dossiers exist."""
    emails: set[str] = set()
    for f in _dossier_dir().glob('*.json'):
        if f.name.startswith('_') or f.name == 'ona_graph.json':
            continue
        try:
            email = (json.loads(f.read_text()).get('email') or '').strip().lower()
            if email:
                emails.add(email)
        except Exception:
            pass
    return emails


# ---------------------------------------------------------------------------
# NetworkX wrapper (auto-install if missing)
# ---------------------------------------------------------------------------

def _ensure_networkx():
    try:
        import networkx  # noqa: F401
    except ImportError:
        import subprocess
        print("[ona] Installing networkx...", file=sys.stderr)
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'networkx'])


def _build_nx_graph(graph: dict, min_weight: int = 1):
    _ensure_networkx()
    import networkx as nx  # type: ignore

    G = nx.Graph()
    for edge in graph.get('edges', []):
        w = edge.get('weight', 1)
        if w < min_weight:
            continue
        G.add_edge(
            edge['source'],
            edge['target'],
            weight=w,
            last_interaction=edge.get('last_interaction', ''),
            sources=','.join(edge.get('sources', [])),
        )
    return G


def _compute_metrics(G) -> dict[str, dict]:
    """Return per-node metrics dict."""
    import networkx as nx

    metrics = {}

    degree = dict(G.degree(weight='weight'))
    try:
        betweenness = nx.betweenness_centrality(G, weight='weight')
    except Exception:
        betweenness = {n: 0.0 for n in G.nodes}
    try:
        closeness = nx.closeness_centrality(G, distance='weight')
    except Exception:
        closeness = {n: 0.0 for n in G.nodes}

    for node in G.nodes:
        neighbors = list(G.neighbors(node))
        last_interactions = [G.edges[node, nb].get('last_interaction', '') for nb in neighbors]
        last_interactions = [d for d in last_interactions if d]
        metrics[node] = {
            "email": node,
            "weighted_degree": round(degree.get(node, 0), 2),
            "neighbor_count": len(neighbors),
            "betweenness": round(betweenness.get(node, 0), 4),
            "closeness": round(closeness.get(node, 0), 4),
            "last_interaction": max(last_interactions) if last_interactions else '',
        }
    return metrics


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_graph(min_weight: int = 1, fmt: str = 'json'):
    graph = _load_graph()
    G = _build_nx_graph(graph, min_weight)
    metrics = _compute_metrics(G)

    edges_out = []
    for u, v, data in G.edges(data=True):
        edges_out.append({
            "source": u,
            "target": v,
            "weight": data.get('weight', 1),
            "last_interaction": data.get('last_interaction', ''),
            "sources": data.get('sources', ''),
        })

    if fmt == 'dot':
        print("graph ONA {")
        for e in edges_out:
            label = f'[label="{e["weight"]}" weight={e["weight"]}]'
            print(f'  "{e["source"]}" -- "{e["target"]}" {label};')
        print("}")
        return

    output = {
        "generated_at": datetime.now().isoformat(),
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
        "min_weight_filter": min_weight,
        "nodes": sorted(metrics.values(), key=lambda x: -x['weighted_degree']),
        "edges": sorted(edges_out, key=lambda x: -x['weight']),
    }
    print(json.dumps(output, indent=2))


def cmd_top(n: int = 10):
    graph = _load_graph()
    G = _build_nx_graph(graph)
    metrics = _compute_metrics(G)

    ranked = sorted(metrics.values(), key=lambda x: -x['weighted_degree'])
    print(f"Top {n} most connected people:\n")
    for i, m in enumerate(ranked[:n], 1):
        line = (
            f"  {i:2}. {m['email']}\n"
            f"      Connections: {m['neighbor_count']}  "
            f"Weighted degree: {m['weighted_degree']}  "
            f"Betweenness: {m['betweenness']}  "
            f"Last: {m['last_interaction']}"
        )
        print(line)
    print()


def cmd_path(email_a: str, email_b: str):
    graph = _load_graph()
    G = _build_nx_graph(graph)
    _ensure_networkx()
    import networkx as nx

    if email_a not in G:
        print(f"ERROR: {email_a} not in ONA graph.", file=sys.stderr)
        sys.exit(1)
    if email_b not in G:
        print(f"ERROR: {email_b} not in ONA graph.", file=sys.stderr)
        sys.exit(1)

    try:
        path = nx.shortest_path(G, email_a, email_b)
    except nx.NetworkXNoPath:
        print(f"No connection path between {email_a} and {email_b}.")
        return

    print(f"Shortest path ({len(path) - 1} hop{'s' if len(path) > 2 else ''}):\n")
    for i, node in enumerate(path):
        prefix = "  " + ("└─ " if i > 0 else "")
        suffix = ""
        if i < len(path) - 1:
            edge_data = G.edges[node, path[i + 1]]
            suffix = f" [weight={edge_data.get('weight', '?')}, last={edge_data.get('last_interaction', '?')}]"
        print(f"{prefix}{node}{suffix}")
    print()


def cmd_bridge(n: int = 10):
    graph = _load_graph()
    G = _build_nx_graph(graph)
    metrics = _compute_metrics(G)

    ranked = sorted(metrics.values(), key=lambda x: -x['betweenness'])
    print(f"Top {n} bridge nodes (highest betweenness centrality):\n")
    print("  Bridge nodes connect otherwise-separate clusters. Removing them fragments the network.")
    print()
    for i, m in enumerate(ranked[:n], 1):
        print(
            f"  {i:2}. {m['email']}\n"
            f"      Betweenness: {m['betweenness']}  Connections: {m['neighbor_count']}  "
            f"Weighted degree: {m['weighted_degree']}"
        )
    print()


def cmd_cluster():
    graph = _load_graph()
    G = _build_nx_graph(graph)
    _ensure_networkx()
    import networkx as nx
    import networkx.algorithms.community as nx_comm

    try:
        communities = list(nx_comm.greedy_modularity_communities(G, weight='weight'))
    except Exception as e:
        print(f"ERROR clustering: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Detected {len(communities)} communities:\n")
    for i, community in enumerate(sorted(communities, key=len, reverse=True), 1):
        members = sorted(community)
        print(f"  Cluster {i} ({len(members)} members):")
        for m in members:
            print(f"    - {m}")
        print()


def cmd_query(question: str):
    """Answer an NL question about the ONA using LLM + graph metrics."""
    graph = _load_graph()
    G = _build_nx_graph(graph)
    metrics = _compute_metrics(G)

    # Build a compact graph summary for the LLM context
    ranked = sorted(metrics.values(), key=lambda x: -x['weighted_degree'])
    top_nodes = ranked[:30]

    import networkx as nx
    try:
        communities = list(nx.algorithms.community.greedy_modularity_communities(G, weight='weight'))
    except Exception:
        communities = []

    cluster_summary = []
    for i, comm in enumerate(sorted(communities, key=len, reverse=True)[:5], 1):
        cluster_summary.append(f"Cluster {i}: {', '.join(sorted(comm)[:8])}")

    edges_sample = sorted(
        [e for e in graph.get('edges', [])],
        key=lambda x: -x.get('weight', 0),
    )[:50]

    context = {
        "question": question,
        "network_summary": {
            "total_nodes": G.number_of_nodes(),
            "total_edges": G.number_of_edges(),
            "brian_email": MY_EMAIL,
        },
        "top_30_by_degree": top_nodes,
        "top_clusters": cluster_summary,
        "top_50_edges": edges_sample,
    }

    # Try Anthropic first, then OpenAI
    prompt = (
        f"You are an ONA (Organisational Network Analysis) expert assistant for Brian Mauck.\n\n"
        f"Here is the network data:\n{json.dumps(context, indent=2)}\n\n"
        f"Question: {question}\n\n"
        f"Answer concisely based only on the data above. "
        f"Name specific people and relationships where relevant."
    )

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            print(resp.content[0].text)
            return
        except Exception as e:
            print(f"[Anthropic error: {e}]", file=sys.stderr)

    openai_key = os.environ.get('OPENAI_API_KEY')
    if openai_key:
        try:
            import openai
            client = openai.OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model='gpt-4o-mini',
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
            )
            print(resp.choices[0].message.content)
            return
        except Exception as e:
            print(f"[OpenAI error: {e}]", file=sys.stderr)

    # Fallback: just print the graph JSON and let the agent reason
    print("No LLM API key available — printing raw graph data for the agent to reason over:\n")
    print(json.dumps(context, indent=2))


def cmd_export_redshift():
    """Push ONA edges to Redshift table ona_edges for Grafana dashboards."""
    graph = _load_graph()
    G = _build_nx_graph(graph)
    metrics = _compute_metrics(G)

    # Import Redshift connection
    host = os.environ.get('REDSHIFT_HOST', '')
    port = int(os.environ.get('REDSHIFT_PORT', 5439))
    db = os.environ.get('REDSHIFT_DATABASE', '')
    user = os.environ.get('REDSHIFT_USER', '')
    password = os.environ.get('REDSHIFT_PASSWORD', '')
    schema = os.environ.get('REDSHIFT_SCHEMA', 'capital_markets_dm')

    if not all([host, db, user, password]):
        print("ERROR: Redshift env vars not set (REDSHIFT_HOST/DATABASE/USER/PASSWORD).", file=sys.stderr)
        sys.exit(1)

    try:
        import psycopg2
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'psycopg2-binary'])
        import psycopg2

    conn = psycopg2.connect(host=host, port=port, dbname=db, user=user, password=password)
    cur = conn.cursor()

    # Create tables
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema}.ona_edges (
            source          VARCHAR(255),
            target          VARCHAR(255),
            weight          INTEGER,
            last_interaction DATE,
            first_interaction DATE,
            sources         VARCHAR(255),
            updated_at      TIMESTAMP DEFAULT GETDATE()
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {schema}.ona_nodes (
            email           VARCHAR(255) PRIMARY KEY,
            weighted_degree FLOAT,
            neighbor_count  INTEGER,
            betweenness     FLOAT,
            closeness       FLOAT,
            last_interaction DATE,
            updated_at      TIMESTAMP DEFAULT GETDATE()
        )
    """)

    # Truncate and reload
    cur.execute(f"TRUNCATE TABLE {schema}.ona_edges")
    cur.execute(f"TRUNCATE TABLE {schema}.ona_nodes")

    now = datetime.now().isoformat()
    edges_count = 0
    for u, v, data in G.edges(data=True):
        last_i = data.get('last_interaction') or None
        cur.execute(
            f"INSERT INTO {schema}.ona_edges (source, target, weight, last_interaction, sources, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s)",
            (u, v, data.get('weight', 1), last_i, data.get('sources', ''), now),
        )
        edges_count += 1

    nodes_count = 0
    for m in metrics.values():
        last_i = m.get('last_interaction') or None
        cur.execute(
            f"INSERT INTO {schema}.ona_nodes (email, weighted_degree, neighbor_count, betweenness, closeness, last_interaction, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (m['email'], m['weighted_degree'], m['neighbor_count'],
             m['betweenness'], m['closeness'], last_i, now),
        )
        nodes_count += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"Exported to Redshift {schema}:")
    print(f"  ona_edges: {edges_count} rows")
    print(f"  ona_nodes: {nodes_count} rows")
    print(f"\nGrafana: query ona_edges / ona_nodes from schema '{schema}'")


def cmd_export_postgres(per_node: int = 4, max_nodes: int = 120):
    """Write ONA nodes/edges into the local (Honcho) Postgres in the shape
    Grafana's Node Graph panel expects, for the existing Grafana instance.

    Columns are stored lowercase; the dashboard aliases them to the camelCase
    names Grafana's node graph requires (mainStat, secondaryStat, arc__*, ...).
    """
    graph = _load_graph()
    G = _build_nx_graph(graph)
    metrics = _compute_metrics(G)

    # The raw co-occurrence graph has every meeting/email participant (~500
    # nodes), and ranking purely by weighted degree drowns external
    # counterparties (LPs, counsel, auditors) under internal meeting volume.
    # The meaningful "parties Brian interacts with" set is the tracked dossier
    # peers — which includes those externals. Use them as the core; fall back
    # to degree-ranking if no dossiers are present.
    total_nodes = G.number_of_nodes()
    tracked = _tracked_peer_emails()
    graph_nodes = {n for n in G.nodes if _is_person_email(n)}
    core = {e for e in tracked if e in graph_nodes and _is_person_email(e)}
    if core:
        print(f"[ona] {total_nodes} people in the full graph; focusing on the "
              f"{len(core)} tracked peers (dossiers) present in the network.")
        if len(core) > max_nodes:
            # ALWAYS keep every external counterparty — they're the point of the
            # ONA. Ranking purely by degree drowns them under internal meeting
            # volume (e.g. top-70 by degree is 100% @tryjeeves). Cap only the
            # internal side to fill the remaining slots.
            externals = {e for e in core if not e.endswith('@tryjeeves.com')}
            internals = core - externals
            slots = max(0, max_nodes - len(externals))
            top_internals = {
                m['email'] for m in sorted(
                    (metrics[e] for e in internals), key=lambda m: -m['weighted_degree']
                )[:slots]
            }
            core = externals | top_internals
            print(f"[ona] kept all {len(externals)} external peers + top "
                  f"{len(top_internals)} internal connectors (cap {max_nodes}).")
    else:
        ranked = sorted((m for m in metrics.values() if _is_person_email(m['email'])),
                        key=lambda m: -m['weighted_degree'])
        core = {m['email'] for m in ranked[:max_nodes]}
        print(f"[ona] no dossiers found; keeping top {max_nodes} by connectedness.")
    H = G.subgraph(core)

    # Within the core, a k-nearest-neighbour graph: each person keeps their
    # `per_node` strongest ties. Avoids a hairball while keeping everyone wired
    # to their closest collaborators.
    keep_keys: set[tuple] = set()
    for node in H.nodes:
        nbrs = sorted(H.edges(node, data=True), key=lambda e: -e[2].get('weight', 1))
        for u, v, _ in nbrs[:per_node]:
            keep_keys.add(tuple(sorted((u, v))))
    kept = [(u, v, H.edges[u, v]) for (u, v) in keep_keys]
    print(f"[ona] core view: {len(core)} people, each linked to their top "
          f"{per_node} collaborators → {len(kept)} edges.")
    kept_nodes = {u for u, _, _ in kept} | {v for _, v, _ in kept}

    host = os.environ.get('ONA_PG_HOST', '127.0.0.1')
    port = int(os.environ.get('ONA_PG_PORT', '5433'))
    db = os.environ.get('ONA_PG_DATABASE', 'postgres')
    user = os.environ.get('ONA_PG_USER', 'postgres')
    password = os.environ.get('ONA_PG_PASSWORD', 'postgres')

    try:
        import psycopg2
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'psycopg2-binary'])
        import psycopg2

    conn = psycopg2.connect(host=host, port=port, dbname=db, user=user, password=password)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ona_nodes (
            id               TEXT PRIMARY KEY,
            title            TEXT,
            connections      INTEGER,
            weighted_degree  DOUBLE PRECISION,
            betweenness      DOUBLE PRECISION,
            internal         DOUBLE PRECISION,
            external         DOUBLE PRECISION,
            last_interaction TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ona_edges (
            id      TEXT PRIMARY KEY,
            source  TEXT,
            target  TEXT,
            weight  BIGINT
        )
    """)
    cur.execute("TRUNCATE TABLE ona_nodes")
    cur.execute("TRUNCATE TABLE ona_edges")

    nodes_count = 0
    for email in sorted(kept_nodes):
        m = metrics.get(email, {})
        title = email.split('@')[0]
        is_internal = email.lower().endswith('@tryjeeves.com')
        cur.execute(
            "INSERT INTO ona_nodes (id, title, connections, weighted_degree, betweenness, "
            "internal, external, last_interaction) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (email, title, m.get('neighbor_count', 0), m.get('weighted_degree', 0),
             m.get('betweenness', 0), 1.0 if is_internal else 0.0,
             0.0 if is_internal else 1.0, m.get('last_interaction', '')),
        )
        nodes_count += 1

    edges_count = 0
    for u, v, data in kept:
        cur.execute(
            "INSERT INTO ona_edges (id, source, target, weight) VALUES (%s,%s,%s,%s)",
            (f"{u}|{v}", u, v, int(data.get('weight', 1))),
        )
        edges_count += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"Exported to local Postgres {host}:{port}/{db}:")
    print(f"  ona_nodes: {nodes_count} rows")
    print(f"  ona_edges: {edges_count} rows")
    print("\nGrafana: add a Postgres datasource and a Node Graph panel "
          "(see monitoring/grafana/dashboards/ona-network.json).")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == 'graph':
        min_weight = 1
        fmt = 'json'
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == '--min-weight' and i + 1 < len(sys.argv):
                min_weight = int(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == '--format' and i + 1 < len(sys.argv):
                fmt = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        cmd_graph(min_weight, fmt)

    elif cmd == 'top':
        n = 10
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == '--n' and i + 1 < len(sys.argv):
                n = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        cmd_top(n)

    elif cmd == 'path':
        if len(sys.argv) < 4:
            print("Usage: ona_tool.py path <email_a> <email_b>", file=sys.stderr)
            sys.exit(1)
        cmd_path(sys.argv[2], sys.argv[3])

    elif cmd == 'bridge':
        n = 10
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == '--n' and i + 1 < len(sys.argv):
                n = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        cmd_bridge(n)

    elif cmd == 'cluster':
        cmd_cluster()

    elif cmd == 'query':
        if len(sys.argv) < 3:
            print('Usage: ona_tool.py query "<question>"', file=sys.stderr)
            sys.exit(1)
        cmd_query(sys.argv[2])

    elif cmd == 'export-redshift':
        cmd_export_redshift()

    elif cmd == 'export-postgres':
        per_node = 4
        max_nodes = 120
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == '--per-node' and i + 1 < len(sys.argv):
                per_node = int(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == '--max-nodes' and i + 1 < len(sys.argv):
                max_nodes = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        cmd_export_postgres(per_node, max_nodes)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: graph, top, path, bridge, cluster, query, export-redshift, export-postgres", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
