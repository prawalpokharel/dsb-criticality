"""Dependency-criticality scores for DeathStarBench components.

The main score is the Centrality-Entropy Index (CEI) of Pokharel (FTC 2026;
IEMCON 2026):

    score_i = alpha * C_i + beta * H_i + gamma * R_i

C_i  composite centrality on the call graph: 0.35 PageRank + 0.30
     betweenness + 0.20 degree + 0.15 closeness, divided by the maximum.
H_i  Shannon entropy of the node's demand history (calls per time bin,
     divided by its own peak) over 10 bins in [0, 1], divided by log2(10).
R_i  governance term in [0, 1]: operator-declared criticality.
Defaults alpha=0.40, beta=0.35, gamma=0.25. Classes: >=0.75 critical,
>=0.50 elevated, >=0.25 moderate, else low.

Next to it the module reports the request-coverage blast radius (share of
traced requests that touched the node, the share a crash would fail if every
call is synchronous and has no fallback) and the plain graph measures it is
compared with.
"""

from __future__ import annotations

import math
from collections import defaultdict

import networkx as nx
import numpy as np

from .traces import TraceSet

DEFAULT_WEIGHTS = {"alpha": 0.40, "beta": 0.35, "gamma": 0.25}
CLASSES = (("critical", 0.75), ("elevated", 0.50), ("moderate", 0.25))


def composite_centrality(g: nx.DiGraph) -> dict:
    if g.number_of_nodes() == 0:
        return {}
    try:
        pr = nx.pagerank(g, alpha=0.85)
    except nx.PowerIterationFailedConvergence:
        pr = {n: 1.0 / g.number_of_nodes() for n in g}
    bw = nx.betweenness_centrality(g)
    dg = nx.degree_centrality(g)
    cl = nx.closeness_centrality(g)
    comp = {n: 0.35 * pr[n] + 0.30 * bw[n] + 0.20 * dg[n] + 0.15 * cl[n] for n in g}
    top = max(comp.values())
    return {n: (v / top if top > 0 else 0.0) for n, v in comp.items()}


def demand_entropy(history, bins: int = 10) -> float:
    vals = np.clip(np.asarray(list(history), dtype=float), 0.0, 1.0)
    if vals.size == 0:
        return 0.0
    hist, _ = np.histogram(vals, bins=bins, range=(0.0, 1.0))
    total = hist.sum()
    if total == 0:
        return 0.0
    p = hist[hist > 0] / total
    return float(min(1.0, -(p * np.log2(p)).sum() / math.log2(bins)))


def classify(score: float) -> str:
    for name, bar in CLASSES:
        if score >= bar:
            return name
    return "low"


def demand_histories(ts: TraceSet, cover: dict, bin_seconds: float = 1.0) -> dict:
    """Per node: requests touching it in each time bin, divided by the node's peak bin."""
    if not ts.trace_start:
        return {}
    t0 = min(ts.trace_start.values())
    t1 = max(ts.trace_start.values())
    nbins = max(1, int((t1 - t0) / (bin_seconds * 1e6)) + 1)
    out = {}
    for node, tids in cover.items():
        counts = np.zeros(nbins)
        for tid in tids:
            counts[min(nbins - 1, int((ts.trace_start[tid] - t0) / (bin_seconds * 1e6)))] += 1
        peak = counts.max()
        out[node] = (counts / peak) if peak > 0 else counts
    return out


def rank(ts: TraceSet, g: nx.DiGraph, cover: dict, governance: dict | None = None,
         weights: dict | None = None, bin_seconds: float = 1.0) -> list:
    """One row per graph node with every score, sorted by CEI descending."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    governance = governance or {}
    cen = composite_centrality(g)
    hist = demand_histories(ts, cover, bin_seconds)
    bw = nx.betweenness_centrality(g)
    try:
        pr = nx.pagerank(g, alpha=0.85)
    except nx.PowerIterationFailedConvergence:
        pr = {n: 1.0 / max(1, g.number_of_nodes()) for n in g}
    n_tr = max(1, ts.n_traces)
    rows = []
    for n in g.nodes:
        c = cen.get(n, 0.0)
        h = demand_entropy(hist.get(n, []))
        r = float(governance.get(n, 0.0))
        s = w["alpha"] * c + w["beta"] * h + w["gamma"] * r
        up = nx.ancestors(g, n)
        rows.append({
            "component": n,
            "kind": g.nodes[n].get("kind", "service"),
            "cei": s,
            "cei_class": classify(s),
            "centrality": c,
            "entropy": h,
            "governance": r,
            "blast_share": len(cover.get(n, ())) / n_tr,
            "upstream_dependents": len(up),
            "downstream_reach": len(nx.descendants(g, n)),
            "betweenness": bw.get(n, 0.0),
            "degree": g.degree(n),
            "fan_out": g.out_degree(n),
            "fan_in": g.in_degree(n),
            "pagerank": pr.get(n, 0.0),
            "traces": len(cover.get(n, ())),
        })
    rows.sort(key=lambda r: (-r["cei"], r["component"]))
    return rows


def what_if(ts: TraceSet, cover: dict, failed) -> dict:
    """Predicted share of requests that touch any failed node, overall and by root operation."""
    failed = set(failed)
    hit = set()
    for f in failed:
        hit |= cover.get(f, set())
    by_root = defaultdict(lambda: [0, 0])
    for tid, (svc, op) in ts.trace_roots.items():
        key = f"{svc} {op}".strip()
        by_root[key][1] += 1
        if tid in hit:
            by_root[key][0] += 1
    return {
        "failed": sorted(failed),
        "share": len(hit) / max(1, ts.n_traces),
        "by_root": {k: v[0] / v[1] for k, v in sorted(by_root.items())},
    }
