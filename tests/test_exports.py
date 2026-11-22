"""Synthetic read-only export, selection, provenance and format regressions."""

import csv
import hashlib
import io
import json
import sqlite3
import zipfile

import pytest

from workbench_atlas.exports import ExportError, build_snapshot, export_data, main


@pytest.mark.parametrize("format", [[], {}, None, 1, True])
def test_render_rejects_malformed_format_without_type_error(format):
    from workbench_atlas.exports import render_snapshot

    with pytest.raises(ExportError, match="Choose json, csv, xlsx, pdf, docx or pptx"):
        render_snapshot({}, format=format)


@pytest.mark.parametrize("format", ["docx", "pptx"])
def test_editable_office_reports_preserve_source_text_and_private_context(export_settings, format):
    from workbench_atlas.exports import render_snapshot

    snapshot = build_snapshot(export_settings, scenario_ids=["SYN-SCN-A"])
    snapshot["manifest"]["classification"] = "includes_organization_context"
    snapshot["manifest"]["warnings"].append("Synthetic unresolved reference requires review.")
    snapshot["tables"]["organization_changes"] = [{"record_id": "Fictional service", "field": "owner",
        "before": {"owner": "Former fictional owner"}, "after": {"owner": "New fictional owner"},
        "notes": "Unicode café 金融 <untrusted> \x01"}]
    before = json.dumps(snapshot, sort_keys=True)
    artifact = render_snapshot(snapshot, format=format)
    assert artifact.filename.endswith("." + format)
    assert artifact.snapshot_id == snapshot["manifest"]["snapshot_id"]
    assert json.dumps(snapshot, sort_keys=True) == before
    if format == "docx":
        from docx import Document
        document = Document(io.BytesIO(artifact.content))
        text = "\n".join(p.text for p in document.paragraphs)
        assert document.paragraphs[0].style.name == "Title"
        assert str(document.styles["Title"].font.color.rgb) == "000000"
        assert "pBdr" not in document.styles["Title"].element.xml
        assert "snapshot checksum" in text
    else:
        from pptx import Presentation
        deck = Presentation(io.BytesIO(artifact.content))
        text = "\n".join(shape.text for slide in deck.slides for shape in slide.shapes if shape.has_text_frame)
        assert len(deck.slides) > 1
        assert all(shape.shape_type != 13 for slide in deck.slides for shape in slide.shapes)
    assert "Includes private organization context" in text
    assert "Former fictional owner" in text and "New fictional owner" in text
    assert "café 金融 <untrusted> \\u0001" in text
    assert "Synthetic unresolved reference requires review." in text
    assert "SYN-EVT-000" in text and "approved_synthetic" in text
    with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
        assert not any(name.endswith(("vbaProject.bin", ".exe")) for name in archive.namelist())
        for name in archive.namelist():
            if name.endswith(".rels"):
                assert b'TargetMode="External"' not in archive.read(name)


def test_powerpoint_paginates_long_text_without_dropping_the_end(export_settings):
    from workbench_atlas.exports import render_snapshot
    from pptx import Presentation

    snapshot = build_snapshot(export_settings, scenario_ids=["SYN-SCN-A"])
    snapshot["tables"]["scenarios"][0]["description"] = "Fictional stress condition " * 350 + "FINAL_SENTINEL"
    artifact = render_snapshot(snapshot, format="pptx")
    deck = Presentation(io.BytesIO(artifact.content))
    assert len(deck.slides) > 8
    paragraphs = [p.text for slide in deck.slides for shape in slide.shapes if shape.has_text_frame
                  for p in shape.text_frame.paragraphs]
    assert any("FINAL_SENTINEL" in text for text in paragraphs)
    assert any("continued" in text for text in paragraphs)
    for slide in deck.slides:
        for shape in slide.shapes:
            assert shape.left >= 0 and shape.top >= 0
            assert shape.left + shape.width <= deck.slide_width
            assert shape.top + shape.height <= deck.slide_height


def test_pdf_generic_organization_records_use_readable_nested_fields(export_settings):
    from workbench_atlas.exports import render_snapshot
    from pypdf import PdfReader

    snapshot = build_snapshot(export_settings, scenario_ids=["SYN-SCN-A"])
    snapshot["manifest"]["classification"] = "includes_organization_context"
    snapshot["tables"]["organization_changes"] = [{"record_id": "Fictional service", "change": "changed",
        "before": {"owner": "Former owner"}, "after": {"owner": "Current owner"}}]
    artifact = render_snapshot(snapshot, format="pdf")
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(artifact.content)).pages)
    assert "Before: Owner: Former owner" in text and "After: Owner: Current owner" in text
    assert '{"owner"' not in text


@pytest.fixture
def export_settings(settings):
    from workbench_atlas.compound_join import migrate_compound

    migrate_compound(str(settings.scenario_db_path))
    with sqlite3.connect(settings.scenario_db_path) as conn:
        conn.executemany(
            "INSERT INTO scenarios (scenario_id, origin, title, description, status, evidence_refs, "
            "risk_category, discussion_prompts, affected_capabilities, defensive_options) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("SYN-SCN-A", "synthetic_fixture", "Fictional payments disruption", "A fictional dependency failure.",
                 "active", '["SYN-EVT-000"]', "Digital resilience", '["Which dependency fails first?"]',
                 '["Payment processing"]', '["Exercise the manual fallback"]'),
                ("SYN-SCN-B", "operator_authored", "Retired hypothetical scenario", "Retained for history.",
                 "retired", '["SYN-EVT-001"]', "Digital resilience", "[]", "[]", "[]"),
            ],
        )
        conn.execute(
            "INSERT INTO signpost_assessments (scenario_id, classification, rationale, evidence_refs, "
            "considered_refs, evaluated_at, review_status, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("SYN-SCN-A", "strengthened", "Synthetic review reasoning.", '["SYN-EVT-002"]',
             '["SYN-EVT-002", "SYN-EVT-003"]', "2026-09-08", "pending", "model_assisted"),
        )
        conn.executemany(
            "INSERT INTO compound_scenarios (compound_id, risk_ids, state_hash, status, pair_id) VALUES (?, ?, ?, ?, ?)",
            [("SYN-CMP-A", '["risk-a"]', "a", "candidate", "pair-a"),
             ("SYN-CMP-OLD", '["risk-a"]', "old", "superseded", "pair-old")],
        )
        conn.execute(
            "INSERT INTO score_realizations (realization_id, run_id, pair_id, compound_id, "
            "state_hash, scored_at, realization_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("SYN-SCORE-A", "run-a", "pair-a", "SYN-CMP-A", "a", "2026-09-08", '{"p_hat":0.25,"loss_units":"proxy"}'),
        )
    settings.emerging_risks_path.write_text(json.dumps([
        {"risk_id": "risk-a", "issue": "Fictional cloud dependency", "evidence_refs": ["SYN-EVT-004"],
         "provenance_class": "synthetic", "review_status": "approved_synthetic"},
        {"risk_id": "risk-b", "issue": "Another fictional signal", "evidence_refs": ["SYN-EVT-005"],
         "provenance_class": "synthetic", "review_status": "pending"},
    ]))
    return settings


def test_selected_scenario_preserves_review_history_and_its_evidence(export_settings):
    before = export_settings.scenario_db_path.read_bytes()
    snapshot = build_snapshot(export_settings, scenario_ids=["SYN-SCN-A"])
    tables = snapshot["tables"]
    assert [r["scenario_id"] for r in tables["scenarios"]] == ["SYN-SCN-A"]
    assert {r["event_id"] for r in tables["evidence_events"]} == {"SYN-EVT-000", "SYN-EVT-002", "SYN-EVT-003"}
    assert tables["risks"] == []
    assert tables["signpost_assessments"][0]["review_status"] == "pending"
    assert tables["scenarios"][0]["origin"] == "synthetic_fixture"
    assert all(row["provenance_class"] == "synthetic" for row in tables["evidence_events"])
    assert tables["relationships"]
    assert export_settings.scenario_db_path.read_bytes() == before


def test_compound_selection_includes_risk_evidence_and_score_components(export_settings):
    tables = build_snapshot(export_settings, compound_ids=["SYN-CMP-A"])["tables"]
    assert [row["risk_id"] for row in tables["risks"]] == ["risk-a"]
    assert [row["event_id"] for row in tables["evidence_events"]] == ["SYN-EVT-004"]
    assert json.loads(tables["score_realizations"][0]["realization_json"])["loss_units"] == "proxy"
    assert tables["scenarios"] == []


def test_decision_history_is_selected_with_linked_evidence(export_settings):
    from workbench_atlas.decisions import append_decision, decision_event

    with sqlite3.connect(export_settings.evidence_db_path) as conn:
        for eid in ("SYN-EVT-000", "SYN-EVT-010"):
            before = {"event_id": eid, "review_status": "pending"}
            after = {"event_id": eid, "review_status": "approved_synthetic"}
            event = decision_event(kind="event", record_id=eid, actor="Synthetic reviewer",
                                   action="approve", reason="Fictional test decision", before=before, after=after)
            append_decision(conn, event, before, after)
    tables = build_snapshot(export_settings, scenario_ids=["SYN-SCN-A"])["tables"]
    assert [row["record_id"] for row in tables["review_decisions"]] == ["SYN-EVT-000"]
    assert json.loads(tables["review_decisions"][0]["before_json"])["review_status"] == "pending"


def test_context_requires_explicit_selection_and_enabled_workspace(export_settings):
    with pytest.raises(ExportError, match="explicit non-empty"):
        build_snapshot(export_settings, include_context=True)
    with pytest.raises(ExportError, match="disabled"):
        build_snapshot(export_settings, scenario_ids=["SYN-SCN-A"], include_context=True)
    with pytest.raises(ExportError, match="true or false"):
        build_snapshot(export_settings, scenario_ids=["SYN-SCN-A"], include_context="yes")


def test_private_context_requires_opt_in_and_stays_scoped_to_selected_scenario(export_settings, tmp_path):
    from workbench_atlas.context import import_records

    settings = export_settings.model_copy(update={"context_enabled": True, "context_db_path": tmp_path / "context.db"})
    records = []
    for suffix in ("A", "B"):
        records.extend([
            {"kind": "services", "data": {"id": f"service-{suffix}", "source": "Fictional organization inventory",
                "name": f"Fictional service {suffix}", "owner": "Synthetic owner", "impact_tolerance": "Four hours"}},
            {"kind": "exposures", "data": {"id": f"exposure-{suffix}", "source": "Fictional mapping workshop",
                "scenario_id": f"SYN-SCN-{suffix}", "service_id": f"service-{suffix}", "rationale": "Synthetic linkage"}},
        ])
    import_records(settings, records, actor="Synthetic operator")
    public = build_snapshot(settings, scenario_ids=["SYN-SCN-A"])
    assert not any(name.startswith("organization_") for name in public["tables"])
    private = build_snapshot(settings, scenario_ids=["SYN-SCN-A"], include_context=True)
    assert private["manifest"]["classification"] == "includes_organization_context"
    assert [row["id"] for row in private["tables"]["organization_services"]] == ["service-A"]
    assert [row["id"] for row in private["tables"]["organization_exposures"]] == ["exposure-A"]


def test_private_evidence_assessment_includes_counterevidence_and_its_history(export_settings, tmp_path):
    from workbench_atlas.context import save_record
    from workbench_atlas.decisions import append_decision, decision_event

    settings = export_settings.model_copy(update={"context_enabled": True, "context_db_path": tmp_path / "context.db"})
    for suffix, evidence_id in (("A", "SYN-EVT-010"), ("B", "SYN-EVT-011")):
        save_record(settings, "evidence_assessments", {
            "source": "Fictional counterevidence review", "evidence_id": evidence_id,
            "owner": "Synthetic reviewer", "claim": "A fictional alternative mechanism",
            "rationale": "Synthetic review basis", "relation": "contradicts",
            "evidence_refs": ["synthetic://review-basis"], "scenario_ids": [f"SYN-SCN-{suffix}"],
        }, actor="Synthetic reviewer")
        with sqlite3.connect(settings.evidence_db_path) as conn:
            before = {"event_id": evidence_id, "review_status": "pending"}
            after = {"event_id": evidence_id, "review_status": "approved_synthetic"}
            event = decision_event(kind="event", record_id=evidence_id, actor="Synthetic reviewer",
                                   action="approve", reason="Fictional source review", before=before, after=after)
            append_decision(conn, event, before, after)
    public = build_snapshot(settings, scenario_ids=["SYN-SCN-A"])["tables"]
    assert "SYN-EVT-010" not in {row["event_id"] for row in public["evidence_events"]}
    private = build_snapshot(settings, scenario_ids=["SYN-SCN-A"], include_context=True)["tables"]
    assert "SYN-EVT-010" in {row["event_id"] for row in private["evidence_events"]}
    assert "SYN-EVT-011" not in {row["event_id"] for row in private["evidence_events"]}
    assert [row["record_id"] for row in private["review_decisions"]] == ["SYN-EVT-010"]
    assert any(row["source_type"] == "organization_evidence_assessments" and row["target_id"] == "SYN-EVT-010"
               for row in private["relationships"])


def test_approved_but_stale_framework_mapping_is_explicit(export_settings):
    with sqlite3.connect(export_settings.scenario_db_path) as conn:
        conn.execute("CREATE TABLE framework_mapping_proposals (proposal_id TEXT, scenario_id TEXT, framework TEXT, "
                     "content_version TEXT, status TEXT, scenario_hash TEXT, scenario_revision INTEGER)")
        conn.execute("INSERT INTO framework_mapping_proposals VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("mapping-a", "SYN-SCN-A", "mitre-atlas", "test-release", "approved", "old-hash", 1))
    snapshot = build_snapshot(export_settings, scenario_ids=["SYN-SCN-A"])
    row = snapshot["tables"]["framework_mapping_proposals"][0]
    assert row["status"] == "approved"
    assert row["export_mapping_state"] == "stale"
    assert any("stale" in warning for warning in snapshot["manifest"]["warnings"])
    assert any("pinned release" in warning for warning in snapshot["manifest"]["warnings"])


def test_full_snapshot_excludes_superseded_current_compounds(export_settings):
    tables = build_snapshot(export_settings)["tables"]
    assert len(tables["scenarios"]) == 2
    assert len(tables["evidence_events"]) == 12
    assert [row["compound_id"] for row in tables["compound_scenarios"]] == ["SYN-CMP-A"]
    with pytest.raises(ExportError, match="unknown or unavailable"):
        build_snapshot(export_settings, compound_ids=["SYN-CMP-OLD"])


@pytest.mark.parametrize("selection", [{"scenario_ids": []}, {"risk_ids": []}, {"evidence_ids": []},
                                      {"scenario_ids": ["missing"]}, {"scenario_ids": "SYN-SCN-A"}])
def test_empty_or_invalid_selection_never_widens_to_all(export_settings, selection):
    with pytest.raises(ExportError):
        build_snapshot(export_settings, **selection)


def test_filters_are_recorded_and_do_not_leak_unselected_roots(export_settings):
    snapshot = build_snapshot(export_settings, status="active", origin="synthetic_fixture")
    assert len(snapshot["tables"]["scenarios"]) == 1
    assert snapshot["tables"]["risks"] == []
    assert snapshot["manifest"]["selection"]["origin"] == "synthetic_fixture"
    assert build_snapshot(export_settings, status="nonexistent")["tables"]["scenarios"] == []


def test_missing_optional_table_is_distinct_from_broken_table(export_settings):
    assert "framework_releases" in build_snapshot(export_settings)["manifest"]["missing_optional_tables"]
    with sqlite3.connect(export_settings.scenario_db_path) as conn:
        conn.execute("CREATE TABLE framework_releases (unrelated TEXT)")
    with pytest.raises(ExportError, match="incomplete schema"):
        build_snapshot(export_settings)


@pytest.mark.parametrize("kind", ["missing", "corrupt", "schema"])
def test_required_store_failures_do_not_create_or_repair_files(export_settings, kind):
    path = export_settings.evidence_db_path
    if kind == "missing":
        path.unlink()
    elif kind == "corrupt":
        path.write_bytes(b"not a database")
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TABLE evidence_events")
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(ExportError):
        build_snapshot(export_settings)
    assert (path.read_bytes() if path.exists() else None) == before


def test_bad_register_or_duplicate_identity_fails(export_settings):
    export_settings.emerging_risks_path.write_text('{"not":"a list"}')
    with pytest.raises(ExportError, match="JSON array"):
        build_snapshot(export_settings)
    export_settings.emerging_risks_path.write_text('[{"risk_id":"same"},{"risk_id":"same"}]')
    with pytest.raises(ExportError, match="duplicate"):
        build_snapshot(export_settings)


def test_unresolved_reference_is_retained_with_explicit_warning(export_settings):
    with sqlite3.connect(export_settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET evidence_refs=? WHERE scenario_id=?", ('["missing-ref"]', "SYN-SCN-A"))
    snapshot = build_snapshot(export_settings, scenario_ids=["SYN-SCN-A"])
    assert snapshot["manifest"]["warnings"]
    link = next(row for row in snapshot["tables"]["relationships"] if row["reference"] == "missing-ref")
    assert link["resolution"] == "unresolved"
    assert link["target_id"] is None


def test_json_manifest_hash_and_stable_content_identity(export_settings):
    one = export_data(export_settings, format="json", evidence_ids=["SYN-EVT-000"])
    two = export_data(export_settings, format="json", evidence_ids=["SYN-EVT-000"])
    assert one.snapshot_id == two.snapshot_id
    payload = json.loads(one.content)
    tables_bytes = (json.dumps(payload["tables"], ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    assert payload["manifest"]["tables_sha256"] == hashlib.sha256(tables_bytes).hexdigest()
    assert "source_url" in payload["tables"]["evidence_events"][0]
    assert "url" not in payload["tables"]["evidence_events"][0]


def test_csv_zip_preserves_relationships_and_neutralizes_formula_text(export_settings):
    with sqlite3.connect(export_settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET title=? WHERE scenario_id=?", ('=HYPERLINK("https://example.com")', "SYN-SCN-A"))
    artifact = export_data(export_settings, format="csv", scenario_ids=["SYN-SCN-A"])
    with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read("scenarios.csv").decode("utf-8-sig"))))
        assert rows[0]["title"].startswith("'=HYPERLINK")
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["file_checksums"]["scenarios.csv"] == hashlib.sha256(archive.read("scenarios.csv")).hexdigest()
        assert "relationships.csv" in archive.namelist()
    canonical = json.loads(export_data(export_settings, format="json", scenario_ids=["SYN-SCN-A"]).content)
    assert canonical["tables"]["scenarios"][0]["title"].startswith("=HYPERLINK")


def test_untrusted_register_column_names_are_not_spreadsheet_formulas(export_settings):
    from openpyxl import load_workbook

    risks = json.loads(export_settings.emerging_risks_path.read_text())
    risks[0]["=HYPERLINK(1)"] = "untrusted column label"
    export_settings.emerging_risks_path.write_text(json.dumps(risks))
    xlsx = export_data(export_settings, format="xlsx", risk_ids=["risk-a"])
    sheet = load_workbook(io.BytesIO(xlsx.content))["risks"]
    header = next(cell for cell in sheet[1] if cell.value == "=HYPERLINK(1)")
    assert header.data_type == "s"
    zipped = export_data(export_settings, format="csv", risk_ids=["risk-a"])
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        headers = next(csv.reader(io.StringIO(archive.read("risks.csv").decode("utf-8-sig"))))
        assert "'=HYPERLINK(1)" in headers


@pytest.mark.parametrize("text", ["=1+1", "+SUM(A1)", "-1+2", "@SUM(A1)", "  =1", "\t=1", "＝1", "name,=1"])
def test_xlsx_uses_text_cells_even_for_formula_like_strings(export_settings, text):
    from openpyxl import load_workbook

    with sqlite3.connect(export_settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET title=? WHERE scenario_id=?", (text, "SYN-SCN-A"))
    artifact = export_data(export_settings, format="xlsx", scenario_ids=["SYN-SCN-A"])
    book = load_workbook(io.BytesIO(artifact.content))
    sheet = book["scenarios"]
    column = [cell.value for cell in sheet[1]].index("title") + 1
    assert sheet.cell(2, column).value == text
    assert sheet.cell(2, column).data_type == "s"
    assert sheet.freeze_panes == "A2"
    assert "relationships" in book.sheetnames
    assert not any(cell.data_type == "f" for sheet in book for row in sheet for cell in row)


def test_xlsx_rejects_fields_excel_would_silently_truncate(export_settings):
    with sqlite3.connect(export_settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET description=? WHERE scenario_id=?", ("a" * 32768, "SYN-SCN-A"))
    with pytest.raises(ExportError, match="cannot fit safely"):
        export_data(export_settings, format="xlsx", scenario_ids=["SYN-SCN-A"])


def test_pdf_wraps_long_text_escapes_markup_and_keeps_provenance(export_settings):
    from pypdf import PdfReader

    with sqlite3.connect(export_settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET description=? WHERE scenario_id=?",
                     ("Fictional <b>literal markup</b> & Zoë. " * 120, "SYN-SCN-A"))
    artifact = export_data(export_settings, format="pdf", scenario_ids=["SYN-SCN-A"])
    reader = PdfReader(io.BytesIO(artifact.content))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) >= 3
    for expected in ("Fictional payments disruption", "literal markup", "SYN-EVT-000",
                     "approved_synthetic", "FICTIONAL SYNTHETIC SCENARIO", "rights review", "Page 1"):
        assert expected in text
    assert all("ATLAS WORKBENCH" in page.extract_text() for page in reader.pages)


def test_cli_writes_output_but_never_overwrites(export_settings, tmp_path, monkeypatch):
    monkeypatch.setattr("workbench_atlas.exports.get_settings", lambda: export_settings)
    output = tmp_path / "review.json"
    assert main(["--format", "json", "--scenario-id", "SYN-SCN-A", "--output", str(output)]) == 0
    first = output.read_bytes()
    assert main(["--format", "json", "--output", str(output)]) == 1
    assert output.read_bytes() == first


def test_csv_cli_appends_zip_suffix(export_settings, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("workbench_atlas.exports.get_settings", lambda: export_settings)
    requested = tmp_path / "brief.csv"
    assert main(["--format", "csv", "--scenario-id", "SYN-SCN-A", "--output", str(requested)]) == 0
    written = tmp_path / "brief.csv.zip"
    assert written.is_file()
    assert not requested.exists()
    with zipfile.ZipFile(written) as archive:
        assert "manifest.json" in archive.namelist()
    captured = capsys.readouterr()
    assert "brief.csv.zip" in captured.err

    already = tmp_path / "report.zip"
    assert main(["--format", "csv", "--scenario-id", "SYN-SCN-A", "--output", str(already)]) == 0
    assert already.is_file()
    assert not (tmp_path / "report.zip.zip").exists()
    assert "report.zip.zip" not in capsys.readouterr().err
