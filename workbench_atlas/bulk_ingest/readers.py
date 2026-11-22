"""Reader pod: normalize one file into a :class:`Document`.

Readers only parse. Formulas are read as cached values, macros, scripts and
attachments are never executed or opened, and a file that cannot be parsed
raises :class:`ReaderError` so the orchestrator can quarantine it with a reason.
"""

from __future__ import annotations

import csv
import email
import hashlib
import io
import json
import re
import zipfile
from datetime import date, datetime, time
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from .coerce import clean
from .models import Block, Column, Document, FileEntry, Row, Table
from .synonyms import header_hits, normalize_header

MAX_TABLE_ROWS = 20_000
MAX_COLUMNS = 200
MAX_BLOCKS = 20_000
HEADER_SCAN_ROWS = 15
_VERSION_RE = re.compile(r"(?i)\bversion[:\s]+v?(\d+(?:\.\d+)*)\b")


class ReaderError(ValueError):
    """The file cannot be parsed; the message is the quarantine reason."""


def read_document(root: Path, entry: FileEntry) -> Document:
    path = root / entry.path
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry.sha256:
        raise ReaderError("changed after inventory (hash mismatch)")
    reader = _READERS.get(entry.format)
    if reader is None:
        raise ReaderError(f"unsupported format {entry.format}")
    doc = Document(path=entry.path, format=entry.format)
    try:
        reader(data, doc)
    except ReaderError:
        raise
    except Exception as exc:  # parsers raise many types on damaged input
        raise ReaderError(f"unreadable {entry.format}: {type(exc).__name__}: {str(exc)[:160]}") from exc
    if "version" not in doc.metadata:
        for block in doc.blocks[:8]:
            if m := _VERSION_RE.search(block.text):
                doc.metadata["version"] = m[1]
                break
    return doc


# --------------------------------------------------------------------------- text


def decode_text(data: bytes) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def _paragraph_blocks(text: str, doc: Document, prefix: str = "line") -> None:
    lines = text.splitlines()
    start, buf = 0, []

    def flush():
        if buf:
            joined = "\n".join(buf).strip()
            if joined:
                heading = re.match(r"^(#{1,6})\s+(.*)$", joined)
                if heading and "\n" not in joined:
                    doc.blocks.append(Block(heading[2].strip(), f"{prefix} {start}", "heading", len(heading[1])))
                else:
                    doc.blocks.append(Block(joined, f"{prefix} {start}"))
        buf.clear()

    for number, line in enumerate(lines, start=1):
        if len(doc.blocks) >= MAX_BLOCKS:
            doc.warnings.append(f"text truncated at {MAX_BLOCKS} blocks")
            break
        if not line.strip() or re.match(r"^#{1,6}\s", line):
            flush()
            if line.strip():
                start = number
                buf.append(line)
                flush()
            continue
        if not buf:
            start = number
        buf.append(line)
    flush()


def _md_tables(text: str, doc: Document) -> str:
    """Lift pipe tables into Table objects; return the remaining text."""
    lines = text.splitlines()
    keep: list[str] = []
    i = 0
    while i < len(lines):
        if (lines[i].strip().startswith("|") and i + 1 < len(lines)
                and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1])):
            start = i
            grid = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\s*\|?\s*:?-{2,}", lines[i]):
                    grid.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            name = f"table {len(doc.tables) + 1}"
            locators = [f"line {start + 1}"] + [f"line {start + 2 + n}" for n in range(1, len(grid))]
            table = build_table(name, grid, row_locators=locators)
            if table:
                doc.tables.append(table)
            keep.extend([""] * (i - start))
            continue
        keep.append(lines[i])
        i += 1
    return "\n".join(keep)


def _read_text(data: bytes, doc: Document) -> None:
    text, encoding = decode_text(data)
    doc.metadata["encoding"] = encoding
    rest = _md_tables(text, doc)
    _paragraph_blocks(rest, doc)
    for block in doc.blocks:
        if block.kind == "heading":
            doc.metadata.setdefault("title", block.text)
            break


def _read_csv(data: bytes, doc: Document) -> None:
    text, encoding = decode_text(data)
    doc.metadata["encoding"] = encoding
    if doc.format == "tsv":
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:16384], delimiters=",;\t|").delimiter
        except csv.Error:
            counts = {d: text[:4096].count(d) for d in ",;\t|"}
            delimiter = max(counts, key=counts.get) if any(counts.values()) else ","
    doc.metadata["delimiter"] = delimiter
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    grid = []
    for row in reader:
        if len(grid) >= MAX_TABLE_ROWS:
            doc.warnings.append(f"rows truncated at {MAX_TABLE_ROWS}")
            break
        grid.append(row[:MAX_COLUMNS])
    table = build_table(Path(doc.path).stem, grid, row_locators=[f"row {n}" for n in range(1, len(grid) + 1)])
    if table is None:
        raise ReaderError("empty or header-only table")
    doc.tables.append(table)


# --------------------------------------------------------------------------- tables


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else "%.15g" % value
    return str(value).replace(" ", " ").strip()


def _texty(value: str) -> bool:
    v = value.strip()
    return bool(v) and not re.fullmatch(r"[-+()£$€%.,\d\s/:]+", v)


def _row_profile(row: list[str]) -> tuple[int, int, int]:
    filled = [v for v in row if v.strip()]
    return len(filled), sum(_texty(v) for v in filled), sum(header_hits(v) > 0 for v in filled)


def detect_header(grid: list[list[str]], merged_rows: set[int] | None = None) -> list[int]:
    """Indices of header rows (one or two), chosen by header-synonym hits."""
    merged_rows = merged_rows or set()
    best, best_score = None, -1.0
    for i, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        filled, texty, hits = _row_profile(row)
        if filled < 2 or texty < max(2, filled // 2):
            continue
        score = hits * 3 + texty - (filled - texty)
        if score > best_score:
            best, best_score = i, score
    if best is None:
        return []
    rows = [best]
    width = max(len(r) for r in grid) if grid else 0

    def group_like(i: int, other: int) -> bool:
        if i < 0 or i >= len(grid):
            return False
        filled, texty, _ = _row_profile(grid[i])
        if filled == 0 or texty < filled:
            return False
        values = {v.strip() for v in grid[i] if v.strip()}
        if (len(values) == 1 and filled >= 3) or any(len(v) > 50 or v.startswith("#") for v in values):
            return False  # a title banner or notice, not a column group
        o_filled = _row_profile(grid[other])[0]
        padded = grid[i] + [""] * (width - len(grid[i]))
        gaps = any(not padded[c].strip() and c < len(grid[other]) and grid[other][c].strip()
                   for c in range(width))
        return (i in merged_rows and filled <= o_filled) or (gaps and filled < o_filled)

    if group_like(best - 1, best):
        rows = [best - 1, best]
    elif best + 1 < len(grid):
        # A sub-header row under a merged or gapped group: every cell is a known header.
        sub_filled, sub_texty, sub_hits = _row_profile(grid[best + 1])
        if sub_filled and sub_texty == sub_filled and sub_hits >= 1 and sub_hits * 2 >= sub_filled \
                and (best in merged_rows or any(not up.strip() and low.strip()
                                                for up, low in zip(grid[best], grid[best + 1]))):
            rows = [best, best + 1]
    return rows


def build_table(name: str, grid: list[list[str]], *, sheet: str | None = None,
                row_locators: list[str] | None = None, merged_rows: set[int] | None = None,
                row_numbers: list[int] | None = None) -> Table | None:
    grid = [[_cell(v) for v in row] for row in grid]
    width = max((len(r) for r in grid), default=0)
    while width and all(len(r) < width or not r[width - 1].strip() for r in grid):
        width -= 1
    grid = [(r + [""] * width)[:width] for r in grid]
    if width == 0 or not any(any(v.strip() for v in r) for r in grid):
        return None
    header = detect_header(grid, merged_rows)
    if not header:
        header_rows, columns = [], [Column(i, f"column {i + 1}") for i in range(width)]
        data_start = 0
    else:
        header_rows = header
        if len(header) == 2:
            group = list(grid[header[0]])
            for c in range(1, width):  # forward-fill a group label across its span
                if not group[c].strip() and group[c - 1].strip() and grid[header[1]][c].strip():
                    group[c] = group[c - 1]
            labels = grid[header[1]]
        else:
            group, labels = [""] * width, grid[header[0]]
        columns = []
        for c in range(width):
            label = clean(labels[c]) or clean(group[c]) or f"column {c + 1}"
            columns.append(Column(c, label, clean(group[c]) if clean(group[c]) != label else ""))
        data_start = header[-1] + 1
    rows = []
    for i in range(data_start, len(grid)):
        values = grid[i]
        if not any(v.strip() for v in values):
            continue
        if header and [normalize_header(v) for v in values] == [normalize_header(c.label) for c in columns]:
            continue  # repeated header row
        number = row_numbers[i] if row_numbers else i + 1
        if sheet is not None:
            locator = f"{sheet}!A{number}"
        else:
            locator = row_locators[i] if row_locators and i < len(row_locators) else f"row {number}"
        rows.append(Row(number, values, locator))
    above = range(header[0] if header else 0)
    title_rows = [clean(" ".join(dict.fromkeys(v for v in grid[i] if v.strip()))) for i in above]
    table = Table(name=name, columns=columns, rows=rows,
                  header_rows=[(row_numbers[i] if row_numbers else i + 1) for i in header_rows],
                  sheet=sheet)
    table.locator_prefix = " | ".join(t for t in title_rows if t)[:300]
    for i in above:
        values = [clean(v) for v in grid[i] if clean(v)]
        if values:
            number = row_numbers[i] if row_numbers else i + 1
            loc = f"{sheet}!A{number}" if sheet is not None else (
                row_locators[i] if row_locators and i < len(row_locators) else f"row {number}")
            table.preamble.append((loc, values))
    return table


# --------------------------------------------------------------------------- xlsx


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
_MERGE = re.compile(rb'<(?:\w+:)?mergeCell\s+ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"')


def _col_index(letters: bytes | str) -> int:
    n = 0
    for ch in letters.decode() if isinstance(letters, bytes) else letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx_merges(data: bytes) -> dict[str, list[tuple[int, int, int, int]]]:
    """Merged ranges per sheet name, read from the sheet XML (read-only mode lacks them)."""
    merges: dict[str, list[tuple[int, int, int, int]]] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target", "") for r in rels.findall("rel:Relationship", _NS)}
        for sheet in workbook.findall("m:sheets/m:sheet", _NS):
            target = targets.get(sheet.get(f"{{{_NS['r']}}}id"), "")
            target = target.lstrip("/")
            part = target if target.startswith("xl/") else f"xl/{target}"
            try:
                xml = archive.read(part)
            except KeyError:
                continue
            merges[sheet.get("name", "")] = [
                (int(r1) - 1, _col_index(c1), int(r2) - 1, _col_index(c2))
                for c1, r1, c2, r2 in _MERGE.findall(xml)
            ]
    return merges


def _read_xlsx(data: bytes, doc: Document) -> None:
    import openpyxl

    merges = _xlsx_merges(data)
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        props = workbook.properties
        if props is not None and props.title:
            doc.metadata["title"] = props.title
        hidden = []
        for ws in workbook.worksheets:
            if ws.sheet_state != "visible":
                hidden.append(ws.title)
            grid: list[list[object]] = []
            for row in ws.iter_rows(min_row=1, min_col=1, values_only=True):
                if len(grid) >= MAX_TABLE_ROWS:
                    doc.warnings.append(f"sheet {ws.title}: rows truncated at {MAX_TABLE_ROWS}")
                    break
                grid.append(list(row[:MAX_COLUMNS]))
            merged_rows: set[int] = set()
            for r1, c1, r2, c2 in merges.get(ws.title, []):
                if r1 >= len(grid):
                    continue
                top = grid[r1][c1] if c1 < len(grid[r1]) else None
                for r in range(r1, min(r2, len(grid) - 1) + 1):
                    grid[r] = list(grid[r]) + [None] * max(0, c2 + 1 - len(grid[r]))
                    for c in range(c1, c2 + 1):
                        grid[r][c] = top
                if c2 > c1:
                    merged_rows.update(range(r1, r2 + 1))
            table = build_table(ws.title, grid, sheet=ws.title, merged_rows=merged_rows)
            if table is not None:
                doc.tables.append(table)
        if hidden:
            doc.metadata["hidden_sheets"] = hidden
            doc.warnings.append("hidden sheets were read: " + ", ".join(hidden))
    finally:
        workbook.close()
    if not doc.tables:
        raise ReaderError("workbook contains no data")


# --------------------------------------------------------------------------- docx / pptx / pdf


def _read_docx(data: bytes, doc: Document) -> None:
    from docx import Document as load_docx
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph

    document = load_docx(io.BytesIO(data))
    core = document.core_properties
    if core.title:
        doc.metadata["title"] = core.title
    if core.subject:
        doc.metadata["subject"] = core.subject
    paragraph_no = table_no = 0
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            paragraph_no += 1
            if not text:
                continue
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            level = None
            if m := re.match(r"(?i)heading\s*(\d)", style):
                level = int(m[1])
            elif style.lower() == "title":
                level = 0
                doc.metadata.setdefault("title", text)
            doc.blocks.append(Block(text, f"paragraph {paragraph_no}",
                                    "heading" if level is not None else "paragraph", level))
        elif tag == "tbl":
            table_no += 1
            grid = [[cell.text for cell in row.cells] for row in DocxTable(child, document).rows]
            name = f"table {table_no}"
            table = build_table(name, grid, row_locators=[f"table {table_no} row {n}" for n in range(1, len(grid) + 1)])
            if table:
                doc.tables.append(table)
        if len(doc.blocks) >= MAX_BLOCKS:
            doc.warnings.append(f"text truncated at {MAX_BLOCKS} blocks")
            break


def _shape_items(shapes):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _shape_items(shape.shapes)
        else:
            yield shape


def _read_pptx(data: bytes, doc: Document) -> None:
    from pptx import Presentation

    deck = Presentation(io.BytesIO(data))
    if deck.core_properties.title:
        doc.metadata["title"] = deck.core_properties.title
    for number, slide in enumerate(deck.slides, start=1):
        table_no = 0
        title_shape = slide.shapes.title
        for shape in _shape_items(slide.shapes):
            if getattr(shape, "has_table", False) and shape.has_table:
                table_no += 1
                grid = [[cell.text for cell in row.cells] for row in shape.table.rows]
                prefix = f"slide {number} table {table_no}"
                table = build_table(prefix, grid, row_locators=[f"{prefix} row {n}" for n in range(1, len(grid) + 1)])
                if table:
                    if title_shape is not None and title_shape.has_text_frame:
                        table.locator_prefix = clean(title_shape.text_frame.text)
                    doc.tables.append(table)
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    is_title = title_shape is not None and shape.shape_id == title_shape.shape_id
                    doc.blocks.append(Block(text, f"slide {number}", "heading" if is_title else "paragraph",
                                            1 if is_title else None))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if notes:
                doc.blocks.append(Block(notes, f"slide {number} notes", "notes"))


_SEGMENT = re.compile(r"\S+(?: \S+)*")


def _segments(line: str) -> list[tuple[int, str]]:
    """Cells of a layout-mode text line: runs of words separated by two or more spaces."""
    return [(m.start(), m.group(0)) for m in _SEGMENT.finditer(line)]


def _join_wrapped(previous: str, more: str) -> str:
    """Join a cell continued on the next line. Narrow columns break words ('Complete' / 'd',
    '2026-05-2' / '0'); those fragments join without a space, everything else with one."""
    if not previous:
        return more
    if (re.fullmatch(r"\d{1,2}", more) and re.search(r"[\d-]$", previous)) or (
            re.fullmatch(r"[a-z]{1,2}", more) and re.search(r"[a-z]$", previous)):
        return previous + more
    return f"{previous} {more}"


def layout_tables(text: str) -> list[tuple[list[str], list[tuple[int, list[str]]]]]:
    """Find column-aligned tables in layout-mode PDF text.

    A header is a line of two or more short cells. Following lines are assigned
    to columns by start position; a line with an empty first column continues
    the header (before any row) or the previous row (wrapped cells). The table
    ends at a line that starts left of the first column, at a single cell that
    runs across a column boundary (body prose, page footers), or after three
    blank lines. Returns ``[(labels, [(line_number, cells)])]``.
    """
    lines = text.splitlines()
    tables, i = [], 0
    while i < len(lines):
        header = _segments(lines[i])
        if len(header) < 2 or any(len(t) > 40 for _, t in header) or all(
                re.fullmatch(r"[\d.,%£$€ -]+", t) for _, t in header):
            i += 1
            continue
        starts = [s for s, _ in header]
        labels = [t for _, t in header]
        rows: list[tuple[int, list[str]]] = []
        blanks, j = 0, i + 1
        while j < len(lines):
            segs = _segments(lines[j])
            if not segs:
                blanks += 1
                if blanks >= 3:
                    break
                j += 1
                continue
            blanks = 0
            if segs[0][0] < starts[0] - 2:
                break
            cells = [""] * len(starts)
            overflow = False
            for start, value in segs:
                col = max(k for k in range(len(starts)) if k == 0 or starts[k] <= start + 2)
                end = start + len(value)
                if col + 1 < len(starts) and end > starts[col + 1] + 2:
                    overflow = True
                cells[col] = _join_wrapped(cells[col], value)
            if overflow and len(segs) == 1:
                break
            filled = sum(bool(c) for c in cells)
            wrapped_id = bool(rows) and bool(re.fullmatch(r"\d{1,2}", cells[0])) and bool(
                re.search(r"[A-Za-z]", rows[-1][1][0]) and re.search(r"[\d-]$", rows[-1][1][0]))
            if cells[0] and not wrapped_id and (rows or filled * 2 >= len(starts)):
                rows.append((j + 1, cells))
            elif not rows:
                labels = [_join_wrapped(a, b) if b else a for a, b in zip(labels, cells)]
            else:
                rows[-1] = (rows[-1][0], [_join_wrapped(a, b) if b else a for a, b in zip(rows[-1][1], cells)])
            j += 1
        if len(rows) >= 2 and sum(any(r[1][k] for r in rows) for k in range(1, len(starts))) >= 1:
            tables.append((labels, rows))
            i = j
        else:
            i += 1
    return tables


def _pdf_layout_tables(reader, doc: Document) -> None:
    """Recover tables from layout-mode text; prose blocks are kept as they are."""
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception as exc:  # layout mode is best effort; plain text was already read
            doc.warnings.append(f"page {number}: layout extraction failed ({type(exc).__name__})")
            continue
        for k, (labels, rows) in enumerate(layout_tables(text), start=1):
            name = f"page {number} table {k}"
            doc.tables.append(Table(
                name=name, columns=[Column(c, clean(label)) for c, label in enumerate(labels)],
                rows=[Row(line, [clean(v) for v in cells], f"{name} row {n}")
                      for n, (line, cells) in enumerate(rows, start=1)],
                header_rows=[1]))


def _read_pdf(data: bytes, doc: Document) -> None:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ReaderError("encrypted: password-protected PDF")
        except ReaderError:
            raise
        except Exception as exc:
            raise ReaderError("encrypted: PDF could not be decrypted") from exc
    meta = reader.metadata
    if meta is not None and meta.title:
        doc.metadata["title"] = str(meta.title)
    doc.metadata["pages"] = len(reader.pages)
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            doc.warnings.append(f"page {number}: text extraction failed ({type(exc).__name__})")
            continue
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(paragraphs) <= 1 and text.count("\n") > 3:
            # No paragraph breaks survived extraction: keep one block per line.
            for k, line in enumerate((ln.strip() for ln in text.splitlines()), start=1):
                if line:
                    doc.blocks.append(Block(line, f"page {number} line {k}"))
            continue
        for k, paragraph in enumerate(paragraphs, start=1):
            doc.blocks.append(Block(paragraph, f"page {number} ¶{k}"))
    _pdf_layout_tables(reader, doc)
    if not doc.blocks:
        doc.warnings.append("no extractable text (scanned PDF needs OCR, which is not available)")


# --------------------------------------------------------------------------- json / eml / html


def _flat(value: object) -> str:
    if isinstance(value, (dict, list)):
        if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
            return "; ".join(_cell(v) for v in value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:2000]
    return _cell(value)


def _flatten(item: dict, prefix: str = "", depth: int = 0) -> dict:
    """Nested objects become dotted columns ('citation.url'); lists stay cells."""
    flat: dict = {}
    for key, value in item.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and depth < 2:
            flat.update(_flatten(value, f"{name}.", depth + 1))
        else:
            flat[name] = value
    return flat


def _records_table(name: str, items: list[dict], locator: str) -> Table | None:
    items = [_flatten(item) for item in items]
    keys: list[str] = []
    for item in items:
        for key in item:
            if key not in keys:
                keys.append(key)
    grid = [keys[:MAX_COLUMNS]] + [[_flat(item.get(k)) for k in keys[:MAX_COLUMNS]] for item in items[:MAX_TABLE_ROWS]]
    return build_table(name, grid, row_locators=[f"{locator}"] + [f"{locator}[{i}]" for i in range(len(items))])


def _read_json(data: bytes, doc: Document) -> None:
    text, _ = decode_text(data)

    def reject(value: str):
        raise ReaderError(f"JSON constant {value} is not supported")

    try:
        value = json.loads(text, parse_constant=reject)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ReaderError(f"corrupt: invalid JSON ({type(exc).__name__})") from exc
    _json_value(value, "$", doc, depth=0)


def _json_value(value: object, locator: str, doc: Document, depth: int) -> None:
    if depth > 4:
        doc.blocks.append(Block(_flat(value), locator))
        return
    if isinstance(value, list):
        dicts = [v for v in value if isinstance(v, dict)]
        if dicts and len(dicts) == len(value):
            table = _records_table(locator.lstrip("$.") or "items", dicts, locator)
            if table:
                doc.tables.append(table)
        else:
            for i, item in enumerate(value[:MAX_BLOCKS]):
                _json_value(item, f"{locator}[{i}]", doc, depth + 1)
    elif isinstance(value, dict):
        scalars = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                _json_value(item, f"{locator}.{key}", doc, depth + 1)
            else:
                scalars.append(f"{key}: {_cell(item)}")
                if str(key).lower() in ("title", "name") and isinstance(item, str):
                    doc.metadata.setdefault("title", item)
                if str(key).lower() == "version" and isinstance(item, (str, int, float)):
                    doc.metadata.setdefault("version", str(item))
        if scalars:
            doc.blocks.append(Block("\n".join(scalars), locator, "fields"))
    elif value not in (None, ""):
        doc.blocks.append(Block(_cell(value), locator))


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "section", "article",
             "blockquote", "pre", "td", "th", "table", "ul", "ol", "header", "footer"}
    SKIP = {"script", "style", "noscript", "template", "head", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str, int | None]] = []
        self.tables: list[list[list[str]]] = []
        self._buf: list[str] = []
        self._skip = 0
        self._heading: int | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "table":
            self._flush()
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in self.BLOCK:
            self._flush()
            if re.fullmatch(r"h[1-6]", tag):
                self._heading = int(tag[1])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self.tables:
            self.tables[-1].append(self._row)
            self._row = None
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._row is None:
            self._buf.append(data)

    def _flush(self):
        text = clean("".join(self._buf))
        if text:
            self.blocks.append(("heading" if self._heading else "paragraph", text, self._heading))
        self._buf, self._heading = [], None

    def close(self):
        super().close()
        self._flush()


def html_to_doc(text: str, doc: Document, prefix: str = "element") -> None:
    parser = _HTMLText()
    parser.feed(text)
    parser.close()
    for n, (kind, block, level) in enumerate(parser.blocks[:MAX_BLOCKS], start=1):
        doc.blocks.append(Block(block, f"{prefix} {n}", kind, level))
    for t, grid in enumerate(parser.tables, start=1):
        name = f"html table {t}"
        table = build_table(name, grid, row_locators=[f"{name} row {n}" for n in range(1, len(grid) + 1)])
        if table:
            doc.tables.append(table)


def _read_html(data: bytes, doc: Document) -> None:
    text, _ = decode_text(data)
    if m := re.search(r"(?is)<title[^>]*>(.*?)</title>", text):
        doc.metadata["title"] = clean(re.sub(r"<[^>]+>", "", m[1]))
    html_to_doc(text, doc)


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower().strip("> ") if "@" in address else ""


def _read_eml(data: bytes, doc: Document) -> None:
    message = email.message_from_bytes(data, policy=policy.default)
    if not message.keys():
        raise ReaderError("corrupt: no email headers")
    subject = clean(message.get("subject", ""))
    doc.metadata.update({
        "title": subject, "subject": subject, "date": clean(message.get("date", "")),
        # Addresses are personal data; keep only sender/recipient domains.
        "from_domain": _domain(str(message.get("from", ""))),
        "recipient_domains": sorted({_domain(a) for a in str(message.get("to", "")).split(",") if _domain(a)}),
    })
    if subject:
        doc.blocks.append(Block(subject, "subject", "heading", 1))
    attachments, part_no, bodies = [], 0, 0
    for part in message.walk():
        if part.is_multipart():
            continue
        part_no += 1
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        if filename or disposition == "attachment":
            payload = part.get_payload(decode=True) or b""
            attachments.append({"filename": clean(filename or "(unnamed)"),
                                "content_type": part.get_content_type(), "size": len(payload)})
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        if ctype == "text/html" and bodies:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        bodies += 1
        if ctype == "text/html":
            html_to_doc(content, doc, prefix=f"body {part_no} element")
        else:
            _paragraph_blocks(content, doc, prefix=f"body {part_no} line")
    if attachments:
        doc.metadata["attachments"] = attachments
        doc.warnings.append(f"{len(attachments)} attachment(s) listed but not opened")


_READERS = {
    "csv": _read_csv, "tsv": _read_csv, "xlsx": _read_xlsx, "docx": _read_docx, "pptx": _read_pptx,
    "pdf": _read_pdf, "md": _read_text, "txt": _read_text, "json": _read_json, "eml": _read_eml,
    "html": _read_html,
}
SUPPORTED_FORMATS = tuple(sorted(_READERS))
