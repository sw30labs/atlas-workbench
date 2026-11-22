"""Bulk ingest: document structures seen in real estates (cover sheets, hierarchies, sections, feeds)."""

import json

import pytest
from openpyxl import Workbook

from bulk_ingest_helpers import doc, private_settings, records, run, write_services_xlsx
from workbench_atlas.bulk_ingest import coerce


@pytest.fixture
def psettings(settings, tmp_path):
    return private_settings(settings, tmp_path)


def write_bia(path):
    wb = Workbook()
    cover = wb.active
    cover.title = "Cover"
    for row in (["Business Impact Analysis", "Retail Payments Hub (RPH)"], ["BIA owner", "Payments lead (synthetic)"],
                [], ["Version history"], ["Version", "Date", "Change"], ["1.0", "2026-01-01", "First issue"]):
        cover.append(row)
    deps = wb.create_sheet("Dependencies")
    deps.append(["Dep ID", "Direction", "Type", "Name", "What is needed", "Criticality"])
    deps.append(["D01", "Upstream", "Internal service", "Mortgage Origination Service", "Rates feed", "High"])
    deps.append(["D02", "Downstream", "Internal service", "CAP", "Payment journeys", "Critical"])
    deps.append(["D03", "Upstream", "Supplier", "Northwind Cloud Ltd", "Hosting", "Critical"])
    deps.append(["D04", "Upstream", "Unknown", "Something", "?", "Low"])
    impact = wb.create_sheet("Impact over time")
    impact.append(["Time since disruption", "Customers unable to pay", "Payments backlog (count)", "Notes"])
    impact.append(["1 hour", 85000, 120000, ""])
    impact.append(["4 hours", 420000, 610000, ""])
    impact.append(["Basis: assumed workshop estimates, not measured.", None, None, None])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def test_cover_context_feeds_dependency_and_impact_tables(psettings, tmp_path):
    root = tmp_path / "pack"
    write_services_xlsx(root / "catalogue.xlsx")
    write_bia(root / "bia_retail_payments.xlsx")
    result = run(psettings, root, tmp_path)
    d = doc(result, "bia_retail_payments.xlsx")
    assert d["route_names"] == ["dependencies", "impact_assessments"]
    assert d["context"]["business impact analysis"]["value"] == "Retail Payments Hub (RPH)"
    ids = {c["data"]["name"]: c["id"] for c in records(result, "services")}
    rph, cap, mort = ids["Retail Payments Hub"], ids["Card Authorisation Platform (UK)"], ids["Mortgage Origination Service"]
    deps = {c["meta"]["raw"]["target"]: c for c in records(result, "dependencies", "bia_retail_payments.xlsx")}
    assert (deps["Mortgage Origination Service"]["data"]["from_service_id"],
            deps["Mortgage Origination Service"]["data"]["to_service_id"]) == (rph, mort)
    assert (deps["CAP"]["data"]["from_service_id"], deps["CAP"]["data"]["to_service_id"]) == (cap, rph)  # downstream
    assert deps["Northwind Cloud Ltd"]["data"]["external_provider"] == "Northwind Cloud Ltd"
    assert deps["Something"]["status"] == "invalid"  # unknown type: no destination guessed
    assert "service from document context" in deps["CAP"]["meta"]["derived_fields"]
    entity = next(e for e in result.links["services"]["entities"] if e["id"] == rph)
    assert "RPH" in entity["aliases"]
    impacts = records(result, "impact_assessments")
    assert len(impacts) == 2 and {i["data"]["service_id"] for i in impacts} == {rph}
    four = next(i["data"] for i in impacts if i["data"]["time_since_onset_minutes"] == 240.0)
    assert four["customers"] == 420000 and four["backlog"] == 610000.0
    assert any(i["code"] == "note_rows_skipped" for i in result.issues)


def test_hierarchical_taxonomy_is_exploded_with_parents(psettings, tmp_path):
    root = tmp_path / "pack"
    root.mkdir()
    wb = Workbook()
    ws = wb.active
    ws.append(["Risk taxonomy v2 (synthetic) | a long banner that is not a header row at all"])
    ws.append(["L1 Code", "L1 Name", "L2 Code", "L2 Name", "Definition"])
    ws.append(["T-01", "Technology", "T-01.01", "Cyber attack", "Loss from cyber attack."])
    ws.append([None, None, "T-01.02", "Cloud vendor outage", "Outage at a cloud vendor."])
    ws.append(["T-02", "People", "T-02.01", "Staff conduct", "Misconduct."])
    wb.save(root / "risk_taxonomy_v2.xlsx")
    result = run(psettings, root, tmp_path)
    nodes = {c["data"]["org_code"]: c["data"] for c in records(result, "taxonomy_crosswalk")}
    assert set(nodes) == {"T-01", "T-01.01", "T-01.02", "T-02", "T-02.01"}
    assert nodes["T-01.02"]["parent_code"] == "T-01" and nodes["T-01.02"]["level"] == 2
    assert nodes["T-01.02"]["atlas_category"] == "Third-party dependency"
    assert nodes["T-02"]["parent_code"] == "" and nodes["T-02"]["level"] == 1


def test_heading_sections_with_key_values(psettings, tmp_path):
    from docx import Document

    root = tmp_path / "pack"
    write_services_xlsx(root / "catalogue.xlsx")
    d = Document()
    d.add_heading("Impact tolerance statements (synthetic)", level=1)
    for ref, name, owner, hours in (("SVC-001", "Card Authorisation Platform (UK)", "Head of Cards (synthetic)", 2),
                                    ("SVC-002", "Retail Payments Hub", "Head of Payments (synthetic)", 4)):
        d.add_heading(f"{ref} - {name}", level=2)
        d.add_paragraph(f"Service owner: {owner}. Legal entity: Fictional Bank plc.")
        d.add_paragraph(f"Maximum tolerable duration: {hours} hours")
    d.save(root / "tolerance_statements.docx")
    result = run(psettings, root, tmp_path)
    route = doc(result, "tolerance_statements.docx")["routes"][0]
    assert route["route"] == "services" and route["target"] == "sections"
    cap = next(c for c in records(result, "services") if c["data"]["name"].startswith("Card"))
    hub = next(c for c in records(result, "services") if c["data"]["name"] == "Retail Payments Hub")
    assert len(cap["provenance"]) == 2 and len(hub["provenance"]) == 2  # merged with the catalogue rows
    conflict = cap["meta"]["conflicts"][0]
    assert conflict["field"] == "impact_tolerance" and "2 hours" in conflict["values"]
    assert cap["status"] == "needs_review" and cap["data"]["owner"] == "Head of Cards (synthetic)"


def test_nested_json_feed_and_near_miss_log(psettings, tmp_path):
    root = tmp_path / "pack"
    root.mkdir()
    (root / "feed_export.json").write_text(json.dumps({"items": [
        {"id": "F-1", "title": "Fictional outage at Example Bank", "published": "2024-05-01",
         "entity": {"name": "Example Bank (fictional)", "country": "Exampleland"},
         "citation": {"url": "https://example.com/a", "publisher": "Example News"},
         "summary": "A fictional summary for tests."}]}))
    wb = Workbook()
    ws = wb.active
    ws.append(["Near miss log 2026 (synthetic)"])
    ws.append(["NM Ref", "Date identified", "What happened", "Potential impact (GBP)", "Status"])
    ws.append(["NM-2026-001", "2026-01-10", "Wrong batch nearly released", 17697, "Closed - near miss"])
    wb.save(root / "near_miss_log.xlsx")
    result = run(psettings, root, tmp_path)
    evidence = records(result, "evidence_events")[0]["data"]
    assert evidence["source_url"] == "https://example.com/a" and evidence["organization"] == "Example Bank (fictional)"
    loss = records(result, "internal_loss_events")[0]
    assert loss["data"]["near_miss"] is True and loss["data"]["potential_impact"] == 17697.0
    assert loss["data"]["currency"] == "GBP" and loss["data"]["status"] == "closed" and not loss["errors"]


def test_merge_semantics_local_refs_rounding_and_currency(psettings, tmp_path):
    root = tmp_path / "pack"
    root.mkdir()
    (root / "pir_one.md").write_text("| Ref | Action | Owner | Due |\n|---|---|---|---|\n| A1 | Fix DNS | Ops | 2026-01-31 |\n")
    (root / "pir_two.md").write_text("| Ref | Action | Owner | Due |\n|---|---|---|---|\n| A1 | Rotate keys | Sec | 2026-02-28 |\n")
    (root / "loss_ledger.csv").write_text("Event Ref,Occurrence Date,Gross Loss,Net Loss,Ccy\n"
                                          "LE-2025-00018,2025-01-02,1234567,1234567,GBP\n"
                                          "LE-2025-00019,2025-01-03,500000,500000,USD\n")
    (root / "annual_report.md").write_text("| Event ref | Occurrence Date | Net (GBP m) |\n|---|---|---|\n"
                                           "| LE-2025-00018 | 2025-01-02 | 1.2 |\n| LE-2025-00019 | 2025-01-03 | 0.4 |\n")
    result = run(psettings, root, tmp_path)
    assert len(records(result, "actions")) == 2  # 'A1' is local to each document
    losses = {c["data"]["org_ref"]: c for c in records(result, "internal_loss_events")}
    assert len(losses["LE-2025-00018"]["provenance"]) == 2
    assert not losses["LE-2025-00018"]["meta"].get("conflicts")  # 1.2m is the ledger value, rounded
    assert not losses["LE-2025-00019"]["meta"].get("conflicts")  # USD vs GBP: not comparable
    assert any(i["code"] == "currency_mismatch" for i in result.issues)


def test_row_level_date_order_and_status_segments():
    assert coerce.status("internal_loss_events", "Open - provision") == "open"
    assert coerce.status("controls", "Design only - not tested") == "untested"
    assert coerce.status("controls", "Effective with exceptions") is None  # qualified results stay for review
    assert coerce.parse_date("20 May 2026, 09:00-15:00") == ("2026-05-20", "")
    assert coerce.tolerance_text(tolerance="4 hrs") == "4 hours"
