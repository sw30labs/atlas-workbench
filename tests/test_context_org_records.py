"""Org scenario registers, internal losses and taxonomy crosswalks are private context records."""

import sqlite3
from datetime import date, timedelta

import pytest

from contingency_atlas.config import Settings
from contingency_atlas.context import (
    ContextError, archive_record, context_payload, export_context, import_records, save_record,
)


ACTOR = "synthetic-reviewer"
SOURCE = {"source": "Synthetic operator declaration", "classification": "synthetic"}


@pytest.fixture
def settings(tmp_path):
    scenario_db = tmp_path / "public.db"
    with sqlite3.connect(scenario_db) as conn:
        conn.execute("CREATE TABLE scenarios(scenario_id TEXT PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO scenarios VALUES('scn-one', 'Fictional outage')")
    return Settings(context_enabled=True, context_db_path=tmp_path / "private.db",
                    scenario_db_path=scenario_db, evidence_db_path=tmp_path / "evidence.db",
                    emerging_risks_path=tmp_path / "risks.json", scenario_export_path=tmp_path / "export.json")


def _save(settings, kind, **data):
    return save_record(settings, kind, {**SOURCE, **data}, actor=ACTOR)


def _base(settings):
    _save(settings, "services", id="svc-pay", name="Fictional payments hub",
          owner="Synthetic owner", impact_tolerance="Fictional tolerance: four hours")
    _save(settings, "mechanisms", id="mech-cloud", name="Region loss", description="Fictional region outage")
    return _save(settings, "org_scenarios", id="org-scn-14", org_ref="ORG-SCN-014", title="Cloud region loss",
                 severity_p50=10.0, severity_p90=60.0, severity_p99=180.0, severity_currency="GBP",
                 service_ids=["svc-pay"], atlas_scenario_ids=["scn-one"])


def test_org_scenario_is_referenceable_like_an_atlas_scenario(settings):
    _base(settings)
    exposure = _save(settings, "exposures", id="exp-1", scenario_id="org-scn-14", service_id="svc-pay",
                     mechanism_ids=["mech-cloud"], rationale="Register scenario uses this service",
                     status="confirmed")
    _save(settings, "assessments", service_id="svc-pay", mechanism_id="mech-cloud", status="mapped",
          exposure_ids=[exposure["id"]], assessed_on=date.today().isoformat())
    payload = context_payload(settings)
    assert payload["coverage"]["counts"]["mapped"] == 1
    assert payload["missing_scenario_refs"] == []
    exported = export_context(settings, scenario_ids=["org-scn-14"])
    assert [r["id"] for r in exported["exposures"]] == ["exp-1"]


def test_unknown_scenarios_stay_rejected(settings):
    _base(settings)
    with pytest.raises(ContextError, match="unknown scenario"):
        _save(settings, "exposures", scenario_id="org-scn-99", service_id="svc-pay",
              rationale="No such scenario", status="proposed")
    with pytest.raises(ContextError, match="unknown Atlas scenario"):
        _save(settings, "org_scenarios", title="Bad crosswalk", atlas_scenario_ids=["scn-missing"])


def test_archived_org_scenario_is_no_longer_referenceable(settings):
    record = _base(settings)
    archive_record(settings, "org_scenarios", record["id"], actor=ACTOR, expected_revision=record["revision"])
    with pytest.raises(ContextError, match="unknown scenario"):
        _save(settings, "exposures", scenario_id="org-scn-14", service_id="svc-pay",
              rationale="Archived scenario", status="proposed")


def test_org_scenario_severity_must_be_ordered_and_priced(settings):
    with pytest.raises(Exception, match="non-decreasing"):
        _save(settings, "org_scenarios", title="Inverted", severity_p90=50.0, severity_p99=20.0,
              severity_currency="GBP")
    with pytest.raises(Exception, match="currency"):
        _save(settings, "org_scenarios", title="Unpriced", severity_p99=20.0)


def test_internal_loss_keeps_original_currency_and_declared_conversion(settings):
    _base(settings)
    loss = _save(settings, "internal_loss_events", org_ref="LE-2024-0001", basel_event_type="ET7",
                 occurrence_date="2024-03-01", discovery_date="2024-03-04", currency="EUR",
                 gross_loss=120000.0, recoveries=20000.0, net_loss=100000.0,
                 service_ids=["svc-pay"], scenario_ids=["org-scn-14"])
    assert loss["currency"] == "EUR" and loss["reporting_net_loss"] is None
    with pytest.raises(Exception, match="reporting-currency"):
        _save(settings, "internal_loss_events", org_ref="LE-2024-0002", currency="EUR",
              net_loss=10.0, reporting_currency="GBP", reporting_net_loss=8.5)
    full = _save(settings, "internal_loss_events", org_ref="LE-2024-0003", currency="EUR", net_loss=10.0,
                 reporting_currency="GBP", reporting_net_loss=8.5, fx_rate=0.85,
                 fx_rate_date="2024-03-31", fx_source="Synthetic month-end rate table")
    assert full["fx_rate"] == 0.85


@pytest.mark.parametrize("data, message", [
    ({"net_loss": 10.0}, "require a currency"),
    ({"occurrence_date": "2024-03-05", "discovery_date": "2024-03-01"}, "discovery cannot precede"),
    ({"occurrence_date": (date.today() + timedelta(days=1)).isoformat()}, "future"),
    ({"near_miss": True, "currency": "GBP", "gross_loss": 5.0}, "near miss"),
    ({"basel_event_type": "ET9"}, "ET1"),
])
def test_internal_loss_rules(settings, data, message):
    with pytest.raises(Exception, match=message):
        _save(settings, "internal_loss_events", org_ref="LE-X", **data)


def test_crosswalk_approval_requires_named_reviewer(settings):
    proposed = _save(settings, "taxonomy_crosswalk", org_code="NFR-04.03", org_label="Payment fraud",
                     atlas_category="Financial crime controls", mapping_type="narrower")
    assert proposed["status"] == "proposed"
    with pytest.raises(Exception, match="named reviewer"):
        _save(settings, "taxonomy_crosswalk", org_code="NFR-04.04", org_label="Card fraud",
              atlas_category="Financial crime controls", mapping_type="narrower", status="approved")
    with pytest.raises(Exception, match="unknown Atlas risk category"):
        _save(settings, "taxonomy_crosswalk", org_code="NFR-01", org_label="X", atlas_category="Not a category",
              mapping_type="exact")
    approved = _save(settings, "taxonomy_crosswalk", org_code="NFR-04.04", org_label="Card fraud",
                     atlas_category="Financial crime controls", mapping_type="narrower", status="approved",
                     reviewer="Synthetic taxonomy owner", reviewed_on=date.today().isoformat())
    assert approved["status"] == "approved"


def test_one_import_creates_register_then_dependent_records(settings):
    _save(settings, "services", id="svc-pay", name="Fictional payments hub",
          owner="Synthetic owner", impact_tolerance="Fictional tolerance: four hours")
    written = import_records(settings, [
        {"kind": "internal_loss_events", "data": {**SOURCE, "org_ref": "LE-1", "scenario_ids": ["org-scn-1"]}},
        {"kind": "exposures", "data": {**SOURCE, "scenario_id": "org-scn-1", "service_id": "svc-pay",
                                       "rationale": "Imported together"}},
        {"kind": "org_scenarios", "data": {**SOURCE, "id": "org-scn-1", "title": "Imported register entry"}},
    ], actor=ACTOR)
    assert [r["kind"] for r in written] == ["org_scenarios", "exposures", "internal_loss_events"]
