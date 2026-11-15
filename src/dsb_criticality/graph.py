"""Build the component dependency graph from traced calls.

Nodes are deployable components: services and their datastores. An edge
caller -> callee means the caller needs the callee to answer. Each node also
gets the set of traced requests that touched it ("coverage").

Services trace datastore calls from the client side only, so a datastore
call ``svc::mongodb`` is mapped to the datastore instance that ``svc`` is
configured to use. That mapping comes from a deployment map (see
``maps/*.json``). Without a map, the datastore keeps the placeholder name.
"""

from __future__ import annotations

import json
from collections import defaultdict
from importlib import resources
from pathlib import Path

import networkx as nx

from .traces import TraceSet


def load_map(name_or_path) -> dict:
    """Load a deployment map by app name (bundled) or by file path."""
    p = Path(str(name_or_path))
    if p.exists():
        return json.loads(p.read_text())
    ref = resources.files("dsb_criticality") / "maps" / f"{name_or_path}.json"
    return json.loads(ref.read_text())


def build(ts: TraceSet, deployment: dict | None = None, infer_untraced_stores: bool = True):
    """Return (graph, coverage). coverage maps node -> set of trace ids."""
    deployment = deployment or {}
    stores = deployment.get("stores", {})  # service -> {kind: instance}
    gateway = deployment.get("gateway")
    g = nx.DiGraph()
    cover = defaultdict(set)
    calls_by_edge = defaultdict(int)

    def resolve(node: str) -> str:
        if "::" in node:
            svc, kind = node.split("::", 1)
            return stores.get(svc, {}).get(kind, node)
        return node

    for tid, svcs in ts.trace_services.items():
        for s in svcs:
            cover[s].add(tid)
    traced_store = defaultdict(set)  # service -> kinds seen in client spans
    for c in ts.calls:
        a, b = resolve(c.caller), resolve(c.callee)
        if "::" in c.callee:
            traced_store[c.caller.split("::")[0]].add(c.callee.split("::", 1)[1])
        calls_by_edge[(a, b)] += 1
        cover[b].add(c.trace_id)
        cover[a].add(c.trace_id)

    if infer_untraced_stores:
        # Datastores the deployment says a service uses, but whose calls are not traced:
        # assume every request through the service may reach them (an upper bound).
        for svc, kinds in stores.items():
            for kind, inst in kinds.items():
                if kind not in traced_store.get(svc, set()) and svc in cover:
                    calls_by_edge[(svc, inst)] += 0
                    cover[inst] |= cover[svc]
                    g.add_edge(svc, inst, calls=0, inferred=True)

    for (a, b), n in calls_by_edge.items():
        if g.has_edge(a, b):
            g[a][b]["calls"] += n
        else:
            g.add_edge(a, b, calls=n, inferred=False)
    for n in g.nodes:
        g.nodes[n]["kind"] = _kind(n, gateway, stores)
    return g, dict(cover)


def _kind(node: str, gateway: str | None, stores: dict) -> str:
    if node == gateway:
        return "gateway"
    for kinds in stores.values():
        for kind, inst in kinds.items():
            if inst == node:
                return kind
    if "::" in node:
        return node.split("::", 1)[1]
    return "service"
