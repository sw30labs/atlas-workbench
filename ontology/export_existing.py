#!/usr/bin/env python3
"""
export_existing.py — snapshot the Atlas stores into ontology/existing.ttl.

Ledger shapes see cross-record truth, so they need the current store state.
Register risks and evidence events pass through the guard's own door models (they
are door-tolerant by design); scenarios, compounds, runs and proposals are
projected RAW — they carry historical drift (rogue enums, dangling slugs) that
must reach the ledger as-is to surface as graph-side findings.

Risk ids come from the stored identifier, with the application minting helper
as a fallback. The write seams use the same precedence.

Usage: .venv/bin/python ontology/export_existing.py   (from the repo root)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

sys.path.insert(0, str(Path(__file__).parent))
from guard import DATA, NS, EvidenceEventRec, RiskRec, _normalized_category, project  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contingency_atlas.categories import RISK_CATEGORIES  # noqa: E402
from contingency_atlas.guard_hook import mint_risk_id  # noqa: E402

CAT = Namespace("https://contingency-atlas.local/ontology/cat/")
SKOS_NS = Namespace("http://www.w3.org/2004/02/skos/core#")
_LEGACY_EVIDENCE_TABLE = "ne" + "ws"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _rows_if_present(
    connection: sqlite3.Connection,
    table: str,
):
    """Return all rows from an optional snapshot table, or an empty tuple."""
    if not _table_exists(connection, table):
        return ()
    # Every caller passes a source-controlled table constant, never input.
    return connection.execute(f'SELECT * FROM "{table}"')


def _row_value(row: sqlite3.Row, column: str, default=None):
    """Read an optional historical column without inventing stored data."""
    return row[column] if column in row.keys() else default


def _canonical_evidence_rows(connection: sqlite3.Connection):
    """Return canonical rows, failing clearly when an upgrade is required.

    Snapshot generation is used by the guard and must stay read-only. Schema
    upgrades therefore happen explicitly in ``scripts/migrate_runtime_stores.py``
    before this exporter runs, never as a side effect of validation.
    """
    canonical_table = "evidence_events"
    if _table_exists(connection, _LEGACY_EVIDENCE_TABLE):
        raise RuntimeError(
            "event evidence store requires a schema upgrade; run "
            "`python scripts/migrate_runtime_stores.py` before refreshing "
            "the ontology snapshot"
        )
    if not _table_exists(connection, canonical_table):
        return ()

    required = {
        "event_id",
        "headline",
        "risk_category",
        "source_url",
        "source_date",
        "retrieved_at",
        "provenance_class",
        "review_status",
    }
    columns = {
        row[1]
        for row in connection.execute(
            f'PRAGMA table_info("{canonical_table}")'
        )
    }
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(
            "event evidence store has an incomplete canonical schema "
            f"(missing: {', '.join(missing)}); run "
            "`python scripts/migrate_runtime_stores.py` before refreshing "
            "the ontology snapshot"
        )
    return connection.execute(f'SELECT * FROM "{canonical_table}"')

def main(*, root: Path | None = None, out: Path | None = None) -> None:
    root = (root or Path(__file__).resolve().parent.parent).resolve()
    out = (out or Path(__file__).parent / "existing.ttl").resolve()
    g = Graph()

    def add(s, p, o):
        g.add((s, p, o))

    # -- risk register (through the door; stored id wins, else minted) ------
    register_path = root / "emerging_risks.json"
    register = (
        json.loads(register_path.read_text()) if register_path.exists() else []
    )
    n = 0
    for rec in register:
        # Same precedence as guard_hook.risk_record: a persisted risk_id is the
        # record's identity, so a later title edit cannot move the node.
        model = RiskRec(
            **{
                "kind": "risk",
                **rec,
                "id": rec.get("risk_id") or mint_risk_id(rec["issue"]),
            }
        )
        project(model, g)
        n += 1
    print(f"risks: {n}")

    # -- evidence events (through the door) ----------------------------------
    n = 0
    if (root / "event_evidence.db").exists():
        con = sqlite3.connect(f"file:{root / 'event_evidence.db'}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        for row in _canonical_evidence_rows(con):
            model = EvidenceEventRec(**{
                "kind": "evidence_event",
                "id": row["event_id"],
                "headline": row["headline"] or "(no headline)",
                "risk_category": row["risk_category"],
                "source_url": row["source_url"],
                "source_date": row["source_date"],
                "retrieved_at": row["retrieved_at"],
                "provenance_class": row["provenance_class"],
                "review_status": row["review_status"] or "pending",
            })
            project(model, g)
            n += 1
        con.close()
    print(f"evidence events: {n}")

    # -- scenarios (RAW — historical drift must reach the ledger) ------------
    atlas_path = root / "atlas.db"
    if atlas_path.exists():
        db = sqlite3.connect(f"file:{atlas_path}?mode=ro", uri=True)
    else:  # fresh checkout, no stores yet: empty tables -> empty sections
        db = sqlite3.connect(":memory:")
        for table in ("scenarios", "compound_scenarios",
                      "compound_join_runs", "channel_proposals"):
            db.execute(f"CREATE TABLE {table} (_placeholder)")
    db.row_factory = sqlite3.Row
    n = 0
    for row in _rows_if_present(db, "scenarios"):
        iri = DATA[row["scenario_id"]]
        add(iri, RDF.type, NS.Scenario)
        add(
            iri,
            NS.origin,
            Literal(
                str(_row_value(row, "origin"))
                if _row_value(row, "origin")
                else "legacy_unknown"
            ),
        )
        for col, prop in (("status", NS.scenarioStatus),
                          ("risk_category", NS.riskCategory),
                          ("likelihood", NS.likelihood),
                          ("impact_band", NS.impactBand),
                          ("financial_impact_band", NS.financialImpactBand),
                          ("controllability", NS.controllability),
                          ("signpost_status", NS.signpostStatus),
                          ("event_driver_class", NS.eventDriverClass)):
            val = _row_value(row, col)
            if val is not None and str(val).strip():
                add(iri, prop, Literal(str(val)))
        n += 1
    print(f"scenarios: {n}")

    # -- compound scenarios (RAW) ---------------------------------------------
    n = 0
    for row in _rows_if_present(db, "compound_scenarios"):
        iri = DATA[row["compound_id"]]
        add(iri, RDF.type, NS.CompoundScenario)
        for slug in json.loads(row["risk_ids"]):
            add(iri, NS.riskRef, DATA[f"slug/{slug}"])
        for val, prop in (
            (_row_value(row, "run_id"), NS.runId),
            (_row_value(row, "status"), NS.compoundStatus),
            (_row_value(row, "uncertainty_class"), NS.uncertaintyClass),
            (_row_value(row, "pair_id"), NS.pairId),
        ):
            if val is not None and str(val).strip():
                add(iri, prop, Literal(str(val)))
        loss_units = _row_value(row, "loss_units")
        if loss_units is not None and str(loss_units).strip():
            add(iri, NS.lossUnits, Literal(str(loss_units)))
        n += 1
    print(f"compound scenarios: {n}")

    # -- join runs (RAW) -------------------------------------------------------
    n = 0
    for row in _rows_if_present(db, "compound_join_runs"):
        iri = DATA[row["run_id"]]
        add(iri, RDF.type, NS.CompoundJoinRun)
        add(iri, NS.runId, Literal(row["run_id"]))
        n += 1
    print(f"join runs: {n}")

    # -- channel proposals (RAW) ----------------------------------------------
    n = 0
    for row in _rows_if_present(db, "channel_proposals"):
        iri = DATA[row["proposal_id"]]
        add(iri, RDF.type, NS.ChannelProposal)
        for val, prop in (
            (_row_value(row, "status"), NS.proposalStatus),
            (_row_value(row, "kind"), NS.proposalKind),
            (_row_value(row, "citation_status"), NS.citationStatus),
            (_row_value(row, "channel"), NS.channel),
            (_row_value(row, "reviewer"), NS.reviewer),
            (_row_value(row, "reason"), NS.reason),
        ):
            if val is not None and str(val).strip():
                add(iri, prop, Literal(str(val)))
        graph_version = _row_value(row, "graph_version_applied")
        if graph_version is not None:
            add(iri, NS.graphVersionApplied, Literal(graph_version))
        n += 1
    print(f"channel proposals: {n}")
    db.close()

    # -- risk-category scheme (C-04 reference data, from categories.py) ------
    # altLabels come from ontology/category-map.json (ratified label variants);
    # every label (pref or alt) contributes its normalized form so the C-04
    # ledger join resolves stored free-text values onto the scheme.
    cat_map_path = Path(__file__).parent / "category-map.json"
    cat_map = {"map": {}}
    if cat_map_path.exists():
        cat_map = json.loads(cat_map_path.read_text())
    alt_labels: dict[str, list[str]] = {}
    for raw, target in cat_map.get("map", {}).items():
        alt_labels.setdefault(target, []).append(raw)
    for label in RISK_CATEGORIES:
        concept = CAT[_normalized_category(label)]
        add(concept, RDF.type, SKOS_NS.Concept)
        add(concept, SKOS_NS.prefLabel, Literal(label))
        add(concept, SKOS_NS.inScheme, CAT["risk-categories"])
        add(concept, NS.normalizedLabel, Literal(_normalized_category(label)))
        for alt in sorted(alt_labels.get(label, [])):
            add(concept, SKOS_NS.altLabel, Literal(alt))
            add(concept, NS.normalizedLabel, Literal(_normalized_category(alt)))
    print(f"category concepts: {len(RISK_CATEGORIES)} "
          f"(+{sum(len(v) for v in alt_labels.values())} altLabels)")

    g.bind("atlas", NS)
    g.bind("data", DATA)
    out.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(out, format="turtle")
    print(f"wrote {out} ({len(g)} triples)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="runtime store directory (default: source checkout root)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "existing.ttl",
        help="snapshot output path",
    )
    args = parser.parse_args()
    main(root=args.root, out=args.out)
