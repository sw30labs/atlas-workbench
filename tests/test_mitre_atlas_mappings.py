"""I test governance for the optional MITRE ATLAS scenario overlay."""

from __future__ import annotations

import json
import sqlite3

import pytest

from workbench_atlas.db import get_scenario, update_scenario_fields, upsert_scenario
from workbench_atlas.frameworks.mappings import (
    MappingError,
    approved_mappings_for_scenarios,
    create_mapping_proposal,
    get_mapping_proposal,
    is_mapping_current,
    list_mapping_proposals,
    mapping_state,
    migrate_mapping_proposals,
    resolve_mapping_context,
    scenario_content_hash,
)
from workbench_atlas.frameworks.store import activate_release, framework_status
from workbench_atlas.review import (
    ReviewError,
    approve_mitre_atlas_mapping,
    reject_mitre_atlas_mapping,
)
from workbench_atlas.schema import migrate


def _make_catalog(conn: sqlite3.Connection) -> None:
    """I install the normalized contract without retesting its importer."""
    conn.executescript("""
        CREATE TABLE framework_releases (
            framework TEXT NOT NULL,
            content_version TEXT NOT NULL,
            format_version TEXT NOT NULL,
            release_date TEXT,
            source_url TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (framework, content_version)
        );
        CREATE TABLE framework_objects (
            framework TEXT NOT NULL,
            content_version TEXT NOT NULL,
            object_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            maturity TEXT,
            platforms_json TEXT NOT NULL DEFAULT '[]',
            object_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (framework, content_version, object_id)
        );
        CREATE TABLE framework_relationships (
            framework TEXT NOT NULL,
            content_version TEXT NOT NULL,
            relationship_key TEXT NOT NULL,
            source_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (framework, content_version, relationship_key)
        );
    """)
    for version, active in (("2026.01", 0), ("2026.07", 1)):
        release_digit = "1" if version == "2026.01" else "7"
        release_date = "2026-01-31" if version == "2026.01" else "2026-07-31"
        conn.execute(
            """
            INSERT INTO framework_releases VALUES
                ('mitre-atlas', ?, '6.0.0', ?, ?, ?,
                 '2026-08-12T00:00:00Z', ?)
            """,
            (
                version,
                release_date,
                f"https://example.test/ATLAS-{version}.yaml",
                release_digit * 64,
                active,
            ),
        )
        _add_object(
            conn,
            version,
            "AML.T0043",
            "technique",
            (
                "Craft Adversarial Data"
                if version == "2026.07"
                else "Craft Adversarial Data (legacy)"
            ),
            maturity="Demonstrated" if version == "2026.07" else "Feasible",
            platforms=["Generative AI", "Agentic AI"],
        )
        _add_object(
            conn,
            version,
            "AML.TA0002",
            "tactic",
            "AI Attack Staging",
        )
    _add_object(
        conn,
        "2026.07",
        "AML.M0015",
        "mitigation",
        "Adversarial Input Detection",
    )
    _add_object(
        conn,
        "2026.07",
        "AML.CS0011",
        "case-study",
        "Synthetic Red-Team Exercise",
    )
    relationships = (
        ("rel-achieves", "AML.T0043", "achieves", "AML.TA0002", {"position": 2}),
        ("rel-mitigates", "AML.M0015", "mitigates", "AML.T0043", {}),
        (
            "rel-employs-1",
            "AML.CS0011",
            "employs",
            "AML.T0043",
            {"step-id": "S01", "tactic": "AI Attack Staging"},
        ),
        (
            "rel-employs-2",
            "AML.CS0011",
            "employs",
            "AML.T0043",
            {"step-id": "S02", "tactic": "Impact"},
        ),
    )
    for key, source_id, rel_type, target_id, metadata in relationships:
        conn.execute(
            """
            INSERT INTO framework_relationships VALUES
                ('mitre-atlas', '2026.07', ?, ?, ?, ?, ?)
            """,
            (key, source_id, rel_type, target_id, json.dumps(metadata)),
        )


def _add_object(
    conn: sqlite3.Connection,
    version: str,
    object_id: str,
    object_type: str,
    name: str,
    *,
    maturity: str | None = None,
    platforms: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO framework_objects VALUES
            ('mitre-atlas', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version,
            object_id,
            object_type,
            name,
            f"Description of {name}.",
            maturity,
            json.dumps(platforms or []),
            f"hash-{version}-{object_id}",
            json.dumps({"id": object_id, "name": name}),
        ),
    )


@pytest.fixture
def mapping_db(tmp_path):
    db_path = tmp_path / "atlas.db"
    migrate(str(db_path))
    upsert_scenario(
        str(db_path),
        {
            "scenario_id": "SCN-AI-001",
            "title": "Agent follows a poisoned retrieval instruction",
            "description": "A synthetic scenario used only for tests.",
            "core_risks": json.dumps(["Model risk", "Data integrity"]),
            "transmission_mechanism": "A retrieved instruction invokes a tool.",
            "affected_capabilities": json.dumps(["Payments", "Data access"]),
            "urgency_score": 0.42,
            "status": "active",
        },
    )
    with sqlite3.connect(db_path) as conn:
        _make_catalog(conn)
    return db_path


def test_mapping_migration_is_idempotent_and_claims_are_append_only(mapping_db):
    migrate_mapping_proposals(mapping_db)
    migrate_mapping_proposals(mapping_db)
    proposal = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "The scenario explicitly includes adversarial instructions.",
    )

    with sqlite3.connect(mapping_db) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                """
                UPDATE framework_mapping_proposals SET rationale = 'rewritten'
                WHERE proposal_id = ?
                """,
                (proposal["proposal_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute(
                "DELETE FROM framework_mapping_proposals WHERE proposal_id = ?",
                (proposal["proposal_id"],),
            )


def test_database_triggers_enforce_one_complete_human_decision(mapping_db):
    migrate_mapping_proposals(mapping_db)
    proposal = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "The scenario explicitly describes adversarial data.",
    )
    insert_values = (
        "map-direct-invalid",
        "SCN-AI-001",
        proposal["scenario_hash"],
        proposal["scenario_revision"],
        "mitre-atlas",
        "2026.07",
        "AML.T0043",
        "hypothesized",
        "A direct SQL claim.",
        "[]",
        "manual",
        "2026-08-12T00:00:00+00:00",
    )
    with sqlite3.connect(mapping_db) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="enter pending"):
            conn.execute(
                """
                INSERT INTO framework_mapping_proposals (
                    proposal_id, scenario_id, scenario_hash, scenario_revision,
                    framework,
                    content_version, technique_id, applicability, rationale,
                    evidence_refs_json, origin, status, reviewer, reason,
                    decided_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved',
                          'alice', 'bypass', '2026-08-12', ?)
                """,
                insert_values,
            )
        with pytest.raises(sqlite3.IntegrityError, match="enter pending"):
            conn.execute(
                """
                INSERT INTO framework_mapping_proposals (
                    proposal_id, scenario_id, scenario_hash, scenario_revision,
                    framework,
                    content_version, technique_id, applicability, rationale,
                    evidence_refs_json, origin, status, reviewer, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'pending', 'alice', ?)
                """,
                insert_values,
            )
        with pytest.raises(sqlite3.IntegrityError, match="complete pending"):
            conn.execute(
                """
                UPDATE framework_mapping_proposals
                SET status = 'approved', reviewer = 'alice', reason = ' '
                WHERE proposal_id = ?
                """,
                (proposal["proposal_id"],),
            )

        conn.execute(
            """
            UPDATE framework_mapping_proposals
            SET status = 'approved', reviewer = 'alice',
                reason = 'I verified the mapping.', decided_at = '2026-08-12'
            WHERE proposal_id = ?
            """,
            (proposal["proposal_id"],),
        )
        conn.commit()
        for rewrite in (
            "SET reason = 'rewritten'",
            "SET status = 'pending', reviewer = NULL, reason = NULL, decided_at = NULL",
            "SET status = 'rejected'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="complete pending"):
                conn.execute(
                    f"""
                    UPDATE framework_mapping_proposals {rewrite}
                    WHERE proposal_id = ?
                    """,
                    (proposal["proposal_id"],),
                )

    decided = get_mapping_proposal(mapping_db, proposal["proposal_id"])
    assert decided is not None
    assert decided["status"] == "approved"
    assert decided["reason"] == "I verified the mapping."


def test_scenario_hash_canonicalizes_json_lists_but_detects_content_edits():
    stored = {
        "title": "T",
        "description": "D",
        "core_risks": '[ "Model risk", "Data integrity" ]',
        "transmission_mechanism": "M",
        "affected_capabilities": '["Payments"]',
    }
    materialized = {
        **stored,
        "core_risks": ["Model risk", "Data integrity"],
        "affected_capabilities": ["Payments"],
    }
    assert scenario_content_hash(stored) == scenario_content_hash(materialized)
    assert scenario_content_hash(stored) != scenario_content_hash(
        {**stored, "description": "Changed"}
    )


def test_proposal_validates_scenario_release_and_object_type(mapping_db):
    with pytest.raises(MappingError, match="Scenario not found"):
        create_mapping_proposal(
            mapping_db, "missing", "AML.T0043", "A complete rationale."
        )
    with pytest.raises(MappingError, match="release not found"):
        create_mapping_proposal(
            mapping_db,
            "SCN-AI-001",
            "AML.T0043",
            "A complete rationale.",
            content_version="1900.01",
        )
    with pytest.raises(MappingError, match="not a technique"):
        create_mapping_proposal(
            mapping_db,
            "SCN-AI-001",
            "AML.TA0002",
            "A complete rationale.",
        )
    with pytest.raises(MappingError, match="rationale"):
        create_mapping_proposal(
            mapping_db,
            "SCN-AI-001",
            "AML.T0043",
            " ",
        )


def test_guard_rejection_leaves_mapping_proposal_unstored(mapping_db, monkeypatch):
    import workbench_atlas.frameworks.mappings as mapping_store
    from workbench_atlas.guard_hook import GuardRejected, GuardReport

    observed: dict[str, object] = {}

    def reject(records, *, seam):
        observed["records"] = records
        observed["seam"] = seam
        proposal_id = str(records[0]["id"])
        return GuardReport(
            {f"mapping_proposal:{proposal_id}": "REJECT"},
            [],
            [],
        )

    monkeypatch.setattr(mapping_store, "validate", reject)
    with pytest.raises(GuardRejected, match="create_mapping_proposal"):
        create_mapping_proposal(
            mapping_db,
            "SCN-AI-001",
            "AML.T0043",
            "I propose this mapping only for human review.",
        )

    assert observed["seam"] == "create_mapping_proposal"
    record = observed["records"][0]
    assert record["kind"] == "mapping_proposal"
    assert record["status"] == "pending"
    with sqlite3.connect(mapping_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM framework_mapping_proposals"
        ).fetchone()[0]
    assert count == 0


def test_pending_identity_deduplicates_but_decided_row_allows_reproposal(mapping_db):
    first = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "The first human-authored rationale.",
        evidence_refs=["EVT-1", "EVT-1", " EVT-2 "],
    )
    duplicate = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "A later duplicate intake does not rewrite the first claim.",
    )
    assert duplicate["proposal_id"] == first["proposal_id"]
    assert duplicate["rationale"] == first["rationale"]
    assert duplicate["evidence_refs"] == ["EVT-1", "EVT-2"]

    reject_mitre_atlas_mapping(
        mapping_db,
        first["proposal_id"],
        reviewer="alice",
        reason="The relationship is not sufficiently specific.",
    )
    retry = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "New evidence now makes the relationship specific.",
    )
    assert retry["proposal_id"] != first["proposal_id"]
    assert len(list_mapping_proposals(mapping_db)) == 2


def test_review_gate_updates_only_decision_fields_and_rejects_double_decision(
    mapping_db,
):
    proposal = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "Adversarial data is the described initiating mechanism.",
        content_version="2026.01",
    )
    before = get_mapping_proposal(mapping_db, proposal["proposal_id"])
    scenario_before = get_scenario(str(mapping_db), "SCN-AI-001")

    with pytest.raises(ReviewError, match="reviewer"):
        approve_mitre_atlas_mapping(
            mapping_db,
            proposal["proposal_id"],
            reviewer="",
            reason="Supported.",
        )
    with pytest.raises(ReviewError, match="written reason"):
        approve_mitre_atlas_mapping(
            mapping_db,
            proposal["proposal_id"],
            reviewer="alice",
            reason=" ",
        )

    approve_mitre_atlas_mapping(
        mapping_db,
        proposal["proposal_id"],
        reviewer="alice",
        reason="The initiating mechanism is explicit.",
    )
    after = get_mapping_proposal(mapping_db, proposal["proposal_id"])
    assert before is not None and after is not None
    decision_fields = {"status", "reviewer", "reason", "decided_at"}
    assert {
        key: value for key, value in before.items() if key not in decision_fields
    } == {
        key: value for key, value in after.items() if key not in decision_fields
    }
    assert after["status"] == "approved"
    assert after["reviewer"] == "alice"
    assert get_scenario(str(mapping_db), "SCN-AI-001") == scenario_before

    with pytest.raises(ReviewError, match="no pending decision"):
        reject_mitre_atlas_mapping(
            mapping_db,
            proposal["proposal_id"],
            reviewer="bob",
            reason="Attempted second decision.",
        )


def test_mapping_state_becomes_stale_when_meaning_bearing_scenario_changes(
    mapping_db,
):
    proposal = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "The described instructions are adversarial data.",
    )
    scenario = get_scenario(str(mapping_db), "SCN-AI-001")
    assert scenario is not None
    assert mapping_state(proposal, scenario) == "current"
    assert is_mapping_current(proposal, scenario)

    update_scenario_fields(
        str(mapping_db),
        "SCN-AI-001",
        {"description": "The scenario's meaning has materially changed."},
    )
    changed = get_scenario(str(mapping_db), "SCN-AI-001")
    assert changed is not None
    assert mapping_state(proposal, changed) == "stale"
    assert not is_mapping_current(proposal, changed)


def test_revision_prevents_restored_or_recreated_scenario_from_becoming_current(
    mapping_db,
):
    restored_claim = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "I reviewed the original scenario generation.",
    )
    original = get_scenario(str(mapping_db), "SCN-AI-001")
    assert original is not None
    original_description = original["description"]
    update_scenario_fields(
        str(mapping_db),
        "SCN-AI-001",
        {"description": "A temporary meaning-bearing change."},
    )
    update_scenario_fields(
        str(mapping_db),
        "SCN-AI-001",
        {"description": original_description},
    )
    restored = get_scenario(str(mapping_db), "SCN-AI-001")
    assert restored is not None
    assert scenario_content_hash(restored) == restored_claim["scenario_hash"]
    assert restored["scenario_revision"] > restored_claim["scenario_revision"]
    assert mapping_state(restored_claim, restored) == "stale"
    with pytest.raises(ReviewError, match="changed after mapping proposal"):
        approve_mitre_atlas_mapping(
            mapping_db,
            restored_claim["proposal_id"],
            reviewer="alice",
            reason="Restored text is still a later scenario generation.",
        )
    reject_mitre_atlas_mapping(
        mapping_db,
        restored_claim["proposal_id"],
        reviewer="alice",
        reason="I will remap the restored generation.",
    )

    recreated_claim = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "I reviewed the scenario before deletion.",
    )
    before_delete = get_scenario(str(mapping_db), "SCN-AI-001")
    assert before_delete is not None
    insertable = {
        key: value
        for key, value in before_delete.items()
        if key != "scenario_revision"
    }
    with sqlite3.connect(mapping_db) as conn:
        conn.execute("DELETE FROM scenarios WHERE scenario_id = 'SCN-AI-001'")
    upsert_scenario(str(mapping_db), insertable)
    recreated = get_scenario(str(mapping_db), "SCN-AI-001")
    assert recreated is not None
    assert scenario_content_hash(recreated) == recreated_claim["scenario_hash"]
    assert recreated["scenario_revision"] > recreated_claim["scenario_revision"]
    assert mapping_state(recreated_claim, recreated) == "stale"
    with pytest.raises(ReviewError, match="changed after mapping proposal"):
        approve_mitre_atlas_mapping(
            mapping_db,
            recreated_claim["proposal_id"],
            reviewer="alice",
            reason="Recreation requires a new review even with identical text.",
        )


def test_approval_revalidates_scenario_snapshot_and_pinned_technique(mapping_db):
    stale = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "The original scenario description supports this technique.",
    )
    update_scenario_fields(
        str(mapping_db),
        "SCN-AI-001",
        {"description": "A materially revised scenario description."},
    )
    with pytest.raises(ReviewError, match="changed after mapping proposal"):
        approve_mitre_atlas_mapping(
            mapping_db,
            stale["proposal_id"],
            reviewer="alice",
            reason="This decision is too late for the captured snapshot.",
        )
    assert get_mapping_proposal(mapping_db, stale["proposal_id"])["status"] == (
        "pending"
    )
    # I keep rejection available so a stale claim cannot become stuck in my
    # review queue merely because its scenario advanced.
    reject_mitre_atlas_mapping(
        mapping_db,
        stale["proposal_id"],
        reviewer="alice",
        reason="The scenario changed; I will submit a fresh mapping.",
    )

    missing_technique = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "This fresh proposal matches the revised scenario.",
    )
    with sqlite3.connect(mapping_db) as conn:
        conn.execute(
            """
            DELETE FROM framework_objects
            WHERE framework = 'mitre-atlas'
              AND content_version = '2026.07'
              AND object_id = 'AML.T0043'
            """
        )
    with pytest.raises(ReviewError, match="does not exist"):
        approve_mitre_atlas_mapping(
            mapping_db,
            missing_technique["proposal_id"],
            reviewer="alice",
            reason="The catalog reference must still resolve.",
        )

    missing_scenario = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "The older pinned release still has this technique.",
        content_version="2026.01",
    )
    with sqlite3.connect(mapping_db) as conn:
        conn.execute("DELETE FROM scenarios WHERE scenario_id = 'SCN-AI-001'")
    with pytest.raises(ReviewError, match="no longer exists"):
        approve_mitre_atlas_mapping(
            mapping_db,
            missing_scenario["proposal_id"],
            reviewer="alice",
            reason="The scenario reference must still resolve.",
        )


def test_approved_resolver_uses_pinned_relationship_directions_and_hides_pending(
    mapping_db,
):
    approved = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "The mechanism maps to adversarial data crafting.",
    )
    approve_mitre_atlas_mapping(
        mapping_db,
        approved["proposal_id"],
        reviewer="alice",
        reason="The scenario directly describes this behavior.",
    )
    pending_old_release = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "This separate pinned release remains pending.",
        content_version="2026.01",
    )

    context = resolve_mapping_context(mapping_db, "2026.07", "AML.T0043")
    assert context["name"] == "Craft Adversarial Data"
    assert context["technique_maturity"] == "Demonstrated"
    assert context["platforms"] == ["Generative AI", "Agentic AI"]
    assert context["tactics"][0]["tactic_id"] == "AML.TA0002"
    assert context["mitigations"][0]["mitigation_id"] == "AML.M0015"
    assert context["related_case_studies"][0]["case_study_id"] == "AML.CS0011"
    assert len(context["related_case_studies"]) == 1
    assert context["related_case_studies"][0]["relationships"] == [
        {
            "relationship_key": "rel-employs-1",
            "step-id": "S01",
            "tactic": "AI Attack Staging",
        },
        {
            "relationship_key": "rel-employs-2",
            "step-id": "S02",
            "tactic": "Impact",
        },
    ]

    scenario = get_scenario(str(mapping_db), "SCN-AI-001")
    assert scenario is not None
    grouped = approved_mappings_for_scenarios(mapping_db, [scenario])
    assert list(grouped) == ["SCN-AI-001"]
    assert len(grouped["SCN-AI-001"]) == 1
    resolved = grouped["SCN-AI-001"][0]
    assert resolved["proposal_id"] == approved["proposal_id"]
    assert resolved["state"] == "current"
    assert resolved["mapped_release"] == "2026.07"
    assert resolved["format_version"] == "6.0.0"
    assert resolved["release_date"] == "2026-07-31"
    assert resolved["source_url"].endswith("ATLAS-2026.07.yaml")
    assert resolved["source_sha256"] == "7" * 64
    assert pending_old_release["proposal_id"] not in {
        row["proposal_id"] for row in grouped["SCN-AI-001"]
    }


def test_mapping_context_and_provenance_remain_pinned_after_activation_drift(
    mapping_db,
):
    activate_release(mapping_db, "2026.01")
    proposal = create_mapping_proposal(
        mapping_db,
        "SCN-AI-001",
        "AML.T0043",
        "I assessed this claim against the active January release.",
    )
    approve_mitre_atlas_mapping(
        mapping_db,
        proposal["proposal_id"],
        reviewer="alice",
        reason="I verified the January technique definition.",
    )
    scenario = get_scenario(str(mapping_db), "SCN-AI-001")
    assert scenario is not None
    before = approved_mappings_for_scenarios(mapping_db, [scenario])[
        "SCN-AI-001"
    ][0]
    pinned = {
        key: before[key]
        for key in (
            "content_version",
            "mapped_release",
            "name",
            "technique_maturity",
            "scenario_hash",
            "format_version",
            "release_date",
            "source_url",
            "source_sha256",
        )
    }

    activate_release(mapping_db, "2026.07")
    assert framework_status(mapping_db)["active_release"] == "2026.07"
    after = approved_mappings_for_scenarios(mapping_db, [scenario])[
        "SCN-AI-001"
    ][0]
    assert {key: after[key] for key in pinned} == pinned
    assert after["content_version"] == "2026.01"
    assert after["name"] == "Craft Adversarial Data (legacy)"
    assert after["source_url"].endswith("ATLAS-2026.01.yaml")
    assert after["source_sha256"] == "1" * 64
