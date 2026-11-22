"""Triage pod: route each document to contracts and raise safety flags.

Deterministic rules first (header synonyms, filename and title cues, value
patterns). An optional LLM tiebreak may only pick among the routes the rules
already proposed. Instruction-like text inside a file is flagged and excluded
from extraction; it is never obeyed.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from .coerce import basel_event_type, parse_duration_minutes
from .models import CONTRACT_ROUTES, DocumentResult, Route, Table
from .synonyms import ANCHORS, FIELD_SYNONYMS, STRONG_FIELDS, is_personal_header, map_columns, normalize_header

TABLE_MIN_SCORE = 3
TEXT_MIN_SCORE = 3
LOW_CONFIDENCE_MARGIN = 2

_INJECTION = [re.compile(p, re.I) for p in (
    r"\bignore\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:instructions?|prompts?|rules|directions|guidance)",
    r"\bdisregard\s+(?:all\s+|any\s+|the\s+|your\s+)*(?:previous\s+|prior\s+|above\s+)?"
    r"(?:instructions?|rules|guidelines|prompts?)",
    r"\bforget\s+(?:all\s+|your\s+|the\s+)*(?:previous\s+|prior\s+)?instructions",
    r"\byou\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|a\.i\.|assistant|language\s+model|llm|chatbot|"
    r"large\s+language\s+model)\b",
    r"\b(?:mark|set|flag)\s+(?:all|every|each|these|this|the|any)\b[^.\n]{0,60}?\b(?:as\s+)?"
    r"(?:approved|validated|verified|passed|effective|compliant)\b",
    r"\bmark(?:ed)?\s+(?:it\s+|them\s+)?as\s+(?:pre-?)?(?:approved|validated|verified)\b",
    r"\bauto[-\s]?approv",
    r"\b(?:approve|accept)\s+(?:all|every|this|these)\b[^.\n]{0,40}\b(?:records?|items?|controls?|"
    r"findings?|entries|changes|rows)\b",
    r"\bnote\s+to\s+(?:the\s+|any\s+)?(?:ai|assistant|model|llm|bot|automated|ingestion)",
    r"\b(?:instructions?|message)\s+(?:for|to)\s+(?:the\s+|any\s+)?(?:ai|assistant|model|llm|"
    r"automated\s+\w+|ingestion\s+\w+)\b",
    r"\b(?:ai|assistant|llm|model|agent)s?\s+(?:reading|processing|ingesting|parsing)\s+this\b",
    r"\bsystem\s+prompt\b",
    r"\bdo\s+not\s+(?:flag|report|mention|escalate|show)\b[^.\n]{0,40}\b(?:reviewer|human|this|it)\b",
    r"\b(?:bypass|skip|override)\s+(?:the\s+)?(?:human\s+)?(?:review|approval|gate|validation|checks?)\b",
    r"<\|?\s*(?:im_start|system|assistant)\s*\|?>",
    r"\[\s*(?:system|assistant|inst)\s*\]",
)]

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,5}\)[\s.-]?)?\d{2,5}(?:[\s.-]\d{2,5}){1,4}(?![\w.])")
_IDENT_RE = re.compile(r"\b(?:[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]|\d{3}-\d{2}-\d{4})\b")
PII_DOC_THRESHOLD = 3

CUES: dict[str, tuple[str, ...]] = {
    "services": ("service catalogue", "service catalog", "business services", "important business services",
                 "ibs", "bia", "business impact analysis", "service inventory", "impact tolerances"),
    "dependencies": ("vendor", "vendors", "supplier", "suppliers", "third party", "third parties",
                     "outsourcing", "dependency", "dependencies", "tprm"),
    "mechanisms": ("mechanism", "mechanisms", "failure mode", "failure modes"),
    "controls": ("control", "controls", "rcsa", "control library", "control testing"),
    "assumptions": ("assumption", "assumptions"),
    "actions": ("action", "actions", "remediation", "audit finding", "audit findings", "findings",
                "issues log", "action tracker", "action log"),
    "exercises": ("exercise", "exercises", "tabletop", "simulation", "dr test", "resilience test"),
    "observations": ("post incident", "post-incident", "post mortem", "postmortem", "incident review",
                     "pir", "lessons learned", "major incident"),
    "impact_assessments": ("impact assessment", "impact assessments"),
    "evidence_events": ("external loss", "public loss", "loss watchlist", "external events", "industry events",
                        "external watchlist"),
    "internal_loss_events": ("internal loss", "loss data", "loss event", "loss events", "loss register",
                             "operational loss", "op loss", "loss log"),
    "org_scenarios": ("scenario register", "scenario", "scenarios", "scenario analysis"),
    "taxonomy_crosswalk": ("taxonomy", "risk library", "risk categories", "risk taxonomy"),
}

_ACTION_LINE = re.compile(
    r"(?im)^\s*(?:[-*•]\s*)?(?:agreed\s+|management\s+|remediation\s+)?actions?\s*"
    r"(?:\d+|[A-Z]{1,5}-?\d+)?\s*[:\-–]\s*\S")
_OBS_CUES = ("post-incident review", "post incident review", "post-mortem", "postmortem", "root cause",
             "incident timeline", "lessons learned", "lessons learnt", "incident summary", "what happened")
_OBS_KV = re.compile(r"(?im)^\s*(?:incident\s+date|date\s+of\s+incident|incident|summary|root\s+cause|"
                     r"services?\s+affected|impact)\s*:")
_SCENARIO_FIG = re.compile(r"(?i)\b(?:p99|p90|1[- ]in[- ]\d+|severity|frequency)\b")


def scan_injection(text: str) -> list[tuple[str, str]]:
    """Return (pattern, excerpt) pairs for instruction-like text aimed at a model or reviewer."""
    hits = []
    for pattern in _INJECTION:
        for m in pattern.finditer(text):
            start, end = max(0, m.start() - 80), min(len(text), m.end() + 80)
            hits.append((m.group(0), text[start:end].strip()))
            break
    return hits


def personal_strings(text: str) -> list[str]:
    found = EMAIL_RE.findall(text) + _IDENT_RE.findall(text)
    for m in _PHONE_RE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if 10 <= len(digits) <= 15 and (raw.startswith(("+", "(")) or digits.startswith("0")):
            found.append(raw)
    return found


def redact(text: str) -> str:
    text = EMAIL_RE.sub("[redacted-email]", text)
    text = _IDENT_RE.sub("[redacted-identifier]", text)
    for raw in sorted(set(personal_strings(text)), key=len, reverse=True):
        text = text.replace(raw, "[redacted-phone]")
    return text


def _norm_name(path: str, title: str = "") -> str:
    stem = PurePosixPath(path).as_posix().replace("/", " ")
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", stem)
    return re.sub(r"[^a-z0-9]+", " ", f"{stem} {title}".casefold()).strip()


def _cue_routes(path: str, title: str) -> dict[str, str]:
    text = f" {_norm_name(path, title)} "
    hits = {}
    for route, phrases in CUES.items():
        for phrase in phrases:
            if f" {phrase} " in text:
                hits[route] = phrase
                break
    return hits


def _anchored(route: str, fmap: dict[str, int], context_fields: set[str] = frozenset()) -> bool:
    present = set(fmap) | context_fields
    return all(any(f in present for f in group) for group in ANCHORS[route]) and any(
        f in fmap for f in ANCHORS[route][0])


# Document-level key/value context (BIA covers, PIR header tables, "Date held: ...").
CONTEXT_SERVICE_KEYS = ("service", "business service", "important business service", "ibs",
                        "business impact analysis", "services affected", "service affected", "affected service",
                        "impacted service", "affected services", "services in scope", "service in scope",
                        "ibs reference", "ibs ref")
CONTEXT_DATE_KEYS = ("incident date", "date of incident", "date held", "exercise date", "assessment date",
                     "date of exercise")
# When an impact assessment was made: its own date, else the date the analysis holding it was approved.
CONTEXT_ASSESSED_KEYS = ("assessment date", "date of assessment", "assessed on", "bia date", "date of bia",
                         "approved on", "approval date", "date approved")
CONTEXT_TOLERANCE_KEYS = ("impact tolerance", "tolerance", "maximum tolerable duration", "max tolerable duration",
                          "mtd")
CONTEXT_FIELDS = {"service": CONTEXT_SERVICE_KEYS, "observed_on": CONTEXT_DATE_KEYS,
                  "assessed_on": CONTEXT_ASSESSED_KEYS, "tolerance_reference": CONTEXT_TOLERANCE_KEYS,
                  "basis": ("basis", "basis of estimate")}
CONTEXT_ROUTES = {"dependencies": {"service"}, "impact_assessments": {"service"},
                  "observations": {"service", "observed_on"}, "exercises": {"service", "completed_on"}}
_NOTE_PART = re.compile(r"(?<=[.;])\s+(?=[A-Z][A-Za-z /&-]{1,40}:)")
_NOTE_KV = re.compile(r"^([A-Z][A-Za-z /&-]{1,40}):\s*(.+?)[.;]?$")


def _note_pairs(text: str) -> list[tuple[str, str]]:
    """'Basis: assumed (...). Impact tolerance: 4 hours.' -> [('Basis', 'assumed (...)'), ...]."""
    return [(m[1], m[2]) for part in _NOTE_PART.split(text.strip()) if (m := _NOTE_KV.match(part.strip()))]


def _kv_pairs(doc) -> list[tuple[str, str, str]]:
    pairs = []
    for table in doc.tables:
        for loc, values in table.preamble:
            if len(values) >= 2 and len(values[0]) <= 60:
                pairs.append((values[0], values[1], loc))
        if len(table.columns) >= 3:
            for row in table.rows:  # single-cell note rows inside a table ('Basis: ... Impact tolerance: ...')
                filled = [v for v in row.values if v.strip()]
                if len(filled) == 1 and ":" in filled[0]:
                    pairs.extend((k, v, row.locator) for k, v in _note_pairs(filled[0]))
        if len(table.columns) == 2 and table.rows and all(len(r.values[0]) <= 60 for r in table.rows):
            first = table.columns
            if len(first[0].label) <= 60 and not first[0].label.startswith("column "):
                pairs.append((first[0].label, first[1].label, f"{table.title} header"))
            pairs.extend((r.values[0], r.values[1], r.locator) for r in table.rows if r.values[0] and r.values[1])
    for block in doc.blocks[:80]:
        for line in block.text.splitlines():
            m = re.match(r"^\s*(?:[-*•]\s*)?\**([A-Za-z][A-Za-z /&()]{1,40}?)\**\s*:\s*(.+?)\s*$", line)
            if m:
                pairs.append((m[1], m[2], block.locator))
    return pairs


def build_context(result: DocumentResult) -> None:
    for key, value, loc in _kv_pairs(result.document):
        norm = normalize_header(key)
        if norm and norm not in result.context and len(value) <= 300:
            result.context[norm] = (value.strip(), loc)


def context_value(result: DocumentResult, field: str) -> tuple[str, str] | None:
    keys = CONTEXT_FIELDS.get("observed_on" if field == "completed_on" else field, ())
    for key in keys:
        if key in result.context:
            return result.context[key]
    return None


def _personal_columns(table: Table) -> set[int]:
    cols = {c.index for c in table.columns if is_personal_header(c.label) or is_personal_header(c.header)}
    sample = table.rows[:200]
    for column in table.columns:
        values = [r.values[column.index] for r in sample if r.values[column.index].strip()]
        if values and sum(bool(personal_strings(v)) for v in values) / len(values) >= 0.3:
            cols.add(column.index)
    return cols


def _value_cues(table: Table, route: str, fmap: dict[str, int]) -> tuple[int, list[str]]:
    sample = table.rows[:200]
    bonus, reasons = 0, []
    cells = [v for r in sample for v in r.values if v.strip()]
    if not cells:
        return 0, reasons
    if route in ("internal_loss_events", "evidence_events"):
        et = sum(basel_event_type(v) is not None and len(v) < 80 for v in cells)
        if et >= max(1, len(sample) // 3):
            bonus += 2
            reasons.append(f"{et} Basel event-type values")
    if route == "evidence_events":
        urls = sum(v.startswith(("http://", "https://")) for v in cells)
        if urls >= max(1, len(sample) // 2):
            bonus += 2
            reasons.append(f"{urls} public URL values")
    if route == "services" and "rto" in fmap:
        durations = sum(parse_duration_minutes(r.values[fmap["rto"]])[0] is not None for r in sample)
        if durations:
            bonus += 1
            reasons.append(f"{durations} RTO duration values")
    return bonus, reasons


def route_table(table: Table, index: int, path: str, title: str, excluded: set[int],
                context: set[str] = frozenset()) -> list[Route]:
    columns = [(c.index, c.label, c.group) for c in table.columns]
    cues = _cue_routes(path, f"{title} {table.title} {table.locator_prefix}")
    routes = []
    for route in FIELD_SYNONYMS:
        fmap = map_columns(route, columns, excluded)
        available = {"completed_on" if f == "observed_on" and route == "exercises" else f
                     for f in context} & CONTEXT_ROUTES.get(route, set())
        if not _anchored(route, fmap, available):
            continue
        strong = STRONG_FIELDS.get(route, set())
        score = sum(2 if f in strong else 1 for f in fmap)
        reasons = [f"header {table.columns[i].header!r} -> {f}" for f, i in sorted(fmap.items(), key=lambda x: x[1])]
        bonus, why = _value_cues(table, route, fmap)
        if route == "observations" and "title" in fmap and \
                normalize_header(table.columns[fmap["title"]].label) in ("observation", "observations", "finding",
                                                                       "findings"):
            bonus += 1
            why.append("observation/finding column")
        score += bonus
        reasons += why
        if route in cues:
            score += 2
            reasons.append(f"name cue {cues[route]!r}")
        if score >= TABLE_MIN_SCORE:
            routes.append(Route(route, float(score), reasons, f"table:{index}", field_map=fmap))
    routes.sort(key=lambda r: (-r.score, CONTRACT_ROUTES.index(r.route)))
    return routes


def route_text(text: str, path: str, title: str, cells: str = "") -> list[Route]:
    cues = _cue_routes(path, title)
    lowered = f"{text} {cells}".casefold()
    scores: dict[str, tuple[float, list[str]]] = {}

    def add(route: str, points: float, reason: str) -> None:
        score, reasons = scores.get(route, (0.0, []))
        scores[route] = (score + points, reasons + [reason])

    if actions := len(_ACTION_LINE.findall(text)):
        add("actions", 3 * min(actions, 3), f"{actions} 'Action:' lines")
    obs = sum(cue in lowered for cue in _OBS_CUES)
    kv = len(_OBS_KV.findall(text))
    if obs or kv >= 2:
        add("observations", 2 * min(obs, 3) + min(kv, 3), f"{obs} post-incident cues, {kv} incident fields")
    if "scenario" in lowered and (figs := len(_SCENARIO_FIG.findall(text))):
        add("org_scenarios", min(figs, 4), f"scenario text with {figs} frequency/severity terms")
    if (ex := lowered.count("tabletop") + lowered.count("exercise")) >= 2:
        add("exercises", min(ex, 4), f"{ex} exercise mentions")
    if (asm := lowered.count("assumption")) >= 2:
        add("assumptions", min(asm, 4), f"{asm} assumption mentions")
    for route, phrase in cues.items():
        if route in scores:
            add(route, 2, f"name cue {phrase!r}")
    routes = [Route(r, s, why, "text", confidence="high" if s >= TEXT_MIN_SCORE + 2 else "low")
              for r, (s, why) in scores.items() if s >= TEXT_MIN_SCORE]
    routes.sort(key=lambda r: (-r.score, CONTRACT_ROUTES.index(r.route)))
    return routes


def triage(result: DocumentResult, *, tiebreak: Any = None) -> None:
    """Populate routes and flags on ``result``. ``tiebreak(options, context)`` may return a route."""
    doc = result.document
    if doc is None:
        return
    title = str(doc.metadata.get("title") or "")
    _flag_injection(result)
    personal_hits = _flag_personal(result)
    build_context(result)
    context = {f for f in ("service", "observed_on") if context_value(result, f)}
    if not doc.blocks and not doc.tables:
        result.flag("unreadable", "no extractable text or tables")
    unrouted = []
    for index, table in enumerate(doc.tables):
        excluded = result.excluded_columns.get(index, set())
        options = route_table(table, index, doc.path, title, excluded, context)
        if not options:
            unrouted.append(table)
        if not options:
            if excluded:
                result.flag("pii", f"table {table.title!r} holds personal data and matches no contract")
                result.excluded_columns[index] = {c.index for c in table.columns}
            continue
        chosen = _choose(options, tiebreak, {"file": doc.path, "table": table.title,
                                              "headers": [c.header for c in table.columns][:40]})
        result.routes.append(chosen)
    text = "\n\n".join(b.text for b in doc.blocks if b.locator not in result.excluded_blocks)
    cells = " ".join(" ".join([c.header for c in t.columns] + [v for r in t.rows[:50] for v in r.values])
                     for t in unrouted)
    from .sections import section_routes

    structured = {r.route: r for r in section_routes(result)}
    for option in route_text(text, doc.path, title, cells):
        result.routes.append(structured.pop(option.route, option))
    result.routes.extend(structured.values())
    if not result.routes:
        if any(f.flag in ("pii", "unreadable") for f in result.flags) or personal_hits >= PII_DOC_THRESHOLD:
            reason = "personal data" if any(f.flag == "pii" for f in result.flags) else "unreadable"
            result.routes.append(Route("quarantine", 0.0, [f"quarantined: {reason}"]))
        else:
            result.flag("out_of_scope", "no contract signals in headers, title or text")
            result.routes.append(Route("ignore", 0.0, ["no contract signals in headers, title or text"]))


def _choose(options: list[Route], tiebreak: Any, context: dict) -> Route:
    top = options[0]
    if len(options) > 1 and top.score - options[1].score < LOW_CONFIDENCE_MARGIN:
        top.confidence = "low"
        top.reasons.append("close alternative: " + ", ".join(f"{o.route} ({o.score:g})" for o in options[1:3]))
        if tiebreak is not None:
            close = [o for o in options if top.score - o.score < LOW_CONFIDENCE_MARGIN]
            picked = tiebreak([o.route for o in close], context)
            for option in close:
                if option.route == picked:
                    option.method, option.confidence = "llm", "low"
                    option.reasons.append("LLM tiebreak among deterministic candidates")
                    return option
    return top


def _flag_injection(result: DocumentResult) -> None:
    doc = result.document
    for block in doc.blocks:
        for phrase, excerpt in scan_injection(block.text):
            result.flag("injection", f"instruction-like text: {phrase!r}", block.locator, excerpt[:300])
            result.excluded_blocks.add(block.locator)
    for index, table in enumerate(doc.tables):
        for row in table.rows:
            for c, value in enumerate(row.values):
                if len(value) > 12 and (hits := scan_injection(value)):
                    result.flag("injection", f"instruction-like text: {hits[0][0]!r}",
                                table.cell_locator(row, c), hits[0][1][:300])
                    row.values[c] = ""  # the instruction is shown to the reviewer, never extracted


def _flag_personal(result: DocumentResult) -> int:
    doc = result.document
    for index, table in enumerate(doc.tables):
        cols = _personal_columns(table)
        if cols:
            result.excluded_columns[index] = cols
            names = ", ".join(table.columns[c].header for c in sorted(cols))
            result.flag("pii", f"personal-data columns excluded from extraction: {names}", table.title)
    hits = 0
    for block in doc.blocks:
        found = personal_strings(block.text)
        if found:
            hits += len(found)
            block.text = redact(block.text)
    if hits >= PII_DOC_THRESHOLD:
        result.flag("pii", f"{hits} personal contact strings detected and redacted")
    elif hits:
        doc.warnings.append(f"{hits} contact string(s) redacted before extraction")
    return hits


_VERSION_IN_NAME = re.compile(r"(?i)[\s_\-.(]*(?:v|ver|version)[\s_\-.]?(\d+(?:[._]\d+)*)\)?")


def family_version(path: str, metadata: dict) -> tuple[str, tuple[int, ...]] | None:
    stem = PurePosixPath(path).stem
    m = None
    for m in _VERSION_IN_NAME.finditer(stem):
        pass
    if m is not None:
        family = (stem[:m.start()] + stem[m.end():]).strip(" _-.")
        version = m[1]
    elif metadata.get("version"):
        family, version = stem, str(metadata["version"])
    else:
        return None
    family = re.sub(r"[^a-z0-9]+", " ", family.casefold()).strip()
    try:
        return family, tuple(int(p) for p in re.split(r"[._]", version) if p)
    except ValueError:
        return None


def mark_superseded(results: list[DocumentResult]) -> None:
    families: dict[str, list[tuple[tuple[int, ...], DocumentResult]]] = {}
    for result in results:
        if result.document is None:
            continue
        fv = family_version(result.entry.path, result.document.metadata)
        if fv and fv[0]:
            families.setdefault(fv[0], []).append((fv[1], result))
    for members in families.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m[0])
        newest_version, newest = members[-1]
        for version, result in members[:-1]:
            if version < newest_version:
                result.flag("superseded", f"superseded by {newest.entry.path}")
                result.extract = False
