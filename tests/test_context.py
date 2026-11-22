"""Organization declarations stay private, attributable and explicitly assessed."""

import json
import sqlite3
from datetime import date, timedelta

import pytest

from workbench_atlas.config import Settings, load_yaml_settings
from workbench_atlas.context import (
    ContextConflict, ContextError, archive_record, compare_baseline,
    context_payload, create_baseline, export_context, import_records,
    save_record, validate_record,
)


ACTOR = "synthetic-reviewer"
SOURCE = {"source": "Synthetic operator declaration", "classification": "synthetic"}


@pytest.fixture
def settings(tmp_path):
    scenario_db = tmp_path / "public.db"
    with sqlite3.connect(scenario_db) as conn:
        conn.execute("CREATE TABLE scenarios(scenario_id TEXT PRIMARY KEY, title TEXT)")
        conn.executemany("INSERT INTO scenarios VALUES(?,?)", [("scn-one", "Fictional outage"),
                                                               ("scn-two", "Unrelated fictional disruption")])
    return Settings(context_enabled=True, context_db_path=tmp_path / "private.db",
                    scenario_db_path=scenario_db, evidence_db_path=tmp_path / "evidence.db",
                    emerging_risks_path=tmp_path / "risks.json", scenario_export_path=tmp_path / "export.json")


def _save(settings, kind, **data):
    return save_record(settings, kind, {**SOURCE, **data}, actor=ACTOR)


def _service(settings, record_id="service-one"):
    return _save(settings, "services", id=record_id, name=record_id,
                 owner="Synthetic service owner", criticality="critical",
                 impact_tolerance="Fictional tolerance: two hours")


def _mechanism(settings, record_id="mechanism-one"):
    return _save(settings, "mechanisms", id=record_id, name=record_id,
                 description="Fictional loss of a shared dependency")


def _mapping(settings):
    service, mechanism = _service(settings), _mechanism(settings)
    exposure = _save(settings, "exposures", id="exposure-one", scenario_id="scn-one",
                     service_id=service["id"], mechanism_ids=[mechanism["id"]],
                     rationale="Synthetic scenario uses this fictional service", status="confirmed")
    return service, mechanism, exposure


def test_disabled_context_creates_nothing_and_does_not_disclose_existing_data(settings):
    disabled = settings.model_copy(update={"context_enabled": False})
    payload = context_payload(disabled)
    assert not settings.context_db_path.exists()
    assert payload["enabled"] is False
    assert payload["coverage"]["tested_fraction"] is None
    with pytest.raises(ContextError, match="disabled"):
        _service(disabled)
    _service(settings)
    assert context_payload(disabled)["records"]["services"] == []
    assert "context_db_path" not in settings.to_safe_dict()
    assert "context_enabled" not in settings.to_safe_dict()


def test_reads_do_not_create_stores_and_mutations_do_not_touch_public_store(settings):
    public_before = settings.scenario_db_path.read_bytes()
    assert context_payload(settings)["store_revision"] == 0
    assert not settings.context_db_path.exists()
    record = _service(settings)
    assert settings.scenario_db_path.read_bytes() == public_before
    assert record["revision"] == record["schema_version"] == 1
    assert record["classification"] == "synthetic"
    assert settings.context_db_path.stat().st_mode & 0o777 == 0o600
    private_before = settings.context_db_path.read_bytes()
    assert context_payload(settings)["records"]["services"][0]["id"] == record["id"]
    assert settings.context_db_path.read_bytes() == private_before


def test_public_database_path_and_hardlink_are_rejected(settings, tmp_path):
    bad = settings.model_copy(update={"context_db_path": settings.scenario_db_path})
    with pytest.raises(ContextError, match="separate"):
        _service(bad)
    link = tmp_path / "alias.db"
    link.hardlink_to(settings.scenario_db_path)
    with pytest.raises(ContextError, match="separate"):
        _service(settings.model_copy(update={"context_db_path": link}))


def test_unrelated_database_and_future_schema_are_not_mutated(settings):
    with sqlite3.connect(settings.context_db_path) as conn:
        conn.execute("CREATE TABLE unrelated(value TEXT)")
    before = settings.context_db_path.read_bytes()
    with pytest.raises(ContextError, match="another application"):
        _service(settings)
    assert settings.context_db_path.read_bytes() == before
    settings.context_db_path.unlink()
    with sqlite3.connect(settings.context_db_path) as conn:
        conn.execute("CREATE TABLE context_meta(schema_version INTEGER)")
        conn.execute("INSERT INTO context_meta VALUES(99)")
    before = settings.context_db_path.read_bytes()
    with pytest.raises(ContextError, match="unsupported"):
        _service(settings)
    assert settings.context_db_path.read_bytes() == before


def test_actor_revision_and_immutable_decision_history(settings):
    with pytest.raises(ContextError, match="actor"):
        save_record(settings, "services", {}, actor="  ")
    record = _service(settings)
    with pytest.raises(ContextConflict):
        save_record(settings, "services", {**record, "owner": "Second reviewer"}, actor=ACTOR)
    updated = save_record(settings, "services", {**record, "owner": "Second reviewer"},
                          actor="second-operator", expected_revision=record["revision"])
    assert updated["id"] == record["id"]
    assert updated["revision"] == 2
    with pytest.raises(ContextConflict):
        archive_record(settings, "services", record["id"], actor=ACTOR, expected_revision=1)
    with sqlite3.connect(settings.context_db_path) as conn:
        decisions = conn.execute("SELECT actor, before_json, after_json FROM context_decisions ORDER BY sequence").fetchall()
        assert len(decisions) == 2
        assert decisions[1][0] == "second-operator"
        assert json.loads(decisions[1][1])["owner"] == record["owner"]
        assert json.loads(decisions[1][2])["owner"] == "Second reviewer"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM context_decisions")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE context_decisions SET actor='forged'")


def test_links_and_stable_identifier_kind_are_validated(settings):
    service = _service(settings)
    with pytest.raises(ContextError, match="another kind"):
        _mechanism(settings, service["id"])
    with pytest.raises(ContextError, match="unknown scenario"):
        _save(settings, "exposures", scenario_id="missing", service_id=service["id"], rationale="fiction")
    with pytest.raises(ContextError, match="missing or archived"):
        _save(settings, "dependencies", name="fiction", from_service_id=service["id"],
              to_service_id="missing", category="supplier")
    with pytest.raises(ValueError, match="exactly one"):
        _save(settings, "dependencies", name="fiction", from_service_id=service["id"],
              to_service_id=service["id"], external_provider="provider", category="supplier")
    assert context_payload(settings)["store_revision"] == 1


def test_declared_denominator_and_explicit_assessment_transitions(settings):
    service, mechanism, exposure = _mapping(settings)
    _mechanism(settings, "mechanism-two")
    payload = context_payload(settings)
    assert payload["coverage"]["denominator"] == 2
    assert payload["coverage"]["counts"] == {"unassessed": 2, "mapped": 0, "tested": 0}
    assessment = _save(settings, "assessments", id="assessment-one", service_id=service["id"],
                       mechanism_id=mechanism["id"], status="mapped", exposure_ids=[exposure["id"]],
                       assessed_on=date.today().isoformat())
    assert context_payload(settings)["coverage"]["counts"] == {"unassessed": 1, "mapped": 1, "tested": 0}
    with pytest.raises(ContextError, match="requires confirmed"):
        save_record(settings, "assessments", {**assessment, "status": "tested"}, actor=ACTOR, expected_revision=1)
    exercise = _save(settings, "exercises", id="exercise-one", title="Fictional tabletop",
                     service_ids=[service["id"]], scenario_ids=["scn-one"], status="completed",
                     injects=["Fictional dependency outage"], outcomes=["Fictional recovery took three hours"],
                     evidence_refs=["synthetic://exercise-results"], completed_on=date.today().isoformat())
    assessed = save_record(settings, "assessments", {
        **assessment, "status": "tested", "exercise_ids": [exercise["id"]],
        "evidence_refs": ["synthetic://reviewed-exercise"],
    }, actor=ACTOR, expected_revision=1)
    assert assessed["revision"] == 2
    assert context_payload(settings)["coverage"]["counts"] == {"unassessed": 1, "mapped": 0, "tested": 1}
    # A later review withdraws the mapping. Coverage downgrades rather than
    # retaining a historical "tested" badge whose current basis disappeared.
    save_record(settings, "exposures", {**exposure, "status": "dismissed"}, actor=ACTOR, expected_revision=1)
    row = next(r for r in context_payload(settings)["coverage"]["rows"] if r["assessment_id"])
    assert row["status"] == "unassessed" and row["needs_reassessment"]


def test_unrelated_exercise_cannot_mark_a_cell_tested(settings):
    service, mechanism, exposure = _mapping(settings)
    exercise = _save(settings, "exercises", title="Unrelated tabletop", service_ids=[service["id"]],
                     scenario_ids=["scn-two"], status="completed", injects=["Fictional inject"],
                     outcomes=["Fictional outcome"], completed_on=date.today().isoformat(),
                     evidence_refs=["synthetic://unrelated"])
    with pytest.raises(ContextError, match="requires confirmed"):
        _save(settings, "assessments", service_id=service["id"], mechanism_id=mechanism["id"],
              status="tested", exposure_ids=[exposure["id"]], exercise_ids=[exercise["id"]],
              evidence_refs=["synthetic://assessment"], assessed_on=date.today().isoformat())


def test_closure_and_validation_require_evidence_and_overdue_is_explicit(settings):
    service = _service(settings)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    action = _save(settings, "actions", title="Synthetic recovery task", owner=ACTOR,
                   due_date=yesterday, service_ids=[service["id"]])
    _save(settings, "assumptions", statement="Fictional recovery assumption", owner=ACTOR,
          expires_on=yesterday, test="Run a synthetic failover exercise")
    assert len(context_payload(settings)["overdue"]) == 2
    with pytest.raises(ValueError, match="closure evidence"):
        save_record(settings, "actions", {**action, "status": "closed"}, actor=ACTOR, expected_revision=1)
    save_record(settings, "actions", {**action, "status": "closed", "closure_evidence": ["synthetic://completed"]},
                actor=ACTOR, expected_revision=1)
    assert len(context_payload(settings)["overdue"]) == 1
    with pytest.raises(ValueError, match="date and evidence"):
        _save(settings, "controls", name="Synthetic control", owner=ACTOR, service_ids=[service["id"]],
              validation_status="validated")


def test_archive_rejects_live_links_and_preserves_revisions(settings):
    service = _service(settings)
    dependency = _save(settings, "dependencies", name="Synthetic cloud dependency",
                       from_service_id=service["id"], external_provider="Fictional Provider",
                       category="cloud", common_cause="shared fictional power")
    with pytest.raises(ContextError, match="still referenced"):
        archive_record(settings, "services", service["id"], actor=ACTOR, expected_revision=1)
    archive_record(settings, "dependencies", dependency["id"], actor=ACTOR, expected_revision=1)
    archived = archive_record(settings, "services", service["id"], actor=ACTOR, expected_revision=1)
    assert archived["archived"] is True and archived["revision"] == 2
    assert context_payload(settings)["records"]["services"] == []
    assert len(context_payload(settings)["changes"]) == 4


def test_atomic_import_with_forward_links_and_failure_rollback(settings):
    entries = [
        {"kind": "dependencies", "data": {**SOURCE, "id": "dep", "name": "Fictional dependency",
                                          "from_service_id": "svc", "external_provider": "Fictional Provider", "category": "cloud"}},
        {"kind": "services", "data": {**SOURCE, "id": "svc", "name": "Fictional Service", "owner": ACTOR,
                                      "impact_tolerance": "Fictional two-hour tolerance"}},
    ]
    created = import_records(settings, entries, actor=ACTOR)
    assert len(created) == 2
    before = context_payload(settings)["store_revision"]
    bad = [
        {"kind": "mechanisms", "data": {**SOURCE, "id": "must-rollback", "name": "Fictional", "description": "Fictional"}},
        {"kind": "exposures", "data": {**SOURCE, "scenario_id": "scn-one", "service_id": "missing", "rationale": "Fictional"}},
    ]
    with pytest.raises(ContextError, match="missing or archived"):
        import_records(settings, bad, actor=ACTOR)
    assert context_payload(settings)["store_revision"] == before
    assert context_payload(settings)["records"]["mechanisms"] == []


def test_baselines_are_immutable_and_diffs_include_archival(settings):
    service = _service(settings)
    baseline = create_baseline(settings, "Synthetic quarter start", actor=ACTOR, expected_revision=1)
    archive_record(settings, "services", service["id"], actor=ACTOR, expected_revision=1)
    _service(settings, "new-service")
    diff = compare_baseline(settings, baseline["id"])
    assert diff["counts"] == {"added": 1, "changed": 1, "removed": 0}
    assert diff["changed"][0]["after"]["archived"] is True
    with pytest.raises(ContextConflict):
        create_baseline(settings, "Stale snapshot", actor=ACTOR, expected_revision=1)
    with sqlite3.connect(settings.context_db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE context_baselines SET name='tampered'")


def _research_fixture(settings):
    with sqlite3.connect(settings.scenario_db_path) as conn:
        for column in ("status", "evidence_refs", "signpost_evidence_refs", "signpost_checked_evidence_refs",
                       "signpost_review_status", "signpost_status", "signpost_assessment"):
            conn.execute(f"ALTER TABLE scenarios ADD COLUMN {column} TEXT")
        conn.execute("UPDATE scenarios SET status='active', evidence_refs='[\"source-one\"]' WHERE scenario_id='scn-one'")
        conn.execute("UPDATE scenarios SET status='active', evidence_refs='[\"source-two\"]' WHERE scenario_id='scn-two'")
    with sqlite3.connect(settings.evidence_db_path) as conn:
        conn.execute("CREATE TABLE evidence_events(event_id TEXT PRIMARY KEY, headline TEXT, source_date TEXT, "
                     "retrieved_at TEXT, reviewed_at TEXT, review_status TEXT, source_url TEXT, source_publisher TEXT, "
                     "content_fingerprint TEXT, independently_authored_summary TEXT)")
        conn.executemany("INSERT INTO evidence_events VALUES(?,?,?,?,?,?,?,?,?,?)", [
            ("source-one", "Synthetic outage", "2020-01-01", "2020-01-02", "2020-01-03", "approved_synthetic",
             "https://example.com/one", "Synthetic source", "one", "Original synthetic summary"),
            ("source-two", "Unrelated synthetic source", "2020-02-01", "2020-02-02", "2020-02-03", "pending",
             "https://example.com/two", "Another synthetic source", "two", "Unrelated synthetic summary"),
        ])


def test_research_watchlist_snapshots_are_read_only_and_unchanged_reads_are_suppressed(settings):
    _research_fixture(settings)
    _service(settings)
    public_before = {path: path.read_bytes() for path in (settings.scenario_db_path, settings.evidence_db_path)}
    baseline = create_baseline(settings, "Fictional watchlist", actor=ACTOR, scenario_ids=["scn-one"])
    research = compare_baseline(settings, baseline["id"])["research_changes"]
    assert research["status"] == "compared"
    assert research["scope"]["scenario_ids"] == ["scn-one"]
    assert research["baseline_as_of"] <= research["current_as_of"]
    for kind in ("scenarios", "evidence"):
        assert research[kind]["counts"] == {"added": 0, "changed": 0, "removed": 0}
    assert {path: path.read_bytes() for path in public_before} == public_before
    with sqlite3.connect(settings.context_db_path) as conn:
        snapshot = json.loads(conn.execute("SELECT snapshot_json FROM context_baselines").fetchone()[0])
    assert snapshot["schema_version"] == 2
    assert set(snapshot["research"]["scenarios"]) == {"scn-one"}
    assert set(snapshot["research"]["evidence"]) == {"source-one"}
    source = snapshot["research"]["evidence"]["source-one"]
    assert "independently_authored_summary" not in source
    assert source["independently_authored_summary_hash"]


def test_research_source_and_review_changes_have_field_diffs_and_scope_isolation(settings):
    _research_fixture(settings)
    baseline = create_baseline(settings, "Fictional watchlist", actor=ACTOR, scenario_ids=["scn-one"])
    with sqlite3.connect(settings.evidence_db_path) as conn:
        conn.execute("UPDATE evidence_events SET content_fingerprint='revised', reviewed_at='2020-03-04', "
                     "review_status='rejected', independently_authored_summary='Changed synthetic summary' WHERE event_id='source-one'")
        conn.execute("UPDATE evidence_events SET source_publisher='Unrelated change' WHERE event_id='source-two'")
    with sqlite3.connect(settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET title='Unrelated scenario change' WHERE scenario_id='scn-two'")
    research = compare_baseline(settings, baseline["id"])["research_changes"]
    assert research["scenarios"]["counts"] == {"added": 0, "changed": 0, "removed": 0}
    assert research["evidence"]["counts"] == {"added": 0, "changed": 1, "removed": 0}
    changed = research["evidence"]["changed"][0]
    assert changed["id"] == "source-one"
    fields = {item["field"]: item for item in changed["fields"]}
    assert fields["content_fingerprint"] == {"field": "content_fingerprint", "before": "one", "after": "revised"}
    assert fields["review_status"]["after"] == "rejected"
    assert "reviewed_at" in fields and "independently_authored_summary_hash" in fields
    assert "content_hash" not in fields


def test_research_removal_and_new_historical_source_do_not_claim_new_incidents(settings):
    _research_fixture(settings)
    baseline = create_baseline(settings, "Fictional watchlist", actor=ACTOR, scenario_ids=["scn-one"])
    with sqlite3.connect(settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET evidence_refs='[\"source-two\"]' WHERE scenario_id='scn-one'")
    research = compare_baseline(settings, baseline["id"])["research_changes"]
    assert research["evidence"]["removed"][0]["event_id"] == "source-one"
    assert research["evidence"]["added"][0]["event_id"] == "source-two"
    assert research["evidence"]["added"][0]["publication_relative_to_baseline"] == "historical_publication"
    assert research["evidence"]["added"][0]["links"][0]["scenario_id"] == "scn-one"
    # Physical deletion is observable only when the evidence store is readable.
    with sqlite3.connect(settings.evidence_db_path) as conn:
        conn.execute("DELETE FROM evidence_events WHERE event_id='source-two'")
    research = compare_baseline(settings, baseline["id"])["research_changes"]
    assert research["missing_evidence_ids"]["current"] == ["source-two"]


def test_research_unavailable_or_malformed_scope_never_fabricates_removals(settings):
    _research_fixture(settings)
    baseline = create_baseline(settings, "Fictional watchlist", actor=ACTOR, scenario_ids=["scn-one"])
    settings.evidence_db_path.unlink()
    diff = compare_baseline(settings, baseline["id"])["research_changes"]
    assert diff["status"] == "partial"
    assert diff["evidence"] == {"status": "unavailable", "added": [], "changed": [], "removed": [], "counts": None}
    with sqlite3.connect(settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET evidence_refs='malformed' WHERE scenario_id='scn-one'")
    diff = compare_baseline(settings, baseline["id"])["research_changes"]
    assert diff["evidence"]["counts"] is None
    assert diff["scenarios"]["counts"]["changed"] == 1
    settings.scenario_db_path.unlink()
    diff = compare_baseline(settings, baseline["id"])["research_changes"]
    assert diff["scenarios"]["status"] == "unavailable"
    assert diff["scenarios"]["removed"] == []


def test_default_research_scope_follows_new_current_scenarios_and_retains_retirements(settings):
    _research_fixture(settings)
    baseline = create_baseline(settings, "Fictional full portfolio", actor=ACTOR)
    with sqlite3.connect(settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET status='inactive', signpost_status='weakening', "
                     "signpost_review_status='superseded', signpost_evidence_refs='[\"source-one\"]' WHERE scenario_id='scn-one'")
        conn.execute("INSERT INTO scenarios(scenario_id,title,status) VALUES('scn-new','Fictional new scenario','active')")
        conn.execute("INSERT INTO scenarios(scenario_id,title,status) VALUES('scn-unrelated-history','Fictional historical scenario','inactive')")
        conn.execute("DELETE FROM scenarios WHERE scenario_id='scn-two'")
    diff = compare_baseline(settings, baseline["id"])["research_changes"]
    assert diff["scenarios"]["counts"] == {"added": 1, "changed": 1, "removed": 1}
    retired = diff["scenarios"]["changed"][0]["after"]
    assert retired["scenario_id"] == "scn-one"
    assert retired["scope_state"] == retired["signpost_state"] == "historical"
    assert diff["scenarios"]["added"][0]["scenario_id"] == "scn-new"
    sources = diff["evidence"]["changed"][0]["after"]
    assert all(link["scope_state"] == "historical" for link in sources["links"])


def test_watchlist_scope_validation_and_capacity_fail_closed(settings, monkeypatch):
    import workbench_atlas.context as module
    for ids in ([], "scn-one", [None], [""], ["missing"]):
        with pytest.raises(ContextError):
            create_baseline(settings, "Invalid watchlist", actor=ACTOR, scenario_ids=ids)
    monkeypatch.setattr(module, "MAX_WATCH_SCENARIOS", 1)
    with pytest.raises(ContextError, match="exceeds"):
        create_baseline(settings, "Too broad", actor=ACTOR)
    baseline = create_baseline(settings, "Bounded watchlist", actor=ACTOR, scenario_ids=["scn-one"])
    assert baseline["research_scope"]["scenario_ids"] == ["scn-one"]


def test_legacy_baseline_integrity_and_context_diff_survive_without_research_capture(settings):
    import workbench_atlas.context as module
    service = _service(settings)
    with sqlite3.connect(settings.context_db_path) as conn:
        conn.row_factory = sqlite3.Row
        snapshot = {"schema_version": 1, "records": module._records(conn, include_archived=True)}
        digest = module._hash(snapshot)
        conn.execute("INSERT INTO context_baselines VALUES(?,?,?,?,?,?,?)",
                     ("legacy", "Synthetic legacy baseline", "2020-01-01", ACTOR, 1, module._json(snapshot), digest))
    save_record(settings, "services", {**service, "owner": "Second synthetic owner"}, actor=ACTOR, expected_revision=1)
    diff = compare_baseline(settings, "legacy")
    assert diff["counts"]["changed"] == 1
    assert diff["research_changes"]["status"] == "not_captured"
    assert "scenarios" not in diff["research_changes"]
    with sqlite3.connect(settings.context_db_path) as conn:
        assert conn.execute("SELECT content_hash FROM context_baselines WHERE id='legacy'").fetchone()[0] == digest


def _evidence_assessment(settings, **changes):
    data = {"evidence_id": "source-one", "owner": ACTOR,
            "claim": "Fictional recovery may depend on one identity provider",
            "rationale": "Synthetic review of an explicitly scoped claim",
            "relation": "uncertain", "evidence_refs": ["synthetic://review-basis"], **changes}
    return _save(settings, "evidence_assessments", **data)


def test_evidence_family_and_claim_reviews_are_attributed_private_declarations(settings):
    _research_fixture(settings)
    public_before = {path: path.read_bytes() for path in (settings.scenario_db_path, settings.evidence_db_path)}
    first = _evidence_assessment(settings, scenario_ids=["scn-one"], relation="corroborates",
                                 directness="secondary_report", source_family="Fictional syndication family",
                                 incident_family="Fictional incident")
    second = _evidence_assessment(settings, evidence_id="source-two", scenario_ids=["scn-one"], relation="contradicts",
                                  source_family="Fictional syndication family", incident_family="Fictional incident")
    payload = context_payload(settings)
    rows = payload["records"]["evidence_assessments"]
    assert {row["relation"] for row in rows} == {"corroborates", "contradicts"}
    assert all(row["independence_status"] == "not_assessed" for row in rows)
    assert all(row["effective_verification_status"] == "unverified" for row in rows)
    diagnostic = payload["evidence_assessment_diagnostics"]
    assert diagnostic["counts"]["current"] == 2
    assert diagnostic["source_family_declarations"] == [{"label": "Fictional syndication family",
                                                         "assessment_ids": sorted([first["id"], second["id"]])}]
    assert {path: path.read_bytes() for path in public_before} == public_before
    assert all(change["actor"] == ACTOR for change in payload["changes"])


@pytest.mark.parametrize("updates", [
    {"independence_status": "independent"},
    {"independently_verified": True},
    {"independently_verified": "true"},
    {"independently_verified": True, "verified_on": date.today().isoformat()},
    {"independence_status": "independent", "independently_verified": True,
     "verified_on": (date.today() + timedelta(days=1)).isoformat()},
    {"evidence_refs": []}, {"owner": ""}, {"relation": "proven"}, {"directness": "strong"},
])
def test_evidence_independence_cannot_be_inferred_or_claimed_without_explicit_basis(settings, updates):
    _research_fixture(settings)
    with pytest.raises(ValueError):
        _evidence_assessment(settings, **updates)
    assert context_payload(settings)["records"]["evidence_assessments"] == []


def test_evidence_review_binds_target_revision_and_changed_source_reopens_verification(settings):
    _research_fixture(settings)
    assessment = _evidence_assessment(settings, independence_status="independent", independently_verified=True,
                                       verified_on=date.today().isoformat(), scenario_ids=["scn-one"])
    assert len(assessment["evidence_revision"]) == 64
    assert context_payload(settings)["records"]["evidence_assessments"][0]["effective_verification_status"] == "verified"
    baseline = create_baseline(settings, "Synthetic source review", actor=ACTOR, scenario_ids=["scn-one"])
    with sqlite3.connect(settings.evidence_db_path) as conn:
        conn.execute("UPDATE evidence_events SET content_fingerprint='changed' WHERE event_id='source-one'")
    payload = context_payload(settings)
    stale = payload["records"]["evidence_assessments"][0]
    assert stale["effective_evidence_state"] == "changed"
    assert stale["effective_verification_status"] == "needs_reassessment"
    inspected_revision = stale["current_evidence_revision"]
    assert inspected_revision != assessment["evidence_revision"]
    assert stale["independently_verified"] is True  # Historical declaration is retained.
    with pytest.raises(ContextConflict, match="source evidence changed"):
        save_record(settings, "evidence_assessments", {**stale, "rationale": "New review"},
                    actor=ACTOR, expected_revision=1)
    assert compare_baseline(settings, baseline["id"])["counts"]["changed"] == 0
    with pytest.raises(ContextConflict, match="exact inspected source"):
        save_record(settings, "evidence_assessments", {**stale, "evidence_revision": None},
                    actor=ACTOR, expected_revision=1)
    # An operator deliberately re-reviews the changed source; derived display
    # fields can round-trip without becoming stored business data.
    revised = save_record(settings, "evidence_assessments", {**stale, "evidence_revision": inspected_revision,
                          "rationale": "Explicit second synthetic source review"}, actor=ACTOR, expected_revision=1)
    assert revised["revision"] == 2 and revised["evidence_revision"] != assessment["evidence_revision"]
    assert "effective_evidence_state" not in revised
    assert "current_evidence_revision" not in revised
    with sqlite3.connect(settings.evidence_db_path) as conn:
        conn.execute("UPDATE evidence_events SET content_fingerprint='changed-again' WHERE event_id='source-one'")
    with pytest.raises(ContextConflict, match="source evidence changed"):
        save_record(settings, "evidence_assessments", {**revised, "evidence_revision": inspected_revision},
                    actor=ACTOR, expected_revision=2)
    with pytest.raises(ContextConflict):
        save_record(settings, "evidence_assessments", revised, actor=ACTOR, expected_revision=1)


def test_unknown_missing_or_corrupt_evidence_is_never_accepted_as_verified(settings):
    with pytest.raises(ContextError, match="unknown public evidence"):
        _evidence_assessment(settings)
    assert not settings.evidence_db_path.exists()
    _research_fixture(settings)
    with pytest.raises(ContextError, match="unknown public evidence"):
        _evidence_assessment(settings, evidence_id="missing-source")
    assessment = _evidence_assessment(settings, independently_verified=True, independence_status="shared_origin",
                                       verified_on=date.today().isoformat())
    with sqlite3.connect(settings.evidence_db_path) as conn:
        conn.execute("DELETE FROM evidence_events WHERE event_id='source-one'")
    missing = context_payload(settings)["records"]["evidence_assessments"][0]
    assert missing["effective_evidence_state"] == "missing"
    assert missing["effective_verification_status"] == "needs_reassessment"
    settings.evidence_db_path.write_bytes(b"synthetic corrupt database")
    payload = context_payload(settings)
    assert payload["evidence_assessment_diagnostics"]["status"] == "unavailable"
    assert payload["records"]["evidence_assessments"][0]["effective_evidence_state"] == "unavailable"
    with pytest.raises(ContextError, match="could not be verified"):
        _evidence_assessment(settings, evidence_id="source-two")
    assert context_payload(settings)["records"]["evidence_assessments"][0]["id"] == assessment["id"]


def test_private_evidence_review_export_requires_complete_explicit_scenario_scope(settings):
    _research_fixture(settings)
    scoped = _evidence_assessment(settings, evidence_id="source-two", scenario_ids=["scn-one"], relation="contradicts")
    _evidence_assessment(settings)  # An evidence-only assessment has no selected scenario scope.
    _evidence_assessment(settings, scenario_ids=["scn-two"])
    _evidence_assessment(settings, scenario_ids=["scn-one", "scn-two"])
    exported = export_context(settings, scenario_ids=["scn-one"])["evidence_assessments"]
    assert [row["id"] for row in exported] == [scoped["id"]]
    assert exported[0]["evidence_id"] == "source-two"
    assert exported[0]["effective_evidence_state"] == "current"
    assert exported[0]["current_evidence_review_status"] == "pending"


def test_saved_views_validate_fields_and_roundtrip(settings):
    view = _save(settings, "saved_views", name="Synthetic owner view", view_kind="services",
                 filters={"owner": ACTOR}, columns=["name", "owner"])
    assert view["kind"] == "saved_views" and view["view_kind"] == "services"
    with pytest.raises(ContextError, match="unknown filter"):
        _save(settings, "saved_views", name="Invalid", view_kind="services", filters={"secret-field": "x"})
    normalized = validate_record("saved_views", view)
    assert normalized["view_kind"] == "services" and "revision" not in normalized


def test_explicit_context_export_does_not_leak_unrelated_private_records(settings):
    service, _mechanism_record, _exposure = _mapping(settings)
    other = _service(settings, "other-service")
    _save(settings, "exposures", scenario_id="scn-two", service_id=other["id"], rationale="Unrelated fiction")
    _save(settings, "actions", title="Do not disclose unrelated action", owner=ACTOR,
          due_date=date.today().isoformat(), service_ids=[other["id"]], scenario_ids=["scn-two"])
    _save(settings, "actions", title="Shared action should be omitted", owner=ACTOR,
          due_date=date.today().isoformat(), service_ids=[service["id"], other["id"]], scenario_ids=["scn-one", "scn-two"])
    scoped_action = _save(settings, "actions", title="Scoped service task", owner=ACTOR,
                          due_date=date.today().isoformat(), service_ids=[service["id"]])
    before = settings.context_db_path.read_bytes()
    exported = export_context(settings, scenario_ids=["scn-one"])
    assert {r["id"] for r in exported["services"]} == {service["id"]}
    assert [r["id"] for r in exported["actions"]] == [scoped_action["id"]]
    assert "saved_views" not in exported
    assert "Do not disclose" not in json.dumps(exported)
    assert settings.context_db_path.read_bytes() == before
    with pytest.raises(ContextError, match="explicit"):
        export_context(settings, scenario_ids=[])


def test_export_missing_store_is_an_error_not_empty_success(settings):
    with pytest.raises(ContextError, match="not been created"):
        export_context(settings, scenario_ids=["scn-one"])


def test_yaml_context_opt_in_and_environment_precedence(tmp_path, monkeypatch):
    config = tmp_path / "atlas.yaml"
    config.write_text("organization_context:\n  enabled: true\n  db_path: private-context.db\n")
    monkeypatch.setenv("ATLAS_CONFIG", str(config))
    assert load_yaml_settings()["context_enabled"] is True
    loaded = Settings()
    assert loaded.context_enabled is True and loaded.context_db_path.name == "private-context.db"
    monkeypatch.setenv("ATLAS_CONTEXT_ENABLED", "false")
    assert Settings().context_enabled is False
    assert Settings(context_enabled=True).context_enabled is True


def test_service_scope_and_supplier_identifiers_are_explicit_metadata(settings):
    service = _save(settings, "services", name="Fictional scoped service", owner=ACTOR,
                    impact_tolerance="Fictional tolerance", legal_entity="Fictional Entity",
                    region="Fictional Region", process_ids=["external-process-1"],
                    process_names=["Fictional processing"], supplier_external_ids=["vendor-registry-1"])
    dependency = _save(settings, "dependencies", name="Fictional supplier", from_service_id=service["id"],
                       external_provider="Fictional Provider", supplier_external_id="vendor-registry-1", category="cloud")
    assert service["legal_entity"] == "Fictional Entity"
    assert dependency["supplier_external_id"] == service["supplier_external_ids"][0]


def test_impact_basis_units_missingness_and_tolerance_revision(settings):
    service = _service(settings)
    data = {**SOURCE, "title": "Fictional impact", "service_id": service["id"],
            "scenario_id": "scn-one", "observed_on": date.today().isoformat(),
            "time_since_onset_minutes": 30, "downtime_minutes": 15,
            "tolerance_reference": "Operator-declared fictional two-hour tolerance"}
    assumed = save_record(settings, "impact_assessments", data, actor=ACTOR)
    assert assumed["basis"] == "assumed" and assumed["backlog"] is None
    assert assumed["customers"] is None and assumed["service_revision"] == 1
    with pytest.raises(ValueError, match="measured impact requires"):
        save_record(settings, "impact_assessments", {**data, "basis": "measured"}, actor=ACTOR)
    measured = save_record(settings, "impact_assessments", {**data, "basis": "measured",
                           "customers": 0, "evidence_refs": ["synthetic://measurement"]}, actor=ACTOR)
    assert measured["customers"] == 0  # measured zero is distinct from missing
    changed = save_record(settings, "services", {**service, "impact_tolerance": "Changed fictional tolerance"},
                          actor=ACTOR, expected_revision=1)
    assert changed["revision"] == 2
    with pytest.raises(ContextConflict, match="tolerance or scope changed"):
        save_record(settings, "impact_assessments", {**data, "service_revision": 1}, actor=ACTOR)
    current = context_payload(settings)["records"]["impact_assessments"]
    assert {r["service_revision"] for r in current} == {1}  # prior observation basis remains fixed
    assert len(export_context(settings, scenario_ids=["scn-one"])["impact_assessments"]) == 2


@pytest.mark.parametrize("updates", [
    {"capacity_loss": 101}, {"downtime_minutes": -1}, {"backlog": float("nan")},
    {"customers": 1.5}, {"units": {"time_since_onset_minutes": "hours", "downtime_minutes": "minutes"}},
])
def test_invalid_impact_quantities_and_units_are_rejected(settings, updates):
    service = _service(settings)
    with pytest.raises(ValueError):
        _save(settings, "impact_assessments", title="Fictional impact", service_id=service["id"],
              observed_on=date.today().isoformat(), time_since_onset_minutes=1,
              tolerance_reference="Synthetic tolerance reference", **{**{"downtime_minutes": 0}, **updates})


def test_control_staleness_is_derived_without_rewriting_validation(settings):
    service = _service(settings)
    control = _save(settings, "controls", name="Fictional validated control", owner=ACTOR,
                    service_ids=[service["id"]], validation_status="validated",
                    validated_on=(date.today() - timedelta(days=31)).isoformat(),
                    evidence_refs=["synthetic://validation"], stale_after_days=30)
    with sqlite3.connect(settings.context_db_path) as conn:
        before = conn.execute("SELECT revision, data_json FROM context_records WHERE id=?", (control["id"],)).fetchone()
    payload = context_payload(settings)
    row = payload["records"]["controls"][0]
    assert row["validation_status"] == "validated" and row["effective_validation_status"] == "stale"
    assert payload["control_diagnostics"]["counts"]["stale"] == 1
    with sqlite3.connect(settings.context_db_path) as conn:
        assert conn.execute("SELECT revision, data_json FROM context_records WHERE id=?", (control["id"],)).fetchone() == before
    # A read-only enriched record can be edited without persisting derived fields.
    updated = save_record(settings, "controls", {**row, "notes": "Fictional review note"}, actor=ACTOR, expected_revision=1)
    assert "effective_validation_status" not in updated


def test_accepted_risk_requires_accountability_and_expired_acceptance_reopens_review(settings):
    service = _service(settings)
    data = {**SOURCE, "title": "Fictional acceptance", "owner": ACTOR,
            "service_ids": [service["id"]], "due_date": date.today().isoformat(), "decision_type": "accept_risk"}
    with pytest.raises(ValueError, match="risk acceptance requires"):
        save_record(settings, "actions", data, actor=ACTOR)
    accepted = save_record(settings, "actions", {**data, "status": "closed",
                           "rationale": "Explicit fictional decision rationale", "decision_evidence": ["synthetic://signed-decision"],
                           "acceptance_expires_on": (date.today() - timedelta(days=1)).isoformat()}, actor=ACTOR)
    payload = context_payload(settings)
    row = payload["records"]["actions"][0]
    assert row["status"] == "closed" and row["effective_acceptance_status"] == "expired"
    assert payload["overdue"][0]["id"] == accepted["id"]
    assert payload["overdue"][0]["reason"] == "risk_acceptance_expired"
