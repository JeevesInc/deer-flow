#!/usr/bin/env python3
"""Render the ONA core network as a self-contained interactive HTML file.

No Grafana, no datasource, no server — pyvis inlines vis.js so the output
opens offline by double-click. Same "core" selection as `ona_tool.py
export-postgres`: tracked dossier peers, each linked to their strongest ties.

Usage:
  python ona_html.py [output.html] [--per-node 4] [--max-nodes 120]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ona_tool as O  # noqa: E402


def build(out_path: str, per_node: int = 4, max_nodes: int = 120):
    graph = O._load_graph()
    G = O._build_nx_graph(graph)
    metrics = O._compute_metrics(G)

    # Core = tracked dossier peers present in the graph (falls back to top by
    # weighted degree), capped at max_nodes — identical to export-postgres.
    tracked = O._tracked_peer_emails()
    graph_nodes = {n for n in G.nodes if O._is_person_email(n)}
    core = {e for e in tracked if e in graph_nodes and O._is_person_email(e)}
    if not core:
        ranked = sorted((m for m in metrics.values() if O._is_person_email(m['email'])),
                        key=lambda m: -m['weighted_degree'])
        core = {m['email'] for m in ranked[:max_nodes]}
    elif len(core) > max_nodes:
        ranked = sorted((metrics[e] for e in core), key=lambda m: -m['weighted_degree'])
        core = {m['email'] for m in ranked[:max_nodes]}

    H = G.subgraph(core)
    keep: set[tuple] = set()
    for node in H.nodes:
        nbrs = sorted(H.edges(node, data=True), key=lambda e: -e[2].get('weight', 1))
        for u, v, _ in nbrs[:per_node]:
            keep.add(tuple(sorted((u, v))))
    kept_nodes = {u for u, _ in keep} | {v for _, v in keep}

    try:
        from pyvis.network import Network
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'pyvis'])
        from pyvis.network import Network

    net = Network(height='900px', width='100%', bgcolor='#0f1117',
                  font_color='#e6e6e6', cdn_resources='in_line', directed=False)
    net.barnes_hut(gravity=-9000, central_gravity=0.25, spring_length=130, spring_strength=0.02)

    for email in sorted(kept_nodes):
        m = metrics.get(email, {})
        internal = email.lower().endswith('@tryjeeves.com')
        conns = m.get('neighbor_count', 0)
        title = (f"<b>{email}</b><br>connections: {conns}"
                 f"<br>betweenness: {m.get('betweenness', 0)}"
                 f"<br>last: {m.get('last_interaction', '—')}")
        net.add_node(email, label=email.split('@')[0], title=title,
                     color='#4C8BF5' if internal else '#F5A623',
                     value=max(conns, 1),
                     borderWidth=2 if m.get('betweenness', 0) >= 0.02 else 1)

    for u, v in keep:
        w = H.edges[u, v].get('weight', 1)
        net.add_edge(u, v, value=w, title=f"co-occurrences: {w}", color='#3a3f4b')

    net.set_options('{"physics":{"stabilization":{"iterations":250}},'
                    '"interaction":{"hover":true,"tooltipDelay":80}}')
    net.save_graph(out_path)
    internal_n = sum(1 for e in kept_nodes if e.lower().endswith('@tryjeeves.com'))
    print(f"Wrote {out_path}")
    print(f"  {len(kept_nodes)} people ({internal_n} internal, {len(kept_nodes)-internal_n} external), "
          f"{len(keep)} edges.")


if __name__ == '__main__':
    out = '/mnt/c/Jeeves/redshift-bot/ona-network.html'
    per_node, max_nodes = 4, 120
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--per-node':
            per_node = int(args[i + 1]); i += 2
        elif args[i] == '--max-nodes':
            max_nodes = int(args[i + 1]); i += 2
        else:
            out = args[i]; i += 1
    build(out, per_node, max_nodes)
