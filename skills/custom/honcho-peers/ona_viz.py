#!/usr/bin/env python3
"""Render the ONA core network to a static PNG and a self-contained HTML.

No Grafana, no datasource, no CDN. The HTML inlines vis-network.min.js so it
opens offline by double-click; the PNG opens in any image viewer. Same "core"
selection as `ona_tool.py export-postgres`.

Usage:
  python ona_viz.py [--out-dir DIR] [--per-node 4] [--max-nodes 120]
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DIR))
import ona_tool as O  # noqa: E402


def _core_graph(per_node: int, max_nodes: int):
    graph = O._load_graph()
    G = O._build_nx_graph(graph)
    metrics = O._compute_metrics(G)
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

    import networkx as nx
    K = nx.Graph()
    for e in ({u for u, _ in keep} | {v for _, v in keep}):
        K.add_node(e)
    for u, v in keep:
        K.add_edge(u, v, weight=H.edges[u, v].get('weight', 1))
    return K, metrics


def _is_internal(e: str) -> bool:
    return e.lower().endswith('@tryjeeves.com')


def render_png(K, metrics, out: str):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import networkx as nx

    deg = dict(K.degree())
    # Spring layout spreads the periphery; higher k pushes the dense internal
    # core apart so it isn't one overlapping blob (kamada_kawai collapses it).
    pos = nx.spring_layout(K, k=1.4, iterations=400, seed=42)
    sizes = [120 + 80 * deg.get(n, 1) for n in K.nodes]
    colors = ['#4C8BF5' if _is_internal(n) else '#F5A623' for n in K.nodes]

    fig, ax = plt.subplots(figsize=(24, 16), facecolor='#0f1117')
    ax.set_facecolor('#0f1117')
    nx.draw_networkx_edges(K, pos, ax=ax, edge_color='#39404d', width=0.6, alpha=0.5)
    nx.draw_networkx_nodes(K, pos, ax=ax, node_size=sizes, node_color=colors,
                           edgecolors='#0f1117', linewidths=1)
    # Label the most-connected ~24 with a subtle backing box so they stay legible.
    top = sorted(K.nodes, key=lambda n: -deg.get(n, 0))[:24]
    labels = {n: n.split('@')[0] for n in top}
    nx.draw_networkx_labels(K, pos, labels=labels, ax=ax, font_size=9,
                            font_color='#ffffff',
                            bbox=dict(boxstyle='round,pad=0.15', fc='#1b1e27', ec='none', alpha=0.7))
    internal_n = sum(1 for n in K.nodes if _is_internal(n))
    ax.set_title(f"Organizational Network — {K.number_of_nodes()} tracked peers "
                 f"({internal_n} internal / {K.number_of_nodes()-internal_n} external), "
                 f"{K.number_of_edges()} strongest ties",
                 color='#e6e6e6', fontsize=16, pad=18)
    from matplotlib.lines import Line2D
    ax.legend(handles=[
        Line2D([0], [0], marker='o', color='none', markerfacecolor='#4C8BF5',
               markersize=12, label='Internal (tryjeeves)'),
        Line2D([0], [0], marker='o', color='none', markerfacecolor='#F5A623',
               markersize=12, label='External (counterparty)'),
    ], loc='lower left', facecolor='#1b1e27', labelcolor='#e6e6e6', edgecolor='#39404d')
    ax.axis('off')
    fig.tight_layout()
    fig.savefig(out, dpi=140, facecolor='#0f1117')
    plt.close(fig)
    print(f"Wrote {out}")


def render_html(K, metrics, out: str):
    vis_js = (_DIR / '_vis-network.min.js').read_text(encoding='utf-8')
    deg = dict(K.degree())
    nodes = []
    for n in sorted(K.nodes):
        m = metrics.get(n, {})
        nodes.append({
            "id": n,
            "label": n.split('@')[0],
            "title": (f"{n}\nconnections: {m.get('neighbor_count', deg.get(n,0))}"
                      f"\nbetweenness: {m.get('betweenness', 0)}"
                      f"\nlast: {m.get('last_interaction', '—')}"),
            "value": max(m.get('neighbor_count', deg.get(n, 1)), 1),
            "color": '#4C8BF5' if _is_internal(n) else '#F5A623',
        })
    edges = [{"from": u, "to": v, "value": d.get('weight', 1)} for u, v, d in K.edges(data=True)]
    internal_n = sum(1 for n in K.nodes if _is_internal(n))

    tpl = """<!doctype html><html><head><meta charset="utf-8">
<title>ONA — Relationship Network</title>
<style>
  html,body{margin:0;height:100%;background:#0f1117;font-family:Segoe UI,Arial,sans-serif;color:#e6e6e6}
  #net{width:100vw;height:100vh}
  #hdr{position:fixed;top:10px;left:14px;z-index:5;max-width:560px}
  #hdr h1{font-size:18px;margin:0 0 4px}
  #hdr p{font-size:12px;margin:0;color:#aab}
  .leg{margin-top:6px;font-size:12px}
  .dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin:0 4px -1px 10px}
</style>
<script>__VISJS__</script>
</head><body>
<div id="hdr">
  <h1>Organizational Network Analysis</h1>
  <p>__NN__ tracked peers (__IN__ internal / __EX__ external), __NE__ strongest ties.
     Drag to explore · scroll to zoom · hover for detail.</p>
  <div class="leg"><span class="dot" style="background:#4C8BF5"></span>Internal (tryjeeves)
     <span class="dot" style="background:#F5A623"></span>External (counterparty)</div>
</div>
<div id="net"></div>
<script>
  const nodes = new vis.DataSet(__NODES__);
  const edges = new vis.DataSet(__EDGES__);
  new vis.Network(document.getElementById('net'), {nodes, edges}, {
    nodes:{shape:'dot', scaling:{min:8,max:46}, font:{color:'#e6e6e6',size:13}},
    edges:{color:{color:'#39404d',highlight:'#8ab4ff'}, smooth:false,
           scaling:{min:0.4,max:6}},
    physics:{barnesHut:{gravitationalConstant:-9000,centralGravity:0.25,
             springLength:130,springConstant:0.02}, stabilization:{iterations:250}},
    interaction:{hover:true, tooltipDelay:80}
  });
</script>
</body></html>"""
    out_html = (tpl
                .replace('__VISJS__', vis_js)
                .replace('__NODES__', json.dumps(nodes))
                .replace('__EDGES__', json.dumps(edges))
                .replace('__NN__', str(K.number_of_nodes()))
                .replace('__IN__', str(internal_n))
                .replace('__EX__', str(K.number_of_nodes() - internal_n))
                .replace('__NE__', str(K.number_of_edges())))
    Path(out).write_text(out_html, encoding='utf-8')
    print(f"Wrote {out}  ({len(out_html)//1024} KB, fully self-contained)")


if __name__ == '__main__':
    out_dir = '/mnt/c/Jeeves/redshift-bot'
    per_node, max_nodes = 4, 120
    a = sys.argv[1:]
    i = 0
    while i < len(a):
        if a[i] == '--out-dir':
            out_dir = a[i + 1]; i += 2
        elif a[i] == '--per-node':
            per_node = int(a[i + 1]); i += 2
        elif a[i] == '--max-nodes':
            max_nodes = int(a[i + 1]); i += 2
        else:
            i += 1
    K, metrics = _core_graph(per_node, max_nodes)
    # HTML first (the primary, guaranteed-offline deliverable), then PNG.
    render_html(K, metrics, str(Path(out_dir) / 'ona-network.html'))
    try:
        render_png(K, metrics, str(Path(out_dir) / 'ona-network.png'))
    except Exception as e:
        print(f"PNG skipped ({e}) — HTML still written.", file=sys.stderr)
