"""Command line: rank DeathStarBench components by dependency criticality."""

from __future__ import annotations

import argparse
import csv
import json
import sys

from . import graph as graph_mod
from . import score, traces

COLUMNS = ["component", "kind", "cei", "cei_class", "centrality", "entropy", "governance", "blast_share",
           "upstream_dependents", "downstream_reach", "betweenness", "degree", "fan_out", "fan_in", "pagerank", "traces"]


def _load(args):
    deployment = graph_mod.load_map(args.deployment) if args.deployment else {}
    alias = dict(deployment.get("aliases", {}))
    for a in args.alias or []:
        k, v = a.split("=", 1)
        alias[k] = v
    ts = traces.load(args.traces, alias)
    g, cover = graph_mod.build(ts, deployment, infer_untraced_stores=not args.no_inferred_stores)
    return ts, g, cover


def _governance(path):
    if not path:
        return {}
    with open(path) as f:
        gov = json.load(f)
    bad = {k: v for k, v in gov.items() if not 0.0 <= float(v) <= 1.0}
    if bad:
        raise SystemExit(f"governance values must be in [0, 1]: {bad}")
    return {k: float(v) for k, v in gov.items()}


def cmd_rank(args):
    ts, g, cover = _load(args)
    weights = None
    if args.weights:
        a, b, c = (float(x) for x in args.weights.split(","))
        weights = {"alpha": a, "beta": b, "gamma": c}
    rows = score.rank(ts, g, cover, governance=_governance(args.governance), weights=weights,
                      bin_seconds=args.bin_seconds, entropy=args.entropy)
    if args.sort != "cei":
        rows.sort(key=lambda r: (-r[args.sort], r["component"]))
    if args.top:
        rows = rows[: args.top]
    out = open(args.out, "w", newline="") if args.out else sys.stdout
    if args.format == "json":
        json.dump({"traces": ts.n_traces, "components": rows}, out, indent=2)
        out.write("\n")
    elif args.format == "csv":
        w = csv.DictWriter(out, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()})
    else:
        out.write(f"{ts.n_traces} traces, {g.number_of_nodes()} components, {g.number_of_edges()} call edges\n")
        out.write(f"{'component':32} {'kind':10} {'CEI':>6} {'class':9} {'blast':>6} {'betw':>6} {'up':>3} {'down':>4}\n")
        for r in rows:
            out.write(f"{r['component']:32} {r['kind']:10} {r['cei']:6.3f} {r['cei_class']:9} {r['blast_share']:6.3f} "
                      f"{r['betweenness']:6.3f} {r['upstream_dependents']:3d} {r['downstream_reach']:4d}\n")
    if args.out:
        out.close()


def cmd_graph(args):
    _, g, _ = _load(args)
    out = open(args.out, "w", newline="") if args.out else sys.stdout
    w = csv.writer(out)
    w.writerow(["caller", "callee", "calls", "inferred"])
    for a, b, d in sorted(g.edges(data=True)):
        w.writerow([a, b, d.get("calls", 0), int(bool(d.get("inferred")))])
    if args.out:
        out.close()


def cmd_what_if(args):
    ts, _, cover = _load(args)
    res = score.what_if(ts, cover, [x for x in args.fail.split(",") if x])
    json.dump(res, sys.stdout, indent=2)
    sys.stdout.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dsb-criticality", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("traces", help="Jaeger JSON (.json/.json.gz) or ms_collecter trace.csv")
        p.add_argument("--deployment", help="bundled app name (socialNetwork, hotelReservation, mediaMicroservices) or a map file")
        p.add_argument("--alias", action="append", help="rename a traced service: traced=component")
        p.add_argument("--no-inferred-stores", action="store_true", help="do not add untraced datastore edges from the map")

    p = sub.add_parser("rank", help="score and rank components")
    common(p)
    p.add_argument("--governance", help="JSON {component: value in [0,1]} for the governance term")
    p.add_argument("--weights", help="alpha,beta,gamma (default 0.40,0.35,0.25)")
    p.add_argument("--bin-seconds", type=float, default=1.0)
    p.add_argument("--entropy", default="demand", choices=["demand", "flow"],
                   help="CEI entropy term: demand history (published default) or flow-edge entropy")
    p.add_argument("--sort", default="cei", choices=["cei", "blast_share", "betweenness", "upstream_dependents", "downstream_reach"])
    p.add_argument("--format", default="table", choices=["table", "csv", "json"])
    p.add_argument("--top", type=int, default=0)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_rank)

    p = sub.add_parser("graph", help="export the call graph as CSV")
    common(p)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("what-if", help="predicted share of requests a set of failures would break")
    common(p)
    p.add_argument("--fail", required=True, help="comma-separated components")
    p.set_defaults(fn=cmd_what_if)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
