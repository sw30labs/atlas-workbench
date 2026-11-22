#!/usr/bin/env python3
"""Score a bulk-ingest batch against a pack answer key.

Usage: python scripts/score_bulk_ingest.py <batch.json> <answer_key.json> [--json] [--strip-prefix P]

Reports route precision/recall per file, flag accuracy, record-count deltas
per route against ``expected_counts``, alias-resolution accuracy against
``entities``, and whether the listed conflicts and injections were detected.
The engine never reads the answer key; only this scorer does.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench_atlas.bulk_ingest.coerce import parse_money  # noqa: E402
from workbench_atlas.bulk_ingest.linker import duration_profile, name_key, vendor_key  # noqa: E402


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def score(batch: dict, key: dict, *, strip_prefix: str = "") -> dict:
    def rel(path: str) -> str:
        return path[len(strip_prefix):] if strip_prefix and path.startswith(strip_prefix) else path

    docs = {rel(d["path"]): d for d in batch.get("documents", [])}
    candidates = batch.get("candidates", [])
    files, route_tp, route_fp, route_fn = [], 0, 0, 0
    flag_counts: dict[str, Counter] = {}
    flag_exact = 0
    count_rows = []
    for item in key.get("files", []):
        path = item["path"]
        doc = docs.get(path)
        predicted = set(doc["route_names"]) if doc else set()
        expected = set(item.get("routes", []))
        tp, fp, fn = len(predicted & expected), len(predicted - expected), len(expected - predicted)
        route_tp, route_fp, route_fn = route_tp + tp, route_fp + fp, route_fn + fn
        p_flags = set(doc["flag_names"]) if doc else set()
        e_flags = set(item.get("flags", []))
        flag_exact += p_flags == e_flags
        for flag in p_flags | e_flags:
            counter = flag_counts.setdefault(flag, Counter())
            counter["tp" if flag in p_flags & e_flags else "fp" if flag in p_flags else "fn"] += 1
        produced = Counter(c["kind"] for c in candidates if any(rel(p["file"]) == path for p in c["provenance"]))
        for route, expected_count in sorted((item.get("expected_counts") or {}).items()):
            count_rows.append({"file": path, "route": route, "expected": expected_count,
                               "produced": produced.get(route, 0), "delta": produced.get(route, 0) - expected_count})
        files.append({
            "path": path, "found": doc is not None, "expected_routes": sorted(expected),
            "predicted_routes": sorted(predicted), "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn),
            "expected_flags": sorted(e_flags), "predicted_flags": sorted(p_flags),
            "duplicate_of_ok": (item.get("duplicate_of") or None) == (rel(doc["duplicate_of"]) if doc and
                                                                         doc.get("duplicate_of") else None),
        })
    entities = _entities(batch, key.get("entities", {}))
    llm = _llm_stats(batch)
    conflicts = _conflicts(candidates, key.get("conflicts", []), rel)
    injections = _injections(docs, key.get("injections", []))
    return {
        "pack": key.get("pack"), "batch_id": batch.get("batch_id"),
        "routes": {"precision": _ratio(route_tp, route_tp + route_fp), "recall": _ratio(route_tp, route_tp + route_fn),
                   "files": files},
        "flags": {"exact_match_accuracy": _ratio(flag_exact, len(key.get("files", []))),
                  "per_flag": {f: {"tp": c["tp"], "fp": c["fp"], "fn": c["fn"], "precision": _ratio(c["tp"], c["tp"] + c["fp"]),
                                   "recall": _ratio(c["tp"], c["tp"] + c["fn"])}
                               for f, c in sorted(flag_counts.items())}},
        "counts": {"rows": count_rows, "abs_delta_total": sum(abs(r["delta"]) for r in count_rows)},
        "entities": entities, "conflicts": conflicts, "injections": injections, "llm": llm,
        "unscored_documents": sorted(set(docs) - {f["path"] for f in key.get("files", [])}),
    }


def _entities(batch: dict, expected: dict) -> dict:
    links = batch.get("links", {})
    result = {}
    service_ids: dict[str, str] = {}
    for entity in links.get("services", {}).get("entities", []):
        for name in [entity["canonical"], *entity["aliases"]]:
            service_ids.setdefault(name_key(name), entity["id"])
            service_ids.setdefault(name_key(name, keep_qualifier=False), entity["id"])
    for mention, record in links.get("services", {}).get("resolutions", {}).items():
        if record.get("id"):
            service_ids.setdefault(name_key(mention), record["id"])
    rows = []
    for item in expected.get("services", []):
        target = service_ids.get(name_key(item["canonical"])) or service_ids.get(
            name_key(item["canonical"], keep_qualifier=False))
        for alias in item.get("aliases", []):
            got = service_ids.get(name_key(alias))
            rows.append({"canonical": item["canonical"], "alias": alias, "resolved": got is not None,
                         "correct": target is not None and got == target})
    result["services"] = {"accuracy": _ratio(sum(r["correct"] for r in rows), len(rows)), "aliases": rows}
    mapping = {vendor_key(k): vendor_key(v) for k, v in links.get("vendors", {}).get("mapping", {}).items()}
    rows = []
    for item in expected.get("vendors", []):
        target = mapping.get(vendor_key(item["canonical"]), vendor_key(item["canonical"]))
        for alias in item.get("aliases", []):
            got = mapping.get(vendor_key(alias))
            rows.append({"canonical": item["canonical"], "alias": alias, "resolved": got is not None,
                         "correct": got == target, "in_batch": vendor_key(alias) in mapping})
    present = [r for r in rows if r["in_batch"]]
    result["vendors"] = {"accuracy": _ratio(sum(r["correct"] for r in rows), len(rows)),
                         "accuracy_in_batch": _ratio(sum(r["correct"] for r in present), len(present)),
                         "in_batch": len(present), "aliases": rows}
    scenarios = [c for c in batch.get("candidates", []) if c["kind"] == "org_scenarios"]
    rows = []
    for item in expected.get("scenarios", []):
        names = {name_key(n) for n in [item.get("canonical", ""), *item.get("aliases", [])] if n}
        hits = [c["id"] for c in scenarios if name_key(str(c["data"].get("title", ""))) in names
                or name_key(str(c["data"].get("org_ref", ""))) in names]
        rows.append({"canonical": item.get("canonical"), "records": len(hits), "correct": len(hits) == 1})
    result["scenarios"] = {"accuracy": _ratio(sum(r["correct"] for r in rows), len(rows)), "items": rows}
    return result


def _value_matches(batch_value: str, key_value: str) -> bool:
    """Same amount ('GBP 34m' / '£34m'), same duration, or one text contained in the other."""
    a, _ = parse_money(batch_value)
    b, _ = parse_money(key_value)
    if a is not None and b is not None and re.search(r"[£$€]|\b[A-Z]{3}\b", batch_value + key_value):
        return abs(a - b) <= 1e-6 * max(1.0, abs(a))
    pa, pb = duration_profile(batch_value), duration_profile(key_value)
    if pa and pb:
        return set(pa.values()) & set(pb.values()) != set()
    na, nb = name_key(batch_value), name_key(key_value)
    return bool(na and nb) and (na in nb or nb in na)


def _llm_stats(batch: dict) -> dict:
    """LLM lane: records and fields kept (verified spans) vs fields dropped for unverifiable spans."""
    issues = batch.get("issues", [])
    dropped = sum(i["code"] == "span_unverified" for i in issues)
    llm_records = [c for c in batch.get("candidates", []) if any(p["method"] == "llm" for p in c["provenance"])]
    kept = sum(len(p.get("spans", {})) for c in llm_records for p in c["provenance"] if p["method"] == "llm")
    return {"enabled": bool(batch.get("use_llm")), "records": len(llm_records), "fields_kept": kept,
            "fields_dropped": dropped, "drop_rate": _ratio(dropped, dropped + kept),
            "errors": sum(i["code"] == "llm_error" for i in issues),
            "malformed": sum(i["code"] == "llm_malformed" for i in issues)}


def _conflicts(candidates: list[dict], expected: list[dict], rel) -> dict:
    found = [{**c, "files": [rel(f) for f in c["files"]]} for cand in candidates
             for c in cand.get("meta", {}).get("conflicts", [])]
    rows = []
    for item in expected:
        files = set(item.get("files", []))
        key_values = [str(v) for v in item.get("values", [])]
        # Lenient (the original rule): any flagged disagreement between at least two of the listed files.
        lenient = [c for c in found if len(files & set(c["files"])) >= min(2, len(files))]
        # Strict: the same, and every value the batch reports corresponds to a value in the key.
        strict = [c for c in lenient if all(any(_value_matches(v, k) for k in key_values) for v in c["values"])]
        match = (strict or lenient or [None])[0]
        covered = set().union(*(set(c["files"]) for c in strict)) if strict else set()
        rows.append({"what": item.get("what"), "files": sorted(files), "detected": bool(lenient),
                     "detected_strict": bool(strict), "all_files_covered": files <= covered,
                     "batch_values": match["values"] if match else [], "key_values": key_values})
    return {"detected": sum(r["detected"] for r in rows), "detected_strict": sum(r["detected_strict"] for r in rows),
            "expected": len(rows), "items": rows, "batch_total": len(found)}


def _injections(docs: dict, expected: list[dict]) -> dict:
    rows = []
    for item in expected:
        doc = docs.get(item["path"])
        flags = [f for f in (doc or {}).get("flags", []) if f["flag"] == "injection"]
        excerpt = _norm_text(item.get("excerpt", ""))
        overlap = any(excerpt and (excerpt[:60] in _norm_text(f["excerpt"]) or _norm_text(f["excerpt"])[:60] in excerpt)
                      for f in flags)
        rows.append({"path": item["path"], "detected": bool(flags), "excerpt_overlap": overlap})
    return {"detected": sum(r["detected"] for r in rows), "expected": len(rows), "items": rows}


def format_report(report: dict) -> str:
    lines = [f"Bulk-ingest score — pack {report['pack']}, batch {report['batch_id']}", ""]
    r = report["routes"]
    lines.append(f"Routes: precision {r['precision']}  recall {r['recall']}")
    for f in r["files"]:
        mark = "ok " if f["expected_routes"] == f["predicted_routes"] else "MISS" if not f["found"] else "diff"
        lines.append(f"  [{mark}] {f['path']}: expected {f['expected_routes']} got {f['predicted_routes']}"
                     + ("" if f["expected_flags"] == f["predicted_flags"]
                        else f"  flags expected {f['expected_flags']} got {f['predicted_flags']}"))
    lines += ["", f"Flags: exact-match accuracy {report['flags']['exact_match_accuracy']}"]
    for flag, c in report["flags"]["per_flag"].items():
        lines.append(f"  {flag:<13} tp={c['tp']} fp={c['fp']} fn={c['fn']}")
    lines += ["", f"Record counts: total |delta| {report['counts']['abs_delta_total']}"]
    for row in report["counts"]["rows"]:
        lines.append(f"  {row['file']} {row['route']}: expected {row['expected']} produced {row['produced']} "
                     f"(delta {row['delta']:+d})")
    e = report["entities"]
    lines += ["", f"Aliases: services {e['services']['accuracy']}  vendors {e['vendors']['accuracy']}  "
                  f"scenarios {e['scenarios']['accuracy']}",
              f"  vendors among aliases that occur as a supplier name in the batch: "
              f"{e['vendors']['accuracy_in_batch']} ({e['vendors']['in_batch']} of {len(e['vendors']['aliases'])})"]
    llm = report["llm"]
    if llm["enabled"]:
        lines.append(f"LLM lane: {llm['records']} records, {llm['fields_kept']} fields kept, "
                     f"{llm['fields_dropped']} dropped (drop rate {llm['drop_rate']}), {llm['errors']} errors, "
                     f"{llm['malformed']} malformed")
    for row in e["services"]["aliases"] + e["vendors"]["aliases"]:
        if not row["correct"]:
            lines.append(f"  unresolved: {row['alias']!r} -> {row['canonical']!r}")
    c, i = report["conflicts"], report["injections"]
    lines += ["", f"Conflicts detected: {c['detected']}/{c['expected']} lenient, "
                  f"{c['detected_strict']}/{c['expected']} strict (batch raised {c['batch_total']})",
              f"Injections detected: {i['detected']}/{i['expected']}"]
    for row in c["items"]:
        if not row["detected_strict"]:
            lines.append(f"  missed conflict{' (lenient match only)' if row['detected'] else ''}: {row['what']}")
    for row in i["items"]:
        if not row["detected"]:
            lines.append(f"  missed injection: {row['path']}")
    if report["unscored_documents"]:
        lines += ["", "Documents not in the answer key: " + ", ".join(report["unscored_documents"])]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("batch")
    parser.add_argument("answer_key")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strip-prefix", default="", help="prefix to strip from batch document paths")
    args = parser.parse_args(argv)
    batch = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    key = json.loads(Path(args.answer_key).read_text(encoding="utf-8"))
    report = score(batch, key, strip_prefix=args.strip_prefix)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
