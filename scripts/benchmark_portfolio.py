"""Offline synthetic scenario-scale benchmark; never opens configured stores.

Writes only to an automatically cleaned temporary directory. The legacy
comparison is its scenario SELECT * plus JSON encoding, a lower bound for
the larger /api/data response, not a browser rendering benchmark.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from contextlib import closing
from pathlib import Path

from workbench_atlas.config import Settings
from workbench_atlas.schema import migrate
from workbench_atlas.web.portfolio import build_scenario_page


def _measure(call, repeats):
    elapsed, peaks, sizes = [], [], []
    for _ in range(repeats):
        tracemalloc.start()
        started = time.perf_counter()
        payload = call()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        elapsed.append((time.perf_counter() - started) * 1000)
        peaks.append(tracemalloc.get_traced_memory()[1])
        sizes.append(len(encoded))
        tracemalloc.stop()
        del payload, encoded
    return {"median_ms": round(statistics.median(elapsed), 2),
            "peak_python_mib": round(max(peaks) / 1024 ** 2, 2), "json_bytes": max(sizes)}


def benchmark(size, repeats):
    with tempfile.TemporaryDirectory(prefix="atlas-synthetic-scale-") as directory:
        root = Path(directory)
        path = root / "synthetic.db"
        migrate(str(path))
        # Fictional repeated content only, deliberately with no URL or person.
        narrative = ("Fictional dependency disruption used for a synthetic scale fixture. " * 8)
        with closing(sqlite3.connect(path)) as connection:
            connection.executemany(
                "INSERT INTO scenarios (scenario_id,title,description,risk_category,origin,status,urgency_score,created_year,detailed_rationale,transmission_mechanism,challenged_assumptions,weakening_signposts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ((f"SYN-SCALE-{index:07d}", f"Synthetic scenario {index:07d}", narrative,
                  "Synthetic resilience", "synthetic_fixture", "active" if index % 10 else "retired",
                  index % 101 if index % 17 else None, 2020 + index % 7,
                  narrative, narrative, narrative, narrative) for index in range(size)),
            )
            connection.commit()
        configured = Settings(scenario_db_path=path, evidence_db_path=root / "unused-evidence.db",
                              emerging_risks_path=root / "unused-risks.json", context_db_path=root / "unused-context.db")
        def legacy():
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                return {"scenarios": [dict(row) for row in connection.execute("SELECT * FROM scenarios")]}
        return {"synthetic_scenarios": size,
                "legacy_scenario_aggregate": _measure(legacy, repeats),
                "first_page_50": _measure(lambda: build_scenario_page(configured, "limit=50"), repeats),
                "deep_page_50": _measure(lambda: build_scenario_page(configured, f"limit=50&offset={max(0, size - 50)}"), repeats),
                "filtered_page_50": _measure(lambda: build_scenario_page(configured, "limit=50&status=active&q=dependency"), repeats)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000, 50000])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 5 or any(not 1 <= size <= 100000 for size in args.sizes):
        parser.error("Use 1–5 repetitions and synthetic sizes from 1 to 100000")
    print(json.dumps({"fixture": "synthetic_only", "python": platform.python_version(),
                      "platform": platform.system(), "repeats": args.repeats,
                      "measurement": "SQLite read plus JSON encoding with tracemalloc; excludes HTTP, browser, framework joins and other stores",
                      "results": [benchmark(size, args.repeats) for size in args.sizes]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
