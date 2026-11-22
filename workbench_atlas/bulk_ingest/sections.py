"""Heading-sectioned key/value documents.

Statements, registers exported to Word and audit reports often repeat one
pattern: a heading per item ("IBS1001002 - Card Authorisation Platform")
followed by "Key: value" lines. Keys are mapped with the same exact header
synonyms as tables; partial matches are not used here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extractors import POD, assemble
from .models import Candidate, Document, DocumentResult, Provenance, Route
from .synonyms import ANCHORS, FIELD_SYNONYMS, field_score

MIN_SECTIONS = 2
_KEY = re.compile(r"(?:^|(?<=[.;]\s)|(?<=\n))\s*([A-Z][A-Za-z /&()'-]{1,40}?)\s*:\s+")
_REF_TITLE = re.compile(r"^\s*([A-Z][A-Z0-9]*[-_.]?[A-Z0-9-]*\d[A-Z0-9.-]*)\s*[-–—:|]\s*(.+?)\s*$")
_TITLE_FIELD = {"services": "name", "controls": "name", "mechanisms": "name", "dependencies": "name",
                "actions": "title", "exercises": "title", "observations": "title", "assumptions": "statement",
                "org_scenarios": "title", "internal_loss_events": "title", "impact_assessments": "title"}


@dataclass
class Section:
    title: str
    locator: str
    pairs: list[tuple[str, str, str]] = field(default_factory=list)  # key, value, locator


def key_values(text: str) -> list[tuple[str, str]]:
    matches = list(_KEY.finditer(text))
    pairs = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[m.end():end].strip(" .;\n")
        if value:
            pairs.append((m[1].strip(), value))
    return pairs


def sections(doc: Document, excluded: set[str]) -> list[Section]:
    out: list[Section] = []
    current: Section | None = None
    for block in doc.blocks:
        if block.locator in excluded:
            continue
        if block.kind == "heading":
            current = Section(block.text, block.locator)
            out.append(current)
            continue
        if current is not None:
            current.pairs.extend((k, v, block.locator) for k, v in key_values(block.text))
    return [s for s in out if s.pairs]


def _map(route: str, section: Section) -> tuple[dict[str, str], dict[str, str]]:
    raw, cells = {}, {}
    title_field = _TITLE_FIELD.get(route)
    if title_field:
        m = _REF_TITLE.match(section.title)
        if m and "org_ref" in FIELD_SYNONYMS[route]:
            raw["org_ref"], raw[title_field] = m[1], m[2]
        else:
            raw[title_field] = section.title
        cells[title_field] = section.locator
    for key, value, loc in section.pairs:
        best = max(FIELD_SYNONYMS[route], key=lambda f: field_score(route, f, key))
        if field_score(route, best, key) >= 2 and best not in raw:
            raw[best], cells[best] = value, loc
    return raw, cells


def _anchored(route: str, raw: dict[str, str]) -> bool:
    return all(any(f in raw for f in group) for group in ANCHORS[route])


def section_routes(result: DocumentResult) -> list[Route]:
    found = sections(result.document, result.excluded_blocks)
    routes = []
    for route, title_field in _TITLE_FIELD.items():
        hits = sum(1 for s in found if _anchored(route, _map(route, s)[0]) and len(s.pairs) >= 2)
        if hits >= MIN_SECTIONS:
            routes.append(Route(route, 3.0 + hits, [f"{hits} headed sections with {route} key/value lines"],
                                "sections"))
    return routes


def extract_sections(result: DocumentResult, route: Route) -> list[Candidate]:
    out = []
    for section in sections(result.document, result.excluded_blocks):
        raw, cells = _map(route.route, section)
        if not _anchored(route.route, raw):
            continue
        for data, meta, notes in assemble(route.route, raw):
            out.append(Candidate(route.route, data, [Provenance(
                result.entry.path, section.locator, f"{POD}:{route.route}:sections", "deterministic",
                spans=dict(raw), cells=cells)], meta=meta, warnings=notes))
    return out
