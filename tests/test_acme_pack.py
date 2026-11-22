"""Offline checks for the synthetic ACME Bank information pack and its answer key."""

from __future__ import annotations

import email
import email.policy
import json
import re
from functools import cache, lru_cache
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "examples" / "acme-bank"
PACK = BASE / "pack"
KEY_PATH = BASE / "answer_key.json"
README = BASE / "README.md"

# Files deliberately present in pack/ but not listed in the answer key (none today).
EXTRAS: set[str] = set()
MAX_FILE_BYTES = 2_000_000  # per-file limit of scripts/public_release_gate.py
MAX_PACK_BYTES = 6_000_000

ROUTES = {"services", "dependencies", "mechanisms", "controls", "assumptions", "actions", "exercises",
          "observations", "impact_assessments", "evidence_events", "internal_loss_events", "org_scenarios",
          "taxonomy_crosswalk", "ignore", "quarantine"}
FLAGS = {"pii", "injection", "duplicate", "superseded", "unreadable", "out_of_scope", "conflict"}
FILE_KEYS = {"path", "format", "routes", "expected_counts", "flags", "duplicate_of", "notes"}
TOP_KEYS = {"pack", "version", "synthetic", "generated_by", "files", "entities", "conflicts", "injections"}

SECRET_PATTERNS = {
    "aws access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "openai/anthropic-style key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b"),
    "slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "google api key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "password assignment": re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key)\s*[:=]\s*\S{6,}"),
    "credential in url": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
}
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def key() -> dict:
    return json.loads(KEY_PATH.read_text(encoding="utf-8"))


def pack_files() -> set[str]:
    return {p.relative_to(PACK).as_posix() for p in PACK.rglob("*") if p.is_file()}


def entries() -> dict[str, dict]:
    return {f["path"]: f for f in key()["files"]}


def unreadable() -> set[str]:
    return {p for p, f in entries().items() if "unreadable" in f["flags"]}


@cache
def extract_text(rel: str) -> str:
    """Return the human-visible text of a pack file using the base-install readers."""
    path = PACK / rel
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts = [str(wb.properties.description or "")]
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                parts.extend(str(v) for v in row if v is not None)
        wb.close()
        return "\n".join(parts)
    if suffix == ".docx":
        import docx

        doc = docx.Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for section in doc.sections:
            parts += [p.text for p in section.header.paragraphs] + [p.text for p in section.footer.paragraphs]
        for table in doc.tables:
            parts += [cell.text for row in table.rows for cell in row.cells]
        return "\n".join(parts)
    if suffix == ".pptx":
        import pptx

        parts = []
        for slide in pptx.Presentation(str(path)).slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    parts.append(shape.text_frame.text)
                if getattr(shape, "has_table", False) and shape.has_table:
                    parts += [cell.text for row in shape.table.rows for cell in row.cells]
        return "\n".join(parts)
    if suffix == ".pdf":
        import pypdf

        return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(str(path)).pages)
    if suffix == ".eml":
        msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        headers = "\n".join(f"{k}: {v}" for k, v in msg.items())
        return headers + "\n" + msg.get_content()
    return path.read_text(encoding="utf-8-sig")


def readable_files() -> list[str]:
    skip = unreadable() | {p for p, f in entries().items() if f["format"] == "unknown"}
    return sorted(pack_files() - skip)


# ---------------------------------------------------------------------------


def test_answer_key_format():
    k = key()
    assert set(k) == TOP_KEYS
    assert k["pack"] == "acme-bank" and k["version"] == 1 and k["synthetic"] is True
    assert k["generated_by"] == "scripts/build_acme_pack.py"
    paths = [f["path"] for f in k["files"]]
    assert len(paths) == len(set(paths)), "duplicate answer-key paths"
    for f in k["files"]:
        assert set(f) == FILE_KEYS, f["path"]
        assert not f["path"].startswith("/") and ".." not in Path(f["path"]).parts
        assert isinstance(f["format"], str) and f["format"]
        assert f["routes"] and set(f["routes"]) <= ROUTES, f["path"]
        assert set(f["flags"]) <= FLAGS, f["path"]
        assert set(f["expected_counts"]) <= set(f["routes"]), f["path"]
        assert all(isinstance(v, int) and v >= 0 for v in f["expected_counts"].values()), f["path"]
        assert isinstance(f["notes"], str)
        if "duplicate" in f["flags"]:
            assert f["duplicate_of"] in paths, f["path"]
            assert (PACK / f["path"]).read_bytes() == (PACK / f["duplicate_of"]).read_bytes()
        else:
            assert f["duplicate_of"] is None, f["path"]
        if {"ignore", "quarantine"} & set(f["routes"]):
            assert len(f["routes"]) == 1, f"{f['path']}: ignore/quarantine must be exclusive"
    for group in ("services", "vendors", "scenarios"):
        items = k["entities"][group]
        assert items, group
        canon = [e["canonical"] for e in items]
        assert len(canon) == len(set(canon)), group
        for e in items:
            assert set(e) == {"canonical", "aliases"}
            assert isinstance(e["canonical"], str) and e["canonical"]
            assert all(isinstance(a, str) and a for a in e["aliases"])
    for c in k["conflicts"]:
        assert set(c) == {"what", "files", "values"}
        assert len(c["files"]) >= 2 and len(c["values"]) >= 2
        assert set(c["files"]) <= set(paths)
        for path in c["files"]:
            assert "conflict" in entries()[path]["flags"], path
    assert k["injections"]
    for inj in k["injections"]:
        assert set(inj) == {"path", "excerpt"}
        assert "injection" in entries()[inj["path"]]["flags"]
        assert inj["excerpt"] in extract_text(inj["path"])


def test_answer_key_and_pack_files_match():
    listed = set(entries())
    present = pack_files()
    assert listed - present == set(), "listed but missing"
    assert present - listed == EXTRAS, "present but not listed"
    assert not (BASE / "pack" / "answer_key.json").exists(), "answer key must live outside pack/"


def test_sizes_within_limits():
    sizes = {p: (PACK / p).stat().st_size for p in pack_files()}
    too_big = {p: s for p, s in sizes.items() if s > MAX_FILE_BYTES}
    assert not too_big
    assert sum(sizes.values()) < MAX_PACK_BYTES


def test_every_file_declared_synthetic():
    readme = README.read_text(encoding="utf-8")
    assert "SYNTHETIC" in readme and "never be mistaken" in readme
    assert key()["synthetic"] is True
    for path in pack_files():
        assert f"`{path}`" in readme, f"README does not list {path}"
    for path in readable_files():
        assert "SYNTHETIC" in extract_text(path), f"{path} lacks a visible SYNTHETIC marker"
    for path, f in entries().items():
        if f["format"] == "unknown":
            assert b"SYNTHETIC" in (PACK / path).read_bytes()


def test_unreadable_files_are_really_unreadable():
    import zipfile

    for path in unreadable():
        with pytest.raises(zipfile.BadZipFile):
            zipfile.ZipFile(PACK / path).testzip()


def _public_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return (parsed.scheme in ("http", "https") and "." in host and not parsed.username
            and not re.match(r"^(localhost|127\.|10\.|192\.168\.)", host))


def test_external_watchlist_exports_only_approved_public_records():
    import openpyxl

    wb = openpyxl.load_workbook(PACK / "04_external_loss_watchlist" / "external_loss_watchlist_2026Q3.xlsx", read_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    header = list(rows[2])
    url_i, status_i, id_i = header.index("Source URL"), header.index("Source review status"), header.index("Atlas Event ID")
    data = [r for r in rows[3:] if any(v is not None for v in r)]
    assert data
    for r in data:
        assert _public_url(r[url_i]), r[id_i]
        assert r[status_i] == "approved", r[id_i]
        assert r[header.index("Summary (independently authored)")]
    feed = json.loads((PACK / "04_external_loss_watchlist" / "watchlist_feed_export.json").read_text(encoding="utf-8"))
    assert feed["items"] and feed["count"] == len(feed["items"])
    for item in feed["items"]:
        assert _public_url(item["citation"]["url"]), item["id"]
        assert item["review"]["status"] == "approved", item["id"]
    xlsx_ids = {r[id_i] for r in data}
    json_ids = {i["ref"]["atlas_event_id"] for i in feed["items"]}
    counts = {f["path"]: f["expected_counts"] for f in key()["files"]}
    assert len(xlsx_ids) == counts["04_external_loss_watchlist/external_loss_watchlist_2026Q3.xlsx"]["evidence_events"]
    assert len(json_ids) == counts["04_external_loss_watchlist/watchlist_feed_export.json"]["evidence_events"]
    assert xlsx_ids & json_ids and json_ids - xlsx_ids, "feed must partially overlap the xlsx"


def test_no_secrets_private_terms_or_real_emails():
    from workbench_atlas.independence_lexicon import (
        DISCLOSURE_ONLY,
        STRING_FORBIDDEN,
        scan_identifier_residue,
    )

    texts = {p: extract_text(p) for p in readable_files()}
    for p in pack_files() - set(readable_files()):
        texts[p] = (PACK / p).read_bytes().decode("latin-1")
    texts["../answer_key.json"] = KEY_PATH.read_text(encoding="utf-8")
    texts["../README.md"] = README.read_text(encoding="utf-8")
    for path, text in texts.items():
        for label, pattern in SECRET_PATTERNS.items():
            assert not pattern.search(text), f"{path}: {label}"
        for label, pattern in {**STRING_FORBIDDEN, **DISCLOSURE_ONLY}.items():
            assert not pattern.search(text), f"{path}: {label}"
        assert not any(scan_identifier_residue(line) for line in text.splitlines()), f"{path}: legacy identifier"
        for match in EMAIL.finditer(text):
            assert match.group(1).lower().endswith(".example"), f"{path}: non-reserved email domain {match.group(0)}"


def test_expected_counts_match_tabular_sources():
    import csv

    counts = {f["path"]: f["expected_counts"] for f in key()["files"]}
    lines = (PACK / "03_internal_loss_data" / "oprisk_loss_extract_2019_2026.csv").read_text(encoding="utf-8").splitlines()
    rows = list(csv.reader(lines[4:]))
    assert len(rows) == counts["03_internal_loss_data/oprisk_loss_extract_2019_2026.csv"]["internal_loss_events"]
    assert 300 <= len(rows) <= 600
    dates = [r[8] for r in rows]
    assert all(dates), "every loss has an occurrence date"
    tpr = (PACK / "01_operational_resilience" / "third_party_register.csv").read_text(encoding="utf-8-sig").splitlines()
    vendors = list(csv.reader(tpr[1:], delimiter=";"))[1:]
    links = sum(len([s for s in r[7].split(" | ") if s]) for r in vendors)
    assert links == counts["01_operational_resilience/third_party_register.csv"]["dependencies"]
    assert 40 <= len(vendors) <= 60
