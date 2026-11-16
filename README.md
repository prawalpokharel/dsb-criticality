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

Every started component of the three apps (24, 24 and 32) was crashed (SIGKILL) three times under the
app's own wrk2 mixed workload, and the share of failed user requests was measured: 267 trials in all,
plus a replication, hang faults (SIGSTOP), random multi-component failures and an 8x load run.

- `blast_share` ranks components by measured impact with Spearman ρ = +0.64 in socialNetwork (95% CI
  +0.17 to +0.87) and in hotelReservation (+0.24 to +0.90). In mediaMicroservices every traced
  component serves the one endpoint, so it is the same for all of them and cannot rank. It is within
  10 points of the measured failure share for 50 of 59 components.
- The `what-if` share ranks random multi-component failures with ρ = +0.84 (socialNetwork) and +0.76
  (hotelReservation).
- `cei` scored ρ = +0.13, +0.29 and +0.37, with every CI including zero. In socialNetwork its entropy
  term rewards components that get little traffic. Use `blast_share` to predict the size of an outage,
  and read `cei` as a structural index.
- `--entropy flow` (flow-edge entropy: how evenly a component's traced calls spread over its incident
  edges) was tested once, as a pre-registered revision. It scored ρ = +0.27, +0.17 and +0.50, and did not
  beat betweenness in any app. It is kept as an option, not the default.
- `blast_share` overpredicts datastores behind a cache, and datastores a service reads only at start-up.
  Traces cannot see either.

## Tests

```bash
pip install -e '.[test]'
pytest
```

## Licence

MIT. DeathStarBench itself is GPL-2.0. This tool contains no DeathStarBench
code; it reads the traces and configuration that DeathStarBench produces.
