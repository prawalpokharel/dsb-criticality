# dsb-criticality

Rank the services and datastores of a [DeathStarBench](https://github.com/delimitrou/DeathStarBench)
deployment by how much of the application depends on them, using the Jaeger
traces the benchmark already produces.

For every component (a microservice, a MongoDB, memcached or Redis instance,
or the gateway) it reports:

| Column | Meaning |
|---|---|
| `cei` | Dependency-criticality score from the Centrality-Entropy Index (see below) |
| `cei_class` | `critical` (≥ 0.75), `elevated` (≥ 0.50), `moderate` (≥ 0.25) or `low` |
| `blast_share` | Share of traced requests that touched the component. If every call is synchronous with no fallback, a crash fails this share of requests |
| `upstream_dependents` | Components with a call path to this one (static blast radius) |
| `downstream_reach` | Components this one depends on, directly or not |
| `betweenness`, `degree`, `fan_out`, `fan_in`, `pagerank` | Plain graph measures, for comparison |

## Install

```bash
pip install -e .
```

Python 3.9 or newer. The only dependencies are NetworkX and NumPy.

## Use

Download traces from the Jaeger UI or its query API
(`/api/traces?service=<gateway>&start=...&end=...`), then:

```bash
dsb-criticality rank traces.json --deployment socialNetwork
dsb-criticality rank traces.json --deployment socialNetwork --format csv --out ranking.csv
dsb-criticality rank traces.json --deployment hotelReservation --sort blast_share --top 10
dsb-criticality what-if traces.json --deployment socialNetwork --fail user-mongodb,social-graph-redis
dsb-criticality graph traces.json --deployment socialNetwork --out edges.csv
```

The CSV that DeathStarBench's own `ms_collecter/trace_collect.py` writes is
also accepted (`dsb-criticality rank trace.csv ...`).

### Deployment maps

DeathStarBench services trace their datastore calls from the client side
only (for example `compose_user_mentions_memcached_get_client`). The bundled
maps in `src/dsb_criticality/maps/` say which datastore instance each service
is configured to use, so those calls land on the right MongoDB, memcached or
Redis node. They were derived from the DeathStarBench source by
`tools/derive_maps.py` (commit `6ecb097` of the upstream repository):

```bash
python tools/derive_maps.py /path/to/DeathStarBench src/dsb_criticality/maps
```

When the map lists a datastore whose calls are not traced (hotelReservation
does not trace its MongoDB calls), the tool adds the edge and assumes every
request through the service may reach it. `--no-inferred-stores` turns this
off.

### Governance term

Operators can declare criticality for any component in a JSON file with
values in [0, 1]:

```bash
dsb-criticality rank traces.json --deployment socialNetwork --governance gov.json
```

```json
{"user-service": 1.0, "media-service": 0.2}
```

## The score

The score is the Centrality-Entropy Index (CEI):

    score_i = alpha * C_i + beta * H_i + gamma * R_i

- `C_i`: composite centrality on the call graph, 0.35 PageRank + 0.30
  betweenness + 0.20 degree + 0.15 closeness, divided by the maximum over
  components.
- `H_i`: Shannon entropy of the component's demand history (requests that
  touch it per time bin, divided by its own peak), over 10 bins in [0, 1],
  divided by log2(10).
- `R_i`: the governance term, 0 unless declared.
- Default weights alpha = 0.40, beta = 0.35, gamma = 0.25 (`--weights` to change).

CEI was introduced in:

- P. Pokharel, "Evaluation of the CEI Framework: Enterprise Cloud Governance
  in Multi-Tier Distributed Environments," Future Technologies Conference
  (FTC), 2026.
- P. Pokharel, "Self-Tuning Governance of the Energy-Resilience Tradeoff in
  GPU Cluster Scheduling," IEEE IEMCON, 2026.

## How well does it predict real failures?

The score was checked against crash-stop fault injection on all three
DeathStarBench applications: each component was killed in turn under the
app's own mixed workload and the share of failed user requests was
measured. The results, including where a simpler measure does as well, are
in the accompanying report.

## Tests

```bash
pip install -e '.[test]'
pytest
```

## Licence

MIT. DeathStarBench itself is GPL-2.0. This tool contains no DeathStarBench
code; it reads the traces and configuration that DeathStarBench produces.
