"""Bulk ingest: generic alias, reference, vendor, quantity and date-order resolution (synthetic names)."""

from contingency_atlas.bulk_ingest.extractors import extract_scenario_text, group_date_orders
from contingency_atlas.bulk_ingest.linker import (
    Registry, VendorRegistry, abbreviation_cover, apply_reference_aliases, duration_profile, link_batch,
    refs_equivalent, segment_acronyms, vendor_key,
)
from contingency_atlas.bulk_ingest.models import (
    Block, Candidate, Column, Document, DocumentResult, FileEntry, Provenance, Row, Table,
)
from contingency_atlas.bulk_ingest.readers import layout_tables
from contingency_atlas.bulk_ingest.synonyms import field_score
from contingency_atlas.bulk_ingest.triage import _note_pairs


def registry(*names):
    reg = Registry("services")
    for name in names:
        reg.add(name)
    return reg


def test_abbreviation_and_segment_strategies():
    assert abbreviation_cover("FP Gateway", "Faster Payments Gateway")
    assert abbreviation_cover("TF Docs", "Trade Finance Docs")
    assert abbreviation_cover("Trade Finance Documentation", "Trade Finance Docs")
    assert abbreviation_cover("Fraud & TM", "Fraud Detection & Transaction Monitoring")
    assert not abbreviation_cover("Fraud Engine", "Fraud Detection & Transaction Monitoring")
    assert not abbreviation_cover("FX Pricing Engine", "FX Trading & Pricing")
    assert not abbreviation_cover("Gateway", "Faster Payments Gateway")  # one generic word is not enough
    assert "HVP" in segment_acronyms("CHAPS & High-Value Payments")
    reg = registry("Faster Payments Gateway", "CHAPS & High-Value Payments", "Collateral Management")
    assert reg.match("FP Gateway")[1] == "abbreviation"
    assert reg.match("HVP")[1] == "segment_acronym"
    hits, strategy = reg.match("CollMan")
    assert [e.canonical for e in hits] == ["Collateral Management"] and strategy.startswith("camel_case")


def test_org_prefix_and_slash_alternatives_and_ambiguity():
    reg = registry("Mobile Banking App", "Client Onboarding & KYC", "Northwind Payments", "Northwind Payroll")
    reg.org_words = {"zenith"}
    hits, strategy = reg.match("Zenith Mobile")
    assert [e.canonical for e in hits] == ["Mobile Banking App"] and strategy.startswith("org_prefix")
    hits, strategy = reg.match("CLM/KYC")
    assert [e.canonical for e in hits] == ["Client Onboarding & KYC"] and strategy == "slash_alternatives"
    resolved, strategy = reg.resolve("Northwind")
    assert resolved is None and strategy == "ambiguous"  # several hits stay an issue, never a guess


def test_reference_aliases_from_a_keyed_table():
    reg = Registry("services")
    reg.add("Faster Payments Gateway", ["SVC-0003"])
    reg.add("Online Banking", ["SVC-0009"])
    issues = []
    added = apply_reference_aliases(reg, [
        {"ref": "SVC-0003", "name": "FPS Gateway", "file": "p.xlsx", "locator": "Processes!A4"},
        {"ref": "SVC-0009", "name": "Faster Payments Gateway", "file": "p.xlsx", "locator": "Processes!A5"},
        {"ref": "SVC-9999", "name": "Unknown", "file": "p.xlsx", "locator": "Processes!A6"},
    ], issues)
    assert added == 1 and reg.resolve("FPS Gateway")[1] == "reference"
    assert [i.code for i in issues] == ["ambiguous_link"]  # a name already owned by another service


def test_vendor_suffixes_dotted_abbreviations_and_register_ids():
    assert vendor_key("Bloomberg L.P.") == vendor_key("Bloomberg") == "bloomberg"
    assert vendor_key("S.W.I.F.T. SC") == "swift"
    assert vendor_key("Lumen Risk Solutions") == "lumen risk"
    assert vendor_key("Marlow Clearing Bank") == "marlow clearing"
    assert vendor_key("salesforce.com EMEA Ltd") == "salesforce"
    assert vendor_key("Bank of Avalon") == "bank of avalon"  # only a trailing descriptor is dropped
    reg = VendorRegistry()
    reg.build(["Tessaract Data Labs Ltd", "Tessaract", "BT Business", "British Telecommunications plc"])
    assert reg.canonical("Tessaract") == "Tessaract Data Labs Ltd"
    assert reg.canonical("BT Business") == "British Telecommunications plc"
    reg = VendorRegistry()  # one register, two supplier ids: two suppliers, whatever the names say
    reg.build(["Orbital Ireland Operations Ltd", "Orbital 365"], ["reg.csv#TP-2", "reg.csv#TP-4"])
    assert reg.canonical("Orbital 365") != reg.canonical("Orbital Ireland Operations Ltd")


def test_reference_equivalence():
    assert refs_equivalent("SCN-22", "ZEN-SCN-2025-022")
    assert refs_equivalent("SCN-2025-007", "ZEN-SCN-2025-007")
    assert not refs_equivalent("SCN-22", "ZEN-SCN-2025-023")
    assert not refs_equivalent("KC-PAY-002", "KC-RES-002")
    assert not refs_equivalent("SCN-2024-007", "ZEN-SCN-2025-007")


def cand(kind, data, file, ref=None, **meta):
    meta = {"raw": {k: str(v) for k, v in data.items()}, **({"org_ref": ref} if ref else {}), **meta}
    return Candidate(kind, dict(data), [Provenance(file, "row 1", "extractor", "deterministic")], meta=meta)


def test_quantities_compare_on_value_not_text():
    assert duration_profile("RTO 4 hours (240 minutes); RPO 1 hour") == {"rto": 240.0, "rpo": 60.0}
    assert duration_profile("Statement: within 4 hours of the start") == {}
    base = {"owner": "Head of Ops (synthetic)", "criticality": "critical"}
    same = link_batch([cand("services", {**base, "name": "Ledger", "impact_tolerance": "RTO 4 hours; RPO 1 hour"},
                            "a.xlsx", "SVC-1"),
                       cand("services", {**base, "name": "Ledger", "impact_tolerance": "4 hours"}, "b.docx", "SVC-1")])
    assert not any(i.code == "conflict" for i in same.issues)
    differ = link_batch([cand("services", {**base, "name": "Ledger", "impact_tolerance": "RTO 4 hours"},
                              "a.xlsx", "SVC-1"),
                         cand("services", {**base, "name": "Ledger", "impact_tolerance": "2 hours"}, "b.docx", "SVC-1")])
    assert any(i.code == "conflict" for i in differ.issues)
    freq = link_batch([cand("org_scenarios", {"title": "Ledger outage", "frequency_text": "1-in-10"}, "r.xlsx",
                            "ZEN-SCN-2025-003"),
                       cand("org_scenarios", {"title": "Ledger outage", "frequency_text": "1 in 10 years"}, "d.pptx",
                            "ZEN-SCN-2025-003")])
    assert not any(i.code == "conflict" for i in freq.issues)


def test_abbreviated_scenario_refs_merge_and_link():
    register = cand("org_scenarios", {"title": "Ledger outage", "org_ref": "ZEN-SCN-2025-022"}, "register.xlsx",
                    "ZEN-SCN-2025-022")
    minutes = cand("org_scenarios", {"title": "RTGS outage", "org_ref": "SCN-22"}, "minutes.docx", "SCN-22")
    action = cand("actions", {"title": "Get buffer data", "owner": "Treasury (synthetic)", "due_date": "2026-02-28"},
                  "minutes.docx", scenario_mentions=["SCN-22"])
    linked = link_batch([register, minutes, action])
    scenarios = [c for c in linked.candidates if c.kind == "org_scenarios"]
    assert len(scenarios) == 1 and "SCN-22" in scenarios[0].meta["aliases"]
    assert not any(i.code == "conflict" for i in linked.issues)  # an abbreviated ref is an alias, not a conflict
    act = next(c for c in linked.candidates if c.kind == "actions")
    assert act.data["scenario_ids"] == [scenarios[0].id] and act.status == "ready"


def test_header_containment_ignores_attribute_words_and_decimals():
    assert field_score("dependencies", "name", "Dependency RTO") == 0
    assert field_score("dependencies", "name", "Dependency") == 3
    assert field_score("org_scenarios", "severity_p99", "Register P99 (GBP m)") == 1
    assert field_score("org_scenarios", "severity_p99", "Stressed P99.9 (GBP m)") == 0


def table(header, rows, prefix="t"):
    return Table(prefix, [Column(i, h) for i, h in enumerate(header)],
                 [Row(n, r, f"row {n}") for n, r in enumerate(rows, start=1)])


def test_date_order_per_source_group_only_when_consistent():
    rows = [["UK", "25/03/2025"], ["UK", "02/04/2025"], ["US", "03/25/2025"], ["US", "02/04/2025"]] * 3
    found = group_date_orders(table(["System", "Date"], rows), [1])
    assert found is not None and found[0] == ["System"]
    assert found[1][1] is True and found[1][3] is False  # same text, different meaning per group
    mixed = rows + [["UK", "03/25/2025"]]  # a group that contradicts itself: refuse
    assert group_date_orders(table(["System", "Date"], mixed), [1]) is None


def test_layout_tables_rebuild_wrapped_cells():
    text = "\n".join([
        " 6.3 Scenarios",
        " Ref           Scenario                    Frequency    Register P99",
        "                                                        (GBP m)",
        " SCN-2025-020  Identity and cloud cascade  1-in-50      450",
        "",
        " SCN-2025-007  Ransomware across the       1-in-20      310",
        "               retail estate",
        " Paragraph text that runs straight across every column boundary of the table above it.",
    ])
    (labels, rows), = layout_tables(text)
    assert labels == ["Ref", "Scenario", "Frequency", "Register P99 (GBP m)"]
    assert [r[1] for r in rows] == [["SCN-2025-020", "Identity and cloud cascade", "1-in-50", "450"],
                                    ["SCN-2025-007", "Ransomware across the retail estate", "1-in-20", "310"]]
    wrapped = "\n".join([" Test       Status      Date", " EX-26-0    Complete    2026-05-2",
                         " 1          d           0", " EX-26-0    Planned     2026-10-2", " 2                      2"])
    (_, rows), = layout_tables(wrapped)
    assert [r[1] for r in rows] == [["EX-26-01", "Completed", "2026-05-20"], ["EX-26-02", "Planned", "2026-10-22"]]


def test_note_rows_feed_context():
    assert _note_pairs("Basis: assumed (workshop estimates, not measured). Impact tolerance: 4 hours.") == [
        ("Basis", "assumed (workshop estimates, not measured)"), ("Impact tolerance", "4 hours")]


def test_minutes_scenario_decisions():
    blocks = [Block("Scenario 22 - RTGS outage", "paragraph 1", "heading", 1),
              Block("SCN-22: Treasury noted costs. Agreed: P90 raised from £30m to £34m; P99 unchanged at £95m "
                    "pending data (action WS-25-11-06).", "paragraph 2"),
              Block("Scenario 14 - Cloud region loss", "paragraph 3", "heading", 1),
              Block("Agreed: P99 revised from £180m to £195m (action WS-25-11-02). Frequency 1-in-30 unchanged.",
                    "paragraph 4")]
    result = DocumentResult(FileEntry("minutes.docx", 1, "accepted"), Document("minutes.docx", "docx", blocks=blocks))
    found = {c.data["title"]: c for c in extract_scenario_text(result)}
    rtgs, cloud = found["RTGS outage"], found["Cloud region loss"]
    assert (rtgs.meta["org_ref"], rtgs.data["severity_p90"], rtgs.data["severity_p99"]) == ("SCN-22", 34e6, 95e6)
    assert rtgs.data["severity_currency"] == "GBP" and "P90 raised" in rtgs.provenance[0].spans["severity_p90"]
    assert "org_ref" not in cloud.meta  # 'WS-25-11-02' is an action id, not scenario 14's reference
    assert cloud.data["severity_p99"] == 195e6 and cloud.data["frequency_text"] == "1-in-30"
