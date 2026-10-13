"""Layered memory: build L2 "scenarios" from L1 mem0 facts (2026-10-12).

Pyramid (idea borrowed from TencentDB Agent Memory's L0-L3 design, built on
our own stack — no new service, no proxy):

  L0  raw conversations          LangGraph checkpoints (unchanged)
  L1  atomic facts                mem0 namespace ``deerflow_user`` (unchanged)
  L2  scenarios (THIS SCRIPT)     mem0 namespace ``memory-scenarios`` — one
                                  synthesized current-state summary per topic
                                  cluster of L1 facts
  L3  persona / history           memory.json, derived from L2 by
                                  memory_derive.py

Incremental and stable across nights:
  * existing scenarios keep their id; a centroid is recomputed from members;
  * new facts join the nearest scenario if cosine >= ASSIGN_T, otherwise sit
    in a pool that is greedily clustered; pool clusters >= MIN_SIZE become new
    scenarios, the rest wait for the next night;
  * only scenarios whose membership changed are re-synthesized (LLM) and
    re-written to Qdrant.

State: .deer-flow/memory_layers/state.json   Run: python memory_layers.py [--rebuild] [--dry-run]
"""

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND / ".deer-flow" / "memory_layers"
STATE_PATH = DATA_DIR / "state.json"

FACT_NS = "deerflow_user"
SCENARIO_NS = "memory-scenarios"
COLLECTION = "deerflow_memories"
ASSIGN_T = 0.60      # join an existing scenario
CLUSTER_T = 0.60     # greedy pool clustering
MIN_SIZE = 5         # min facts to form a scenario
MAX_FACTS_IN_PROMPT = 80
MODEL = os.environ.get("MEMORY_LAYERS_MODEL", "claude-sonnet-5-5")
WORKERS = int(os.environ.get("MEMORY_LAYERS_WORKERS", "6"))

PROMPT = """You maintain the long-term memory of an AI analyst working for Brian Mauck, Head of Capital Markets at Jeeves (fintech; debt facilities, borrowing bases, lender reporting in US/MX/CO/BR).

Below are atomic memory facts that belong to ONE topic, oldest first, each with the date it was recorded.

{facts}

Write the topic's memory page as JSON: {{"title": "...", "summary": "..."}}
- title: 3-8 words naming the topic (entity + subject), e.g. "NB facility borrowing base reporting".
- summary: <=140 words. The CURRENT state first, then the durable history that still matters. When facts conflict, the newer fact wins; say what changed and when ("as of 2026-09-30 ..."). Keep exact names, amounts, dates and file/system names exactly as written. Drop transient debugging chatter unless it is the point of the topic. Use ONLY these facts — no outside knowledge, no speculation.
Output only the JSON."""


def _log(msg: str) -> None:
    print(f"[memory_layers] {msg}", file=sys.stderr, flush=True)


def _load_env() -> None:
    shared = BACKEND.parent / "skills" / "custom" / "_shared"
    try:
        sys.path.insert(0, str(shared))
        from env_loader import load_env
        load_env()
    except Exception as e:  # gateway subprocess already has env
        _log(f"env_loader skipped: {e}")


def _qdrant():
    from qdrant_client import QdrantClient
    return QdrantClient(host=os.environ.get("MEM0_QDRANT_HOST", "localhost"),
                        port=int(os.environ.get("MEM0_QDRANT_PORT", "6333")))


def _dense(vec):
    if isinstance(vec, dict):  # named vectors (dense "" + sparse "bm25")
        for k, v in vec.items():
            if isinstance(v, list):
                return v
        return None
    return vec


def load_facts(client) -> dict:
    from qdrant_client import models as qm
    flt = qm.Filter(must=[qm.FieldCondition(key="user_id", match=qm.MatchValue(value=FACT_NS))])
    out, off = {}, None
    while True:
        batch, off = client.scroll(COLLECTION, scroll_filter=flt, limit=2000, offset=off,
                                   with_payload=True, with_vectors=True)
        for p in batch:
            v = _dense(p.vector)
            text = ((p.payload or {}).get("data") or "").strip()
            if v is None or len(v) != 384 or not text:
                continue
            v = np.asarray(v, dtype=np.float32)
            n = float(np.linalg.norm(v))
            if n == 0:
                continue
            out[str(p.id)] = {"vec": v / n, "text": text,
                              "ts": (p.payload or {}).get("created_at") or ""}
        if off is None:
            return out


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"version": 1, "scenarios": {}}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _centroid(ids, facts):
    s = np.sum([facts[i]["vec"] for i in ids], axis=0)
    return s / np.linalg.norm(s)


def _members_hash(ids) -> str:
    return hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()


def assign(state: dict, facts: dict) -> list[str]:
    """Update memberships in place; return ids of scenarios needing re-synthesis."""
    scen = state["scenarios"]
    # 1. prune deleted facts, drop scenarios that fell below MIN_SIZE
    for sid in list(scen):
        s = scen[sid]
        s["members"] = [i for i in s["members"] if i in facts]
        if len(s["members"]) < MIN_SIZE:
            s["retire"] = True
    assigned = {i for s in scen.values() if not s.get("retire") for i in s["members"]}
    live = [sid for sid, s in scen.items() if not s.get("retire")]
    cents = np.array([_centroid(scen[sid]["members"], facts) for sid in live]) if live else np.zeros((0, 384))

    # 2. new facts -> nearest scenario, else pool
    pool = []
    for fid in sorted((f for f in facts if f not in assigned), key=lambda f: facts[f]["ts"]):
        if len(live):
            sims = cents @ facts[fid]["vec"]
            j = int(sims.argmax())
            if sims[j] >= ASSIGN_T:
                scen[live[j]]["members"].append(fid)
                continue
        pool.append(fid)

    # 3. greedy-cluster the pool
    groups, gsum, gcent = [], [], []
    for fid in pool:
        v = facts[fid]["vec"]
        if gcent:
            sims = np.array(gcent) @ v
            j = int(sims.argmax())
            if sims[j] >= CLUSTER_T:
                groups[j].append(fid)
                gsum[j] = gsum[j] + v
                gcent[j] = gsum[j] / np.linalg.norm(gsum[j])
                continue
        groups.append([fid]); gsum.append(v.copy()); gcent.append(v.copy())
    for g in groups:
        if len(g) >= MIN_SIZE:
            scen[str(uuid.uuid4())] = {"members": g, "hash": "", "title": "", "summary": "",
                                       "point_id": None}

    return [sid for sid, s in scen.items()
            if not s.get("retire") and s.get("hash") != _members_hash(s["members"])]


def synthesize(members: list[str], facts: dict) -> dict:
    from anthropic import Anthropic
    ids = sorted(members, key=lambda f: facts[f]["ts"])[-MAX_FACTS_IN_PROMPT:]
    lines = "\n".join(f"- [{facts[i]['ts'][:10] or 'undated'}] {facts[i]['text']}" for i in ids)
    last_err = None
    for attempt in range(2):
        content = PROMPT.format(facts=lines)
        if attempt:
            content += ("\n\nYour previous reply was not valid JSON. Reply with ONE JSON "
                        "object only, double quotes escaped, no prose, no code fence.")
        resp = Anthropic().messages.create(model=MODEL, max_tokens=1200,
                                           messages=[{"role": "user", "content": content}])
        full = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        raw = full[full.find("{"): full.rfind("}") + 1]
        try:
            out = json.loads(raw)
            if out.get("title") and out.get("summary"):
                return {"title": out["title"].strip(), "summary": out["summary"].strip()}
            last_err = f"missing keys: {full[:200]!r}"
        except Exception as e:
            last_err = f"{e}; stop={resp.stop_reason}; head={full[:200]!r}"
    raise ValueError(last_err)


def write_scenario(mem0, sid: str, s: dict, facts: dict) -> None:
    if s.get("point_id"):
        try:
            mem0.delete(s["point_id"])
        except Exception as e:
            _log(f"delete old point {s['point_id']} failed (continuing): {e}")
    last = max(facts[i]["ts"] for i in s["members"])
    res = mem0.add([{"role": "user", "content": f"{s['title']} — {s['summary']}"}],
                   user_id=SCENARIO_NS, infer=False,
                   metadata={"layer": "L2", "scenario_id": sid, "title": s["title"],
                             "n_facts": len(s["members"]), "last_fact_at": last,
                             "member_ids": s["members"]})
    rows = res.get("results", []) if isinstance(res, dict) else (res or [])
    s["point_id"] = rows[0]["id"] if rows else None
    s["last_fact_at"] = last


def run(rebuild: bool = False, dry_run: bool = False, limit: int | None = None) -> dict:
    _load_env()
    sys.path.insert(0, str(BACKEND / "packages" / "harness"))
    client = _qdrant()
    facts = load_facts(client)
    state = {"version": 1, "scenarios": {}} if rebuild else load_state()
    dirty = assign(state, facts)
    retire = [sid for sid, s in state["scenarios"].items() if s.get("retire")]
    covered = sum(len(s["members"]) for s in state["scenarios"].values() if not s.get("retire"))
    _log(f"facts={len(facts)} scenarios={len(state['scenarios']) - len(retire)} "
         f"covered={covered} dirty={len(dirty)} retire={len(retire)}")
    if dry_run:
        return {"facts": len(facts), "dirty": len(dirty), "retire": len(retire), "covered": covered}
    if limit:
        dirty = dirty[:limit]

    from deerflow.agents.memory.mem0_store import get_mem0
    mem0 = get_mem0()
    if rebuild:  # start the L2 namespace clean so a rebuild never duplicates pages
        from qdrant_client import models as qm
        client.delete(COLLECTION, points_selector=qm.FilterSelector(filter=qm.Filter(
            must=[qm.FieldCondition(key="user_id", match=qm.MatchValue(value=SCENARIO_NS))])))
    for sid in retire:
        pid = state["scenarios"][sid].get("point_id")
        if pid:
            try:
                mem0.delete(pid)
            except Exception as e:
                _log(f"retire delete failed {pid}: {e}")
        del state["scenarios"][sid]

    ok = fail = 0
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(synthesize, state["scenarios"][sid]["members"], facts): sid for sid in dirty}
        for fut in cf.as_completed(futs):
            sid = futs[fut]
            s = state["scenarios"][sid]
            try:
                s.update(fut.result())
                write_scenario(mem0, sid, s, facts)
                s["hash"] = _members_hash(s["members"])
                s["built_at"] = datetime.now(UTC).isoformat()
                ok += 1
            except Exception as e:
                fail += 1
                _log(f"scenario {sid[:8]} failed: {e}")
            if (ok + fail) % 25 == 0:
                save_state(state)
    # never persist a scenario that has no stored point — it would look built
    for sid in [k for k, s in state["scenarios"].items() if not s.get("point_id")]:
        del state["scenarios"][sid]
    state["last_run"] = datetime.now(UTC).isoformat()
    save_state(state)
    _log(f"synthesized={ok} failed={fail} retired={len(retire)} total={len(state['scenarios'])}")
    return {"synthesized": ok, "failed": fail, "retired": len(retire), "total": len(state["scenarios"])}


def recent_scenarios(k: int = 25) -> list[dict]:
    """L2 pages ordered by most recent underlying fact (used by memory_derive for L3)."""
    st = load_state()
    rows = [s for s in st.get("scenarios", {}).values() if s.get("summary")]
    rows.sort(key=lambda s: s.get("last_fact_at", ""), reverse=True)
    return rows[:k]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    print(json.dumps(run(rebuild=a.rebuild, dry_run=a.dry_run, limit=a.limit)))
