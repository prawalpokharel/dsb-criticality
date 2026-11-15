"""Load DeathStarBench traces into a flat list of calls.

Two inputs are supported:

* Jaeger query-API JSON (``{"data": [trace, ...]}``), the format the Jaeger UI
  downloads and DeathStarBench's ``ms_collecter`` reads.
* The CSV that ``ms_collecter/trace_collect.py`` writes (columns ``traceId``,
  ``parentMS``, ``childMS``, ``childOperation``, ``startTime``, ...).

A call is one span whose parent span ran in a different service, plus every
client span that talks to a datastore (MongoDB, memcached, Redis), which the
services trace from the caller's side only.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
from dataclasses import dataclass
from pathlib import Path

STORE_PATTERNS = (
    ("mongodb", re.compile(r"mongo", re.I)),
    ("memcached", re.compile(r"memcache|(^|_)mc(_|$)", re.I)),
    ("redis", re.compile(r"redis", re.I)),
)


@dataclass(frozen=True)
class Call:
    trace_id: str
    caller: str
    callee: str  # a service name, or "<service>::<store kind>" for a datastore call
    operation: str
    start_us: int


@dataclass
class TraceSet:
    calls: list
    trace_services: dict  # trace id -> set of services that ran a span in it
    trace_roots: dict  # trace id -> (root service, root operation)
    trace_start: dict  # trace id -> start time in microseconds

    @property
    def n_traces(self) -> int:
        return len(self.trace_services)


def store_kind(operation: str) -> str | None:
    for kind, pat in STORE_PATTERNS:
        if pat.search(operation):
            return kind
    return None


def _open(path: Path):
    path = Path(path)
    if path.suffix == ".gz":
        return gzip.open(path, "rt")
    return open(path)


def load_jaeger(path, alias: dict | None = None) -> TraceSet:
    alias = alias or {}
    with _open(path) as f:
        doc = json.load(f)
    traces = doc["data"] if isinstance(doc, dict) else doc
    calls, services, roots, starts = [], {}, {}, {}
    for tr in traces:
        tid = tr["traceID"]
        procs = {pid: alias.get(p["serviceName"], p["serviceName"]) for pid, p in tr["processes"].items()}
        spans = {s["spanID"]: s for s in tr["spans"]}
        svc_of = {sid: procs[s["processID"]] for sid, s in spans.items()}
        services[tid] = set(svc_of.values())
        starts[tid] = min(s["startTime"] for s in tr["spans"]) if tr["spans"] else 0
        for sid, s in spans.items():
            parents = [r["spanID"] for r in s.get("references", []) if r.get("refType", "CHILD_OF") == "CHILD_OF"]
            parent = next((p for p in parents if p in spans), None)
            me = svc_of[sid]
            if parent is None:
                if not parents:
                    roots[tid] = (me, s["operationName"])
            elif svc_of[parent] != me:
                calls.append(Call(tid, svc_of[parent], me, s["operationName"], s["startTime"]))
            kind = store_kind(s["operationName"])
            if kind and _is_client(s):
                calls.append(Call(tid, me, f"{me}::{kind}", s["operationName"], s["startTime"]))
        if tid not in roots and spans:
            first = min(spans.values(), key=lambda s: s["startTime"])
            roots[tid] = (svc_of[first["spanID"]], first["operationName"])
    return TraceSet(calls, services, roots, starts)


def _is_client(span) -> bool:
    for t in span.get("tags", []):
        if t.get("key") == "span.kind":
            return t.get("value") == "client"
    return span["operationName"].endswith("_client") or "client" in span["operationName"]


def load_collector_csv(path, alias: dict | None = None) -> TraceSet:
    """Read the CSV written by DeathStarBench's ms_collecter/trace_collect.py."""
    alias = alias or {}
    calls, services, roots, starts = [], {}, {}, {}
    with _open(path) as f:
        for row in csv.DictReader(f):
            tid = row["traceId"]
            parent = alias.get(row["parentMS"], row["parentMS"])
            child = alias.get(row["childMS"], row["childMS"])
            start = int(float(row["startTime"]))
            services.setdefault(tid, set()).update({parent, child})
            starts[tid] = min(starts.get(tid, start), start)
            if parent != child:
                calls.append(Call(tid, parent, child, row["childOperation"], start))
            kind = store_kind(row["childOperation"])
            if kind and row["childOperation"].endswith("_client"):
                calls.append(Call(tid, child, f"{child}::{kind}", row["childOperation"], start))
    # The collector CSV has no root span row; treat a service that never appears as a callee as the root.
    callees = {c.callee for c in calls}
    for tid, svcs in services.items():
        top = sorted(s for s in svcs if s not in callees) or sorted(svcs)
        roots[tid] = (top[0], "")
    return TraceSet(calls, services, roots, starts)


def load(path, alias: dict | None = None) -> TraceSet:
    p = str(path)
    if p.endswith(".csv") or p.endswith(".csv.gz"):
        return load_collector_csv(path, alias)
    return load_jaeger(path, alias)
