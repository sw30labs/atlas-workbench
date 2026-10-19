"""Bulk ingest: linker (aliases, vendors, dedup, conflicts, validation, idempotent ids)."""

import pytest

from bulk_ingest_helpers import (
    build_mini_pack, doc, private_settings, records, run, write_board_pptx, write_controls_csv,
    write_scenarios_csv, write_services_xlsx, write_vendors_csv,
)
from contingency_atlas.bulk_ingest.linker import Registry, VendorRegistry


@pytest.fixture
def psettings(settings, tmp_path):
    return private_settings(settings, tmp_path)


def test_service_aliases_resolve_to_one_id(psettings, tmp_path):
    root = tmp_path / "pack"
    write_services_xlsx(root / "catalogue.xlsx")
    write_vendors_csv(root / "vendors.csv")
    write_controls_csv(root / "controls.csv")
    result = run(psettings, root, tmp_path)
    cap = next(c for c in records(result, "services") if c["data"]["name"] == "Card Authorisation Platform (UK)")
    deps = [d for d in records(result, "dependencies") if d["data"]["from_service_id"] == cap["id"]]
    assert len(deps) == 2  # one via "CAP", one via "Card Auth"
    control = next(c for c in records(result, "controls") if c["data"]["name"] == "Daily failover check")
    assert control["data"]["service_ids"] == [cap["id"]]
    resolutions = result.links["services"]["resolutions"]
    assert resolutions["CAP"]["strategy"] == "acronym" and resolutions["Card Auth"]["strategy"] == "prefix"
    entity = next(e for e in result.links["services"]["entities"] if e["id"] == cap["id"])
    assert {"CAP", "Card Auth", "SVC-001"} <= set(entity["aliases"])
    assert cap["id"].startswith("bulk-svc-")


def test_registry_strategies_and_ambiguity():
    reg = Registry("services")
    uk = reg.add("Card Authorisation Platform (UK)")
    reg.add("Card Authorisation Platform (EU)", group="t")
    assert reg.resolve("card authorisation platform (uk)") == (uk.id, "exact")
    assert reg.resolve("Card Authorisation Platform") == (None, "ambiguous")
    assert reg.resolve("CAP") == (None, "ambiguous")
    assert reg.resolve("Payments Hub") == (None, "none")
    reg2 = Registry("services")
    hub = reg2.add("Retail Payments Hub")
    assert reg2.resolve("Retail Payment Hub!") == (hub.id, "normalized")
    assert reg2.resolve("Retail Payments") == (hub.id, "prefix")
    assert reg2.resolve("Payments Hub") == (hub.id, "subset")
    assert reg2.resolve("Hub") == (None, "none")  # one generic token is never enough
    assert reg2.resolve("Hub Retail Payments") == (hub.id, "jaccard")
    assert reg2.resolve("Retail Payments Hub (Retail)")[0] == hub.id


def test_ambiguous_and_orphan_mentions_become_issues_not_guesses(psettings, tmp_path):
    root = tmp_path / "pack"
    write_services_xlsx(root / "catalogue.xlsx", rows=[
        ("S1", "Card Authorisation Platform (UK)", "Owner A", "Tier 1", "4h", ""),
        ("S2", "Card Authorisation Platform (EU)", "Owner B", "Tier 1", "4h", ""),
    ])
    (root / "controls.csv").write_text(
        "Control Name,Control Owner,Service,Test Result\n"
        "Failover check,Ops,Card Authorisation Platform,Untested\n"
        "Branch alarm,Facilities,Branch Network,Untested\n")
    result = run(psettings, root, tmp_path)
    codes = {i["code"] for i in result.issues}
    assert {"ambiguous_link", "orphan_link"} <= codes
    for control in records(result, "controls"):
        assert control["status"] == "invalid" and control["meta"]["unresolved_links"]
        assert any("service_ids" in e for e in control["errors"])


def test_vendor_names_canonicalised():
    vendors = VendorRegistry()
    vendors.build(["Northwind Cloud Ltd", "Northwind Cloud Limited", "NORTHWIND CLOUD LTD.", "Contoso plc",
                   "Amazon Web Services", "AWS"])
    assert vendors.canonical("Northwind Cloud Limited") == vendors.canonical("NORTHWIND CLOUD LTD.")
    assert vendors.canonical("AWS") == "Amazon Web Services"
    assert vendors.canonical("Contoso plc") == "Contoso plc"


def test_dedup_across_files_keeps_both_provenances_and_raises_conflict(psettings, tmp_path):
    root = tmp_path / "pack"
    write_services_xlsx(root / "catalogue.xlsx")
    write_scenarios_csv(root / "scenario_register.csv", p99="£180m")
    write_board_pptx(root / "board_report.pptx", p99="£210m")
    result = run(psettings, root, tmp_path)
    scenarios = records(result, "org_scenarios")
    assert len(scenarios) == 1
    scenario = scenarios[0]
    assert {p["file"] for p in scenario["provenance"]} == {"scenario_register.csv", "board_report.pptx"}
    assert scenario["status"] == "needs_review"
    conflict = scenario["meta"]["conflicts"][0]
    assert conflict["field"] == "severity_p99" and sorted(conflict["values"]) == ["£180m", "£210m"]
    kept = dict(zip(conflict["files"], conflict["values"]))[scenario["provenance"][0]["file"]]
    assert scenario["data"]["severity_p99"] == {"£180m": 180e6, "£210m": 210e6}[kept]
    assert "conflict" in doc(result, "board_report.pptx")["flag_names"]
    assert any(i["code"] == "conflict" and i["severity"] == "error" for i in result.issues)


def test_same_service_in_two_files_merges_with_conflict(psettings, tmp_path):
    root = tmp_path / "pack"
    write_services_xlsx(root / "catalogue.xlsx")
    (root / "bia.csv").write_text("Business Service,Service Owner,RTO\nCAP,Different owner (synthetic),4h\n")
    result = run(psettings, root, tmp_path)
    services = records(result, "services")
    assert len(services) == 3
    cap = next(c for c in services if c["data"]["name"].startswith("Card"))
    assert len(cap["provenance"]) == 2 and cap["status"] == "needs_review"
    assert cap["meta"]["conflicts"][0]["field"] == "owner"


def test_validation_errors_are_surfaced_not_dropped_or_patched(psettings, tmp_path):
    root = tmp_path / "pack"
    write_services_xlsx(root / "catalogue.xlsx")
    write_controls_csv(root / "controls.csv")
    (root / "services_partial.csv").write_text("Business Service,Criticality\nNew Service X,Tier 9\n")
    result = run(psettings, root, tmp_path)
    reconciliation = next(c for c in records(result, "controls") if c["data"]["name"] == "Payment reconciliation")
    assert reconciliation["status"] == "invalid"
    assert reconciliation["data"]["validation_status"] == "validated"
    assert "validated_on" not in reconciliation["data"] or reconciliation["data"]["validated_on"] is None
    assert any("validation requires a date and evidence" in e for e in reconciliation["errors"])
    assert any(i["code"] == "validation_error" for i in result.issues)


def test_ids_are_idempotent_across_runs(psettings, tmp_path):
    root = build_mini_pack(tmp_path / "pack")
    first = run(psettings, root, tmp_path, out_dir=tmp_path / "one")
    second = run(psettings, root, tmp_path, out_dir=tmp_path / "two", max_workers=1)
    assert first.batch_id == second.batch_id
    assert [c["id"] for c in first.candidates] == [c["id"] for c in second.candidates]
    for item in first.handoff["org_context"]:
        assert (tmp_path / "one" / item["file"]).read_bytes() == (tmp_path / "two" / item["file"]).read_bytes()


def test_taxonomy_crosswalk_and_internal_losses(psettings, tmp_path):
    root = build_mini_pack(tmp_path / "pack")
    result = run(psettings, root, tmp_path)
    nodes = {c["data"]["org_code"]: c["data"] for c in records(result, "taxonomy_crosswalk")}
    assert nodes["R1"]["mapping_type"] == "exact" and nodes["R1"]["atlas_category"] == "Third-party dependency"
    assert nodes["R1.2"]["mapping_type"] == "narrower" and nodes["R1.2"]["rationale"]
    assert not nodes["R9"]["atlas_category"] and nodes["R9"]["mapping_type"] is None
    assert all(n["reviewer"] == "" and n["status"] == "proposed" and n["reviewed_on"] is None
               for n in nodes.values())
    losses = {c["data"]["org_ref"]: c for c in records(result, "internal_loss_events")}
    l1 = losses["L-001"]["data"]
    assert (l1["basel_event_type"], l1["occurrence_date"], l1["net_loss"], l1["currency"]) == \
        ("ET7", "2025-02-03", 1_000_000.0, "GBP")
    assert losses["L-001"]["contract_status"] == "agreed"
