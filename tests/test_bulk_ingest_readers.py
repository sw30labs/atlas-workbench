"""Bulk ingest: reader pod normalizes each supported format. Synthetic fixtures only."""

import json

import pytest

from bulk_ingest_helpers import (
    write_actions_docx, write_board_pptx, write_eml, write_pdf, write_services_xlsx,
)
from workbench_atlas.bulk_ingest.inventory import build_inventory
from workbench_atlas.bulk_ingest.readers import ReaderError, read_document


def read(root, name):
    manifest = build_inventory(root)
    entry = next(e for e in manifest.entries if e.path == name)
    assert entry.status == "accepted", entry.reason
    return read_document(manifest.root, entry)


def test_csv_semicolon_cp1252(tmp_path):
    (tmp_path / "v.csv").write_bytes("Vendor;Service Provided;Notes\nCafé Systems;Hosting;£5k fee\n".encode("cp1252"))
    doc = read(tmp_path, "v.csv")
    table = doc.tables[0]
    assert doc.metadata["delimiter"] == ";" and doc.metadata["encoding"] == "cp1252"
    assert [c.label for c in table.columns] == ["Vendor", "Service Provided", "Notes"]
    assert table.rows[0].values[0] == "Café Systems" and table.rows[0].locator == "row 2"


def test_csv_utf8_bom_and_tsv(tmp_path):
    (tmp_path / "a.csv").write_bytes("﻿Name,Owner\nX,Y\n".encode("utf-8"))
    (tmp_path / "b.tsv").write_text("Name\tOwner\nX\tY\n")
    assert read(tmp_path, "a.csv").tables[0].columns[0].label == "Name"
    assert read(tmp_path, "b.tsv").tables[0].rows[0].values == ["X", "Y"]


def test_xlsx_merged_multirow_header_and_cell_locators(tmp_path):
    write_services_xlsx(tmp_path / "s.xlsx")
    doc = read(tmp_path, "s.xlsx")
    table = doc.tables[0]
    assert table.header_rows == [3, 4]
    headers = [c.header for c in table.columns]
    assert headers[:4] == ["Service ID", "Business Service", "Service Owner", "Criticality"]
    assert headers[4:] == ["Recovery objectives / RTO", "Recovery objectives / RPO"]
    first = table.rows[0]
    assert first.locator == "Services!A5" and first.values[1] == "Card Authorisation Platform (UK)"
    assert table.cell_locator(first, 4) == "Services!E5"
    assert "service catalogue" in table.locator_prefix.lower()


def test_docx_headings_paragraphs_and_tables(tmp_path):
    from docx import Document

    path = write_actions_docx(tmp_path / "a.docx")
    doc_obj = Document(path)
    table = doc_obj.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Control", "Owner"
    table.cell(1, 0).text, table.cell(1, 1).text = "Failover check", "Ops"
    doc_obj.save(path)
    doc = read(tmp_path, "a.docx")
    assert doc.blocks[0].kind == "heading" and doc.blocks[0].level == 1
    assert doc.blocks[1].locator.startswith("paragraph ")
    assert doc.tables[0].rows[0].locator == "table 1 row 2"


def test_pptx_slide_text_and_tables(tmp_path):
    write_board_pptx(tmp_path / "b.pptx")
    doc = read(tmp_path, "b.pptx")
    assert any(b.locator == "slide 1" and "Board risk report" in b.text for b in doc.blocks)
    assert doc.tables[0].rows[0].locator == "slide 1 table 1 row 2"
    assert doc.tables[0].locator_prefix.startswith("Board risk report")


def test_pdf_text_per_page(tmp_path):
    write_pdf(tmp_path / "r.pdf", ["Post-incident review (synthetic)", "Root cause: expired certificate"])
    doc = read(tmp_path, "r.pdf")
    assert doc.metadata["pages"] == 1
    assert doc.blocks and doc.blocks[0].locator.startswith("page 1")
    assert "expired certificate" in " ".join(b.text for b in doc.blocks)


def test_corrupt_pdf_is_an_error_or_textless(tmp_path):
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.7\n garbage without objects")
    manifest = build_inventory(tmp_path)
    try:
        doc = read_document(manifest.root, manifest.entries[0])
    except ReaderError:
        return
    assert not doc.blocks and not doc.tables  # triage then flags it unreadable


def test_markdown_headings_and_pipe_tables(tmp_path):
    (tmp_path / "m.md").write_text("# Title here\n\nIntro text.\n\n| Action | Owner | Due |\n|---|---|---|\n"
                                   "| Fix it | Ops | 2026-01-31 |\n")
    doc = read(tmp_path, "m.md")
    assert doc.metadata["title"] == "Title here"
    assert doc.tables[0].rows[0].values == ["Fix it", "Ops", "2026-01-31"]
    assert doc.tables[0].rows[0].locator == "line 7"


def test_json_arrays_and_objects(tmp_path):
    (tmp_path / "t.json").write_text(json.dumps({"title": "Taxonomy", "version": "2.1",
                                                 "nodes": [{"code": "R1", "name": "A", "tags": ["x", "y"]}]}))
    doc = read(tmp_path, "t.json")
    assert doc.metadata == {"title": "Taxonomy", "version": "2.1"}
    table = doc.tables[0]
    assert [c.label for c in table.columns] == ["code", "name", "tags"]
    assert table.rows[0].values[2] == "x; y" and table.rows[0].locator == "$.nodes[0]"


def test_invalid_json_is_reader_error(tmp_path):
    (tmp_path / "x.json").write_text('{"a": NaN}')
    manifest = build_inventory(tmp_path)
    with pytest.raises(ReaderError):
        read_document(manifest.root, manifest.entries[0])


def test_eml_keeps_domains_only_and_lists_attachments(tmp_path):
    write_eml(tmp_path / "m.eml", "Hello team,\n\nThe outage lasted two hours.\n", attachment=b"MZ binary")
    doc = read(tmp_path, "m.eml")
    assert doc.metadata["from_domain"] == "vendor.example"
    assert doc.metadata["attachments"] == [{"filename": "tool.exe", "content_type": "application/octet-stream",
                                            "size": 9}]
    assert "desk@vendor.example" not in json.dumps(doc.summary())
    assert any("two hours" in b.text for b in doc.blocks)


def test_html_text_without_scripts(tmp_path):
    (tmp_path / "p.html").write_text("<html><head><title>T</title><script>alert(1)</script></head><body>"
                                     "<h1>Heading</h1><p>Body text</p><table><tr><th>Vendor</th><th>Type</th></tr>"
                                     "<tr><td>Acme</td><td>Hosting</td></tr></table></body></html>")
    doc = read(tmp_path, "p.html")
    texts = [b.text for b in doc.blocks]
    assert "Heading" in texts and "Body text" in texts and not any("alert" in t for t in texts)
    assert doc.tables[0].rows[0].values == ["Acme", "Hosting"]
