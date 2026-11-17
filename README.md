# dsb-criticality

Rank the services and datastores of a [DeathStarBench](https://github.com/delimitrou/DeathStarBench)
deployment by how much of the application depends on them, using the Jaeger
traces the benchmark already produces.

For every component (a microservice, a MongoDB, memcached or Redis instance,
or the gateway) it reports:

| Column | Meaning |
|---|---|
| `blast_share` | Share of traced requests that touched the component. If every call is synchronous with no fallback, a crash fails this share of requests. This is the column to use (see the fault tests below) |
| `cei` | Score from the Centrality-Entropy Index (see below) |
| `cei_class` | `critical` (≥ 0.75), `elevated` (≥ 0.50), `moderate` (≥ 0.25) or `low` |
| `downstream_reach` | Components this one depends on, directly or not |
| `upstream_dependents` | Components with a call path to this one. In the fault tests it ranked crash impact backwards |
| `betweenness`, `degree`, `fan_out`, `fan_in`, `pagerank` | Plain graph measures, for comparison |

## Install

```bash
git clone https://github.com/prawalpokharel/dsb-criticality
cd dsb-criticality
pip install -e .
```

Python 3.9 or newer. The tool needs NetworkX and NumPy. `tools/derive_maps.py`
also needs PyYAML.

## Use

A quick check on the trace file DeathStarBench ships (20 compose-post
requests, so every component on that path gets a share of 1.0):

```bash
dsb-criticality rank /path/to/DeathStarBench/ms_collecter/traces-1695947455229.json --deployment socialNetwork
```

For a real ranking, run the app's mixed workload and download its traces
from the Jaeger UI or the query API
(`/api/traces?service=<gateway>&start=...&end=...&limit=...`). The gateway's
traced name is `nginx-web-server` in socialNetwork, `frontend` in
hotelReservation and `nginx` in mediaMicroservices. The repository samples
10 to 20% of socialNetwork requests and 1% of hotelReservation requests
(`JAEGER_SAMPLE_RATIO`). That is fine for the shares, but collect a few
thousand traces. Then:

```bash
dsb-criticality rank traces.json --deployment socialNetwork --top 10
dsb-criticality rank traces.json --deployment socialNetwork --format csv --out ranking.csv
dsb-criticality what-if traces.json --deployment socialNetwork --fail user-mongodb,social-graph-redis
dsb-criticality graph traces.json --deployment socialNetwork --out edges.csv
```

`rank` sorts by `blast_share` unless you pass `--sort`. The CSV that
DeathStarBench's own `ms_collecter/trace_collect.py` writes is also accepted
(`dsb-criticality rank trace.csv ...`).

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

When the map lists a datastore whose calls are not traced, the tool adds the
edge and assumes every request through the service may reach it. In
hotelReservation, geo, recommendation and user never trace their MongoDB
calls, and profile, rate and reservation trace them only on a cache miss.
`--no-inferred-stores` turns this off.

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

I killed every started component of socialNetwork, hotelReservation and
mediaMicroservices (24, 24 and 32 processes) three times while a Python port
of each app's wrk2 mixed-workload script sent 100 requests/s. That is 240
crash trials plus 27 no-fault trials. Spearman ρ between each column and the
measured share of failed requests, with 95% bootstrap CIs:

| Column | socialNetwork | hotelReservation | mediaMicroservices |
|---|---|---|---|
| `blast_share` | +0.64 [+0.17, +0.87] | +0.64 [+0.24, +0.90] | n/a (1.0 for every component) |
| `betweenness` | +0.51 [+0.09, +0.77] | +0.57 [+0.10, +0.87] | +0.51 [+0.25, +0.68] |
| `cei` | +0.13 [−0.43, +0.59] | +0.29 [−0.27, +0.72] | +0.37 [−0.02, +0.68] |
| `upstream_dependents` | −0.43 [−0.75, +0.01] | −0.41 [−0.72, +0.08] | −0.33 [−0.66, +0.19] |

`blast_share` landed within 10 percentage points of the measured share for
19 of 22, 13 of 16 and 18 of 21 components. It overpredicts caches whose
misses fall back to MongoDB (post-storage-memcached, review-storage-memcached)
and datastores read only at start-up (geo and recommendation load theirs into
memory). With 16 to 22 components per app the intervals are wide.

Pooled over the three apps, `cei` did worse than plain betweenness
(Δρ −0.24, 95% CI −0.43 to −0.07), and also worse than degree, fan-out and
downstream reach. In socialNetwork its entropy term favours components that
get little traffic. `--entropy flow` (how evenly a component's calls spread
over its edges) was tried once as a pre-registered fix. It scored +0.27,
+0.17 and +0.50, and did not beat betweenness in any app. It is off by
default.

`what-if` was checked by killing whole datastore tiers and random pairs and
triples together in socialNetwork and hotelReservation. Its predicted share
ranked those sets with ρ = +0.84 and +0.76. In hotelReservation, the largest
single `blast_share` in the set did as well (+0.81).

## Tests

```bash
pip install -e '.[test]'
pytest
```

## Licence

MIT. DeathStarBench itself is GPL-2.0. This tool contains no DeathStarBench
code; it reads the traces and configuration that DeathStarBench produces.
