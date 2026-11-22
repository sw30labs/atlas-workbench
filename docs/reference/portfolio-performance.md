# Portfolio pagination and measured limits

`GET /api/scenarios` provides bounded, read-only scenario **summaries**. It is
available to every authorized reader in shared mode. The existing `/api/data`
aggregate is retained for dashboard compatibility.

```text
/api/scenarios?limit=50&offset=0&sort=urgency&direction=desc&status=active
/api/scenarios?limit=50&origin=synthetic_fixture&q=dependency
```

| Parameter | Behavior |
| --- | --- |
| `limit` | 1–200 records; default 50 |
| `offset` | 0–1,000,000; default 0 |
| `sort` | `urgency` (default), `title`, `year`, `id` |
| `direction` | `asc` or `desc`; urgency/year default descending, others ascending |
| `status` | `all` (default), `active`, `draft`, `retired`; absent legacy status counts as active |
| `origin` | `all` (default) or one of the explicit scenario provenance values |
| `category` | Exact category comparison, case-insensitive for ASCII, maximum 160 characters |
| `q` | Literal title/description substring, maximum 200 characters; `%` and `_` remain literal characters |

Unknown/repeated parameters, invalid encoding and malformed bounds return a
400 error. Search values are bound SQL parameters; sort expressions come from
a closed allowlist. SQLite's built-in case-insensitive comparisons do not
provide complete Unicode case folding. The API does not create or migrate a
store. A missing, corrupt, locked or incompatible store returns an explicit
503 error instead of an empty successful portfolio.

The result has `scenarios`, `page`, `sort`, `filters` and a consistency notice.
Each scenario uses dashboard-compatible keys such as `id`, `title`, `category`,
`origin`, `status`, `urgency_score`, `year`, `impact_rank`, `fin_impact`,
`controllability`, `time_horizon` and `last_scored`. A separately named
`description_excerpt` contains up to 400 characters; `description_truncated`
states whether it is incomplete. The response does not claim to be a full
record or include framework mappings, all narrative fields or decision history.
Use a governed export for complete, reproducible records.

`page` reports `offset`, `limit`, `returned`, `total`, `has_more` and
`next_offset`. Follow `next_offset` until it is null. If the offset ceiling is
reached while records remain, `continuation_limited` is true and no unusable
continuation is returned; narrow the filters or use a complete export.

Count and rows come from the same SQLite read transaction. Missing urgency
scores sort last in either direction. Scenario ID, then SQLite row ID, breaks
ties deterministically even for old stores with duplicate or missing IDs.
Offset pages are not a persistent multi-request snapshot: inserts, deletions
or score changes can shift subsequent pages. A frozen export is the supported
way to retain a stable set across concurrent edits.

Queries have a two-second SQLite execution budget and a short lock wait. Large
sorts and substring searches can still scan the catalog; this release adds no
index or full-text-search migration. Deep offsets cost more than the first
page. A timeout returns a 503 with guidance to narrow the filters.

## Reproducible synthetic measurements

Run the benchmark from the repository's installed development environment:

```bash
python scripts/benchmark_portfolio.py --sizes 1000 10000 50000 --repeats 3
```

The script creates a fresh temporary database, fills only fictional scenario
content and removes it afterward. It never opens configured runtime stores.
The comparison measures SQLite reads plus JSON encoding. The legacy side is
only `SELECT * FROM scenarios` and serialization, a **lower bound** for the
larger `/api/data` response. The paginated side includes filtering, count,
sorting and summary serialization. Fixture rows contain several repeated
synthetic narrative fields, rather than only short identifiers.

Observed on macOS with Python 3.12.13, three repetitions per case:

| Synthetic scenarios | Full scenario aggregate, median | First 50 summaries, median | Last 50 summaries, median | Filtered first 50, median |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 64.20 ms | 3.04 ms | 4.23 ms | 3.81 ms |
| 10,000 | 627.70 ms | 9.38 ms | 37.24 ms | 19.23 ms |
| 50,000 | 3,038.56 ms | 35.91 ms | 245.02 ms | 84.88 ms |

| Synthetic scenarios | Full aggregate JSON | Page-of-50 JSON | Full peak Python allocations | Page peak Python allocations |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 3.92 MiB | about 36 KiB | 12.49 MiB | at most 0.14 MiB |
| 10,000 | 39.24 MiB | about 36 KiB | 124.87 MiB | at most 0.14 MiB |
| 50,000 | 196.22 MiB | about 36 KiB | 624.35 MiB | at most 0.14 MiB |

Times include `tracemalloc` instrumentation overhead. Peak allocations cover
Python allocations, **not** total process memory, native SQLite sort memory,
operating-system caches or browser memory. Payload sizes depend on narrative
length. These are observed synthetic results, not a service-level objective
or a concurrent-user load test.

## Dashboard boundary

The scenario card grid renders 24 records per page. Its regression test
executes the real JavaScript renderer over 50 synthetic records and checks
24/24/2 cards, page navigation, correct selected-record identity, filter resets,
clamping after data removal and preservation of all matching IDs for exports.

That card pagination operates over the already loaded client array. The
dashboard still fetches the full `/api/data` aggregate; charts, filtering and
the accessible portfolio table can still process the full collection. The
new API has not replaced those initial data loads. Consequently, this change
does **not** establish end-to-end dashboard scalability at 50,000 scenarios.
Using the summary API for initial portfolio loading, fetching selected record
details separately, paginating the accessible table and measuring real browser
memory/interaction latency are the next steps before making that claim.
