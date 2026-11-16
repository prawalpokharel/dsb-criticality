import csv
import gzip
import json
import math

import networkx as nx
import pytest

from dsb_criticality import build, load, rank, what_if
from dsb_criticality.cli import main as cli_main
from dsb_criticality.score import classify, composite_centrality, demand_entropy
from dsb_criticality.traces import store_kind

# A small social-network-like app: gateway -> compose -> {text, user}; text -> url (+ mongo);
# gateway -> timeline -> storage (+ memcached). Reads are 3x as common as composes.
DEPLOYMENT = {
    "gateway": "gw",
    "aliases": {"nginx": "gw"},
    "stores": {"url": {"mongodb": "url-mongodb"}, "storage": {"memcached": "storage-mc", "mongodb": "storage-mongodb"}},
}


def _span(sid, pid, op, start, parent=None, kind=None):
    s = {"spanID": sid, "processID": pid, "operationName": op, "startTime": start, "duration": 10,
         "references": [{"refType": "CHILD_OF", "spanID": parent}] if parent else [], "tags": []}
    if kind:
        s["tags"].append({"key": "span.kind", "value": kind})
    return s


def _compose_trace(i, t):
    procs = {"p1": {"serviceName": "nginx"}, "p2": {"serviceName": "compose"}, "p3": {"serviceName": "text"},
             "p4": {"serviceName": "url"}, "p5": {"serviceName": "user"}}
    spans = [
        _span("a", "p1", "/compose", t),
        _span("b", "p2", "compose_server", t + 1, "a", "server"),
        _span("c", "p3", "text_server", t + 2, "b", "server"),
        _span("d", "p4", "url_server", t + 3, "c", "server"),
        _span("e", "p4", "url_mongo_insert_client", t + 4, "d", "client"),
        _span("f", "p5", "user_server", t + 5, "b", "server"),
    ]
    return {"traceID": f"c{i}", "spans": spans, "processes": procs}


def _read_trace(i, t):
    procs = {"p1": {"serviceName": "nginx"}, "p2": {"serviceName": "timeline"}, "p3": {"serviceName": "storage"}}
    spans = [
        _span("a", "p1", "/read", t),
        _span("b", "p2", "timeline_server", t + 1, "a", "server"),
        _span("c", "p3", "storage_server", t + 2, "b", "server"),
        _span("d", "p3", "storage_memcached_get_client", t + 3, "c", "client"),
    ]
    return {"traceID": f"r{i}", "spans": spans, "processes": procs}


@pytest.fixture()
def jaeger_file(tmp_path):
    data = []
    for i in range(40):
        t = 1_000_000 * (i // 4)
        data.append(_compose_trace(i, t) if i % 4 == 0 else _read_trace(i, t))
    p = tmp_path / "traces.json.gz"
    with gzip.open(p, "wt") as f:
        json.dump({"data": data}, f)
    return p


def test_store_kind_patterns():
    assert store_kind("compose_user_mentions_memcached_get_client") == "memcached"
    assert store_kind("social_graph_mongo_find_client") == "mongodb"
    assert store_kind("home_timeline_redis_update_client") == "redis"
    assert store_kind("compose_post_server") is None


def test_graph_edges_and_store_mapping(jaeger_file):
    ts = load(jaeger_file, DEPLOYMENT["aliases"])
    g, cover = build(ts, DEPLOYMENT)
    assert ts.n_traces == 40
    assert set(g.successors("gw")) == {"compose", "timeline"}
    assert g.has_edge("url", "url-mongodb")  # traced client span mapped to the instance
    assert g.has_edge("storage", "storage-mc")
    # storage-mongodb is in the map but never traced: inferred edge with storage's coverage
    assert g["storage"]["storage-mongodb"]["inferred"] is True
    assert cover["storage-mongodb"] == cover["storage"]
    assert g.nodes["gw"]["kind"] == "gateway"
    assert g.nodes["url-mongodb"]["kind"] == "mongodb"


def test_no_inferred_stores(jaeger_file):
    ts = load(jaeger_file, DEPLOYMENT["aliases"])
    g, _ = build(ts, DEPLOYMENT, infer_untraced_stores=False)
    assert "storage-mongodb" not in g


def test_blast_share_is_request_coverage(jaeger_file):
    ts = load(jaeger_file, DEPLOYMENT["aliases"])
    g, cover = build(ts, DEPLOYMENT)
    rows = {r["component"]: r for r in rank(ts, g, cover)}
    assert rows["gw"]["blast_share"] == pytest.approx(1.0)
    assert rows["timeline"]["blast_share"] == pytest.approx(0.75)
    assert rows["compose"]["blast_share"] == pytest.approx(0.25)
    assert rows["url-mongodb"]["blast_share"] == pytest.approx(0.25)
    assert rows["compose"]["upstream_dependents"] == 1
    assert rows["compose"]["downstream_reach"] == 4  # text, url, url-mongodb, user


def test_cei_formula_and_weights(jaeger_file):
    ts = load(jaeger_file, DEPLOYMENT["aliases"])
    g, cover = build(ts, DEPLOYMENT)
    gov = {"compose": 1.0}
    rows = {r["component"]: r for r in rank(ts, g, cover, governance=gov)}
    r = rows["compose"]
    assert r["cei"] == pytest.approx(0.40 * r["centrality"] + 0.35 * r["entropy"] + 0.25 * 1.0)
    rows2 = {r["component"]: r for r in rank(ts, g, cover, weights={"alpha": 1, "beta": 0, "gamma": 0})}
    assert rows2["compose"]["cei"] == pytest.approx(rows2["compose"]["centrality"])
    assert max(x["centrality"] for x in rows.values()) == pytest.approx(1.0)


def test_entropy_bounds():
    assert demand_entropy([]) == 0.0
    assert demand_entropy([1.0] * 50) == 0.0
    spread = [i / 9 for i in range(10)]
    assert demand_entropy(spread) == pytest.approx(1.0)
    assert 0.0 < demand_entropy([0.2, 0.9, 0.9, 0.9]) < 1.0


def test_classes():
    assert classify(0.8) == "critical"
    assert classify(0.5) == "elevated"
    assert classify(0.3) == "moderate"
    assert classify(0.1) == "low"


def test_composite_centrality_matches_definition():
    g = nx.DiGraph([("a", "b"), ("b", "c"), ("b", "d")])
    comp = composite_centrality(g)
    pr, bw = nx.pagerank(g), nx.betweenness_centrality(g)
    dg, cl = nx.degree_centrality(g), nx.closeness_centrality(g)
    raw = {n: 0.35 * pr[n] + 0.30 * bw[n] + 0.20 * dg[n] + 0.15 * cl[n] for n in g}
    top = max(raw.values())
    for n in g:
        assert comp[n] == pytest.approx(raw[n] / top)


def test_what_if_by_root(jaeger_file):
    ts = load(jaeger_file, DEPLOYMENT["aliases"])
    _, cover = build(ts, DEPLOYMENT)
    res = what_if(ts, cover, ["url"])
    assert res["share"] == pytest.approx(0.25)
    assert res["by_root"]["gw /compose"] == pytest.approx(1.0)
    assert res["by_root"]["gw /read"] == pytest.approx(0.0)


def test_collector_csv(tmp_path):
    rows = [
        {"traceId": "t1", "startTime": "100", "childOperation": "compose_server", "parentMS": "nginx", "childMS": "compose"},
        {"traceId": "t1", "startTime": "101", "childOperation": "text_server", "parentMS": "compose", "childMS": "text"},
        {"traceId": "t1", "startTime": "102", "childOperation": "user_mongo_find_client", "parentMS": "user", "childMS": "user"},
        {"traceId": "t2", "startTime": "200", "childOperation": "read_server", "parentMS": "nginx", "childMS": "timeline"},
    ]
    p = tmp_path / "trace.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    ts = load(p)
    g, cover = build(ts)
    assert g.has_edge("nginx", "compose") and g.has_edge("compose", "text")
    assert g.has_edge("user", "user::mongodb")
    assert ts.trace_roots["t1"][0] == "nginx"
    assert len(cover["nginx"]) == 2


def test_cli_rank_csv(jaeger_file, tmp_path, capsys):
    mp = tmp_path / "map.json"
    mp.write_text(json.dumps(DEPLOYMENT))
    out = tmp_path / "rank.csv"
    cli_main(["rank", str(jaeger_file), "--deployment", str(mp), "--format", "csv", "--out", str(out)])
    rows = list(csv.DictReader(open(out)))
    assert {r["component"] for r in rows} >= {"gw", "compose", "url-mongodb"}
    assert all(not math.isnan(float(r["cei"])) for r in rows)
    cli_main(["rank", str(jaeger_file), "--deployment", str(mp), "--sort", "blast_share", "--top", "1"])
    assert "gw" in capsys.readouterr().out


def test_cli_governance_range(jaeger_file, tmp_path):
    gov = tmp_path / "gov.json"
    gov.write_text(json.dumps({"compose": 2.0}))
    with pytest.raises(SystemExit):
        cli_main(["rank", str(jaeger_file), "--governance", str(gov)])


def test_flow_entropy():
    from dsb_criticality.score import flow_entropy

    g = nx.DiGraph()
    g.add_edge("a", "b", calls=10, inferred=False)
    g.add_edge("b", "c", calls=10, inferred=False)
    g.add_edge("b", "d", calls=0, inferred=True)
    h = flow_entropy(g, cover={"b": {"t1", "t2"}})
    assert h["a"] == 0.0  # one incident edge
    # b: weights 10, 10 and 2 (the inferred edge gets b's coverage) over 3 edges
    p = [10 / 22, 10 / 22, 2 / 22]
    assert h["b"] == pytest.approx(-sum(x * math.log(x) for x in p) / math.log(3))
    g2 = nx.DiGraph([("x", "y"), ("x", "z")])
    nx.set_edge_attributes(g2, 5, "calls")
    assert flow_entropy(g2)["x"] == pytest.approx(1.0)


def test_rank_flow_entropy_option(jaeger_file):
    ts = load(jaeger_file, DEPLOYMENT["aliases"])
    g, cover = build(ts, DEPLOYMENT)
    rows = {r["component"]: r for r in rank(ts, g, cover, entropy="flow")}
    r = rows["compose"]
    assert r["cei"] == pytest.approx(0.40 * r["centrality"] + 0.35 * r["entropy"])
    with pytest.raises(ValueError):
        rank(ts, g, cover, entropy="other")


def test_bundled_maps_load():
    from dsb_criticality import load_map

    for app in ("socialNetwork", "hotelReservation", "mediaMicroservices"):
        m = load_map(app)
        assert m["gateway"] and m["stores"]
