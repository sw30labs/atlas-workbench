"""Bulk ingest: inventory pod and value coercion. Synthetic fixtures only."""

import io
import os
import zipfile

import pytest

from bulk_ingest_helpers import write_actions_docx, write_services_xlsx
from workbench_atlas.bulk_ingest import coerce
from workbench_atlas.bulk_ingest.inventory import Limits, build_inventory


def entries(manifest):
    return {e.path: e for e in manifest.entries}


def test_hashes_sniffs_and_reports_every_skip(tmp_path):
    root = tmp_path / "pack"
    write_services_xlsx(root / "a" / "catalogue.xlsx")
    (root / "notes.txt").write_text("plain notes")
    (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (root / ".hidden.csv").write_text("a,b\n1,2\n")
    (root / "~$catalogue.xlsx").write_bytes(b"lock")
    (root / "answer_key.json").write_text('{"secret": true}')
    (root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nrest")
    (root / "empty.csv").write_text("")
    m = entries(build_inventory(root))
    assert m["a/catalogue.xlsx"].status == "accepted" and m["a/catalogue.xlsx"].format == "xlsx"
    assert len(m["a/catalogue.xlsx"].sha256) == 64
    assert m["notes.txt"].format == "txt"
    assert m[".DS_Store"].status == "skipped" and m[".DS_Store"].reason == "system file"
    assert m[".hidden.csv"].reason == "hidden or system entry"
    assert m["~$catalogue.xlsx"].status == "skipped"
    key = m["answer_key.json"]
    assert key.status == "skipped" and "answer key" in key.reason and key.sha256 is None
    assert m["photo.png"].reason.startswith("unsupported")
    assert m["empty.csv"].reason == "empty file"
    assert all(e.reason for e in m.values() if e.status != "accepted")


def test_answer_key_is_never_opened(tmp_path, monkeypatch):
    root = tmp_path / "pack"
    root.mkdir()
    (root / "ANSWER_KEY.json").write_text("{}")
    opened = []
    real_open = os.open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    build_inventory(root)
    assert not any("answer_key" in p.lower() for p in opened)


def test_symlink_escape_is_quarantined_and_internal_links_not_followed(tmp_path):
    root = tmp_path / "pack"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("outside the pack")
    (root / "inside.txt").write_text("inside")
    os.symlink(outside, root / "escape.txt")
    os.symlink(root / "inside.txt", root / "alias.txt")
    os.symlink(tmp_path, root / "escape_dir")
    m = entries(build_inventory(root))
    assert m["escape.txt"].status == "quarantined" and "escapes" in m["escape.txt"].reason
    assert m["escape_dir"].status == "quarantined"
    assert m["alias.txt"].status == "skipped" and m["alias.txt"].reason == "symlink not followed"
    assert m["inside.txt"].status == "accepted"


def test_limits_depth_count_and_size(tmp_path):
    root = tmp_path / "pack"
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("deep")
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"file {i}")
    (root / "big.txt").write_text("x" * 2048)
    limits = Limits(max_depth=2, max_files=2, max_file_bytes=1024)
    m = entries(build_inventory(root, limits))
    assert m["a/b/c/"].reason == "directory deeper than the depth limit"
    assert m["big.txt"].reason.startswith("exceeds")
    accepted = [e for e in m.values() if e.status == "accepted"]
    assert len(accepted) == 2
    assert any(e.reason == "file count limit reached" for e in m.values())


def test_duplicates_and_magic_bytes(tmp_path):
    root = tmp_path / "pack"
    write_actions_docx(root / "report.docx")
    (root / "copy").mkdir()
    (root / "copy" / "report (1).docx").write_bytes((root / "report.docx").read_bytes())
    (root / "misnamed.xlsx").write_bytes((root / "report.docx").read_bytes())
    (root / "fake.xlsx").write_text("Name,Owner\nnot a workbook,x\n")
    (root / "fake.pdf").write_text("not a pdf")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("readme.txt", "zip without workbook")
    (root / "hollow.xlsx").write_bytes(buf.getvalue())
    (root / "truncated.xlsx").write_bytes(b"PK\x03\x04" + b"\x00" * 40)
    (root / "locked.xlsx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
    (root / "data.dat").write_text('[{"a": 1}]')
    m = entries(build_inventory(root))
    dup = m["report.docx"]
    assert dup.status == "duplicate" and dup.duplicate_of == "copy/report (1).docx"
    assert m["misnamed.xlsx"].format == "docx" and m["misnamed.xlsx"].status == "duplicate"
    assert m["fake.xlsx"].status == "quarantined" and "does not match" in m["fake.xlsx"].reason
    assert m["fake.pdf"].status == "quarantined"
    assert m["hollow.xlsx"].status == "quarantined"
    assert m["truncated.xlsx"].status == "quarantined" and "corrupt" in m["truncated.xlsx"].reason
    assert m["locked.xlsx"].status == "quarantined" and "encrypted" in m["locked.xlsx"].reason
    assert m["data.dat"].format == "json"


def test_missing_folder_fails_closed(tmp_path):
    from workbench_atlas.bulk_ingest.inventory import InventoryError

    with pytest.raises(InventoryError):
        build_inventory(tmp_path / "nope")


@pytest.mark.parametrize("raw, dayfirst, expected", [
    ("2025-03-12", None, "2025-03-12"), ("12/03/2025", True, "2025-03-12"), ("03/12/2025", False, "2025-03-12"),
    ("13/03/2025", None, "2025-03-13"), ("12 Mar 2025", None, "2025-03-12"), ("March 12, 2025", None, "2025-03-12"),
    ("12-Mar-25", None, "2025-03-12"), ("2025/03/12", None, "2025-03-12"), ("20250312", None, "2025-03-12"),
])
def test_dates(raw, dayfirst, expected):
    assert coerce.parse_date(raw, dayfirst=dayfirst)[0] == expected


def test_ambiguous_date_is_not_guessed():
    value, note = coerce.parse_date("04/05/2025")
    assert value is None and "ambiguous" in note
    assert coerce.infer_dayfirst(["04/05/2025", "25/05/2025"]) is True


@pytest.mark.parametrize("raw, hint, amount, ccy", [
    ("£1.2m", "", 1_200_000, "GBP"), ("GBP 1,200,000", "", 1_200_000, "GBP"), ("(450k)", "", -450_000, None),
    ("1.234.567,89 EUR", "", 1_234_567.89, "EUR"), ("$3.5bn", "", 3.5e9, "USD"), ("180", "P99 (GBP m)", 180e6, "GBP"),
    ("2.5m", "Loss (GBP m)", 2.5e6, "GBP"), ("1,500", "Amount (£'000)", 1_500_000, "GBP"),
])
def test_money(raw, hint, amount, ccy):
    got, currency = coerce.parse_money(raw, header_hint=hint)
    assert got == pytest.approx(amount) and currency == ccy


def test_durations_criticality_and_frequency():
    assert coerce.tolerance_text("4h", "15 mins") == "RTO 4 hours (240 minutes); RPO 15 minutes"
    assert coerce.tolerance_text("240 mins") == "RTO 4 hours (240 minutes)"
    assert coerce.tolerance_text("2 business days") == "RTO 2 business days"
    assert [coerce.criticality(v) for v in ("Tier 1", "T2", "Tier 3", "gold", "weird")] == \
        ["critical", "high", "standard", "critical", None]
    assert coerce.annual_frequency("1 in 20 years") == pytest.approx(0.05)
    assert coerce.basel_event_type("ET7 Execution") == "ET7"
    assert coerce.basel_event_type("Business disruption and system failures") == "ET6"
    assert coerce.split_list("A; B|C\nD") == ["A", "B", "C", "D"]
