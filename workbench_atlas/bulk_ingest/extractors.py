"""Extractor pods: routed documents -> candidate records with provenance.

Tabular routes map headers to fields and coerce values deterministically.
Narrative routes use reliable line patterns ("Action: ... Owner: ... Due: ...",
"Incident date:" key-value headers) and, when enabled, the LLM lane, whose
fields survive only with a verbatim source span.

Assembled records hold contract fields in ``data`` and linking hints (service
mentions, raw values, organization references) in ``meta``. Ids and links are
resolved later by the linker.
"""

from __future__ import annotations

import re
from typing import Any

from . import coerce
from .models import Candidate, DocumentResult, Issue, Provenance, Route, Table

POD = "extractor"
_LIST_FIELDS = {"process_names", "source_refs", "evidence_refs", "injects", "outcomes", "closure_evidence"}
_DATE_FIELDS = {"validated_on", "valid_until", "expires_on", "due_date", "completed_on", "observed_on",
                "source_date", "occurrence_date", "discovery_date", "accounting_date", "last_workshop"}
_MONEY_FIELDS = {"gross_loss", "recoveries", "net_loss", "severity_p50", "severity_p90", "severity_p99",
                 "loss_amount_usd"}
_SERVICE_LIST_KINDS = {"controls", "assumptions", "actions", "exercises", "observations",
                       "internal_loss_events", "org_scenarios"}
# Kinds whose scenario_ids may reference an organization scenario staged in the batch.
_SCENARIO_LINK_KINDS = {"assumptions", "actions", "exercises", "observations", "internal_loss_events"}


def _set(data: dict, key: str, value: Any) -> None:
    if value not in (None, "", []):
        data[key] = value


def assemble(kind: str, raw: dict[str, str], *, hints: dict[str, str] | None = None,
             dayfirst: dict[str, bool | None] | None = None) -> list[tuple[dict, dict, list[str]]]:
    """Turn raw field strings into (data, meta, notes) records for one contract kind."""
    hints, dayfirst = hints or {}, dayfirst or {}
    raw = {k: coerce.clean(v) for k, v in raw.items() if coerce.clean(v)}
    data: dict[str, Any] = {}
    meta: dict[str, Any] = {"raw": dict(raw)}
    notes: list[str] = []

    def date_field(field: str, target: str | None = None) -> None:
        if field not in raw:
            return
        value, note = coerce.parse_date(raw[field], dayfirst=dayfirst.get(field))
        if value:
            data[target or field] = value
        else:
            data[target or field] = raw[field]  # left for validation to reject visibly
            notes.append(f"{field}: {note}")

    def money_field(field: str) -> str | None:
        if field not in raw:
            return None
        hint = f"{hints.get(field, '')} {raw.get('units', '')} {raw.get('currency', '')} {raw.get('severity_currency', '')}"
        amount, ccy = coerce.parse_money(raw[field], header_hint=hint.strip())
        if amount is None:
            notes.append(f"{field}: unrecognised amount {raw[field][:40]!r}")
            return None
        data[field] = amount
        return ccy

    if "org_ref" in raw:
        meta["org_ref"] = raw["org_ref"]
    services = split_mentions(raw.get("service", ""))
    if services and kind in _SERVICE_LIST_KINDS:
        meta["service_mentions"] = {"service_ids": services}
    if raw.get("scenario") and kind in _SCENARIO_LINK_KINDS:
        meta["scenario_mentions"] = split_mentions(raw["scenario"])

    if kind == "services":
        _set(data, "name", raw.get("name"))
        _set(data, "owner", raw.get("owner"))
        if "criticality" in raw:
            data["criticality"] = coerce.criticality(raw["criticality"]) or raw["criticality"]
        tolerance = coerce.tolerance_text(raw.get("rto", ""), raw.get("rpo", ""), raw.get("impact_tolerance", ""))
        _set(data, "impact_tolerance", tolerance)
        for field in ("legal_entity", "region", "notes"):
            _set(data, field, raw.get(field))
        _set(data, "process_names", coerce.split_list(raw.get("process_names", "")))
        _set(data, "source_refs", coerce.split_list(raw.get("source_refs", "")))
        meta["aliases"] = [raw["org_ref"]] if "org_ref" in raw else []
        return [(data, meta, notes)]

    if kind == "dependencies":
        provider, to_service = raw.get("external_provider", ""), raw.get("to_service", "")
        target = raw.get("target", "")
        kind_text = f"{raw.get('dependency_type', '')} {raw.get('category', '')}".lower()
        if target and not provider and not to_service:
            if re.search(r"supplier|vendor|third|external|provider|outsourc", kind_text):
                provider = target
            elif re.search(r"internal|service|application|system|platform", kind_text):
                to_service = target
            else:
                notes.append(f"dependency target {target!r}: type unknown (internal service or supplier?)")
        downstream = "downstream" in raw.get("direction", "").lower()
        froms = services or [""]
        out = []
        for service in froms:
            item, item_meta = dict(data), {**meta, "raw": dict(raw)}
            source, dest = service, to_service
            if downstream and to_service:
                source, dest = to_service, service  # the named service depends on this one
            item_meta["service_mentions"] = {"from_service_id": [source] if source else []}
            if dest:
                item_meta["service_mentions"]["to_service_id"] = [dest]
            if downstream and provider:
                notes.append("downstream supplier relationship kept as declared; check its direction")
            if provider:
                item_meta["vendor"] = provider
                item["external_provider"] = provider
            name = raw.get("name")
            if not name and source and (provider or dest):
                name = f"{source} depends on {provider or dest}"
                item_meta["derived_fields"] = ["name"]
            if not raw.get("category") and raw.get("dependency_type"):
                item["category"] = raw["dependency_type"]
            _set(item, "name", name)
            for field in ("category", "common_cause", "supplier_external_id", "notes"):
                if field not in item:
                    _set(item, field, raw.get(field))
            out.append((item, item_meta, list(notes)))
        return out

    if kind == "mechanisms":
        _set(data, "name", raw.get("name"))
        _set(data, "description", raw.get("description"))
        return [(data, meta, notes)]

    if kind == "controls":
        _set(data, "name", raw.get("name"))
        _set(data, "owner", raw.get("owner"))
        if raw.get("mechanism"):
            meta["mechanism_mentions"] = coerce.split_list(raw["mechanism"])
        if "validation_status" in raw:
            data["validation_status"] = coerce.status("controls", raw["validation_status"]) or raw["validation_status"]
        date_field("validated_on")
        date_field("valid_until")
        _set(data, "evidence_refs", coerce.split_list(raw.get("evidence_refs", "")))
        return [(data, meta, notes)]

    if kind == "assumptions":
        _set(data, "statement", raw.get("statement"))
        _set(data, "owner", raw.get("owner"))
        date_field("expires_on")
        _set(data, "test", raw.get("test"))
        if "status" in raw:
            data["status"] = coerce.status("assumptions", raw["status"]) or raw["status"]
        _set(data, "evidence_refs", coerce.split_list(raw.get("evidence_refs", "")))
        return [(data, meta, notes)]

    if kind == "actions":
        _set(data, "title", raw.get("title"))
        _set(data, "owner", raw.get("owner"))
        date_field("due_date")
        if "status" in raw:
            data["status"] = coerce.status("actions", raw["status"]) or raw["status"]
        _set(data, "closure_evidence", coerce.split_list(raw.get("closure_evidence", "")))
        _set(data, "rationale", raw.get("rationale"))
        return [(data, meta, notes)]

    if kind == "exercises":
        _set(data, "title", raw.get("title"))
        for field in ("injects", "outcomes", "evidence_refs"):
            _set(data, field, coerce.split_list(raw.get(field, "")))
        if "status" in raw:
            data["status"] = coerce.status("exercises", raw["status"]) or raw["status"]
        if data.get("status") == "planned" and "completed_on" in raw:
            # A planned exercise's date is when it is scheduled, not a completion date.
            scheduled, _ = coerce.parse_date(raw["completed_on"], dayfirst=dayfirst.get("completed_on"))
            meta["scheduled_on"] = scheduled or raw["completed_on"]
            notes.append(f"planned exercise: date {raw['completed_on']!r} kept as scheduled, not completed")
        else:
            date_field("completed_on")
        return [(data, meta, notes)]

    if kind == "observations":
        _set(data, "title", raw.get("title"))
        if raw.get("mechanism"):
            meta["mechanism_mentions"] = coerce.split_list(raw["mechanism"])
        date_field("observed_on")
        _set(data, "description", raw.get("description") or raw.get("title"))
        if not raw.get("description") and raw.get("title"):
            meta.setdefault("derived_fields", []).append("description")
        _set(data, "evidence_refs", coerce.split_list(raw.get("evidence_refs", "")))
        return [(data, meta, notes)]

    if kind == "impact_assessments":
        _set(data, "title", raw.get("title"))
        if services:
            meta["service_mentions"] = {"service_id": services[:1]}
        if not raw.get("title") and services and raw.get("time_since_onset_minutes"):
            data["title"] = f"{services[0]} impact after {raw['time_since_onset_minutes']}"
            meta.setdefault("derived_fields", []).append("title")
        date_field("observed_on")
        for field in ("time_since_onset_minutes", "downtime_minutes", "capacity_loss", "backlog"):
            if field in raw:
                minutes, _ = coerce.parse_duration_minutes(raw[field])
                timed = field in ("time_since_onset_minutes", "downtime_minutes")
                number = minutes if minutes is not None and timed else _float(raw[field])
                data[field] = float(number) if number is not None else raw[field]
        if "customers" in raw:
            number = _float(raw["customers"])
            data["customers"] = int(number) if number is not None and number.is_integer() else raw["customers"]
        _set(data, "tolerance_reference", raw.get("tolerance_reference"))
        basis = raw.get("basis", "").casefold()
        if basis.startswith(("assumed", "measured")):  # only an explicit statement sets the basis
            data["basis"] = "assumed" if basis.startswith("assumed") else "measured"
        return [(data, meta, notes)]

    if kind == "evidence_events":
        _set(data, "headline", raw.get("headline"))
        for field in ("organization", "source_url", "source_publisher", "source_title", "country", "region"):
            _set(data, field, raw.get(field))
        date_field("source_date")
        ccy = money_field("loss_amount_usd")
        if ccy not in (None, "USD") and "loss_amount_usd" in data:
            notes.append(f"loss amount is in {ccy}, not USD; left blank for the reviewer")
            meta["loss_amount_original"] = raw.get("loss_amount_usd")
            data.pop("loss_amount_usd")
        _set(data, "independently_authored_summary", raw.get("summary"))
        if raw.get("basel_event_type"):
            meta["basel_event_type"] = coerce.basel_event_type(raw["basel_event_type"]) or raw["basel_event_type"]
        return [(data, meta, notes)]

    if kind == "internal_loss_events":
        _set(data, "org_ref", raw.get("org_ref"))
        _set(data, "title", raw.get("title"))
        if "basel_event_type" in raw:
            data["basel_event_type"] = coerce.basel_event_type(raw["basel_event_type"]) or raw["basel_event_type"]
        for field in ("org_event_type_code", "business_line", "description", "root_cause", "legal_entity",
                      "incident_ref"):
            _set(data, field, raw.get(field))
        for field in ("occurrence_date", "discovery_date", "accounting_date"):
            date_field(field)
        currencies = {c for c in (money_field(f) for f in ("gross_loss", "recoveries", "net_loss",
                                                           "potential_impact", "other_recoveries")) if c}
        if "other_recoveries" in data:
            data["recoveries"] = data.get("recoveries", 0.0) + data.pop("other_recoveries")
            meta.setdefault("derived_fields", []).append("recoveries (sum of recovery columns)")
        explicit = coerce.currency_of(raw.get("currency", "")) or (raw.get("currency") or "").upper() or None
        if explicit:
            data["currency"] = explicit
        elif len(currencies) == 1:
            data["currency"] = currencies.pop()
        elif len(currencies) > 1:
            notes.append("amounts carry different currencies: " + ", ".join(sorted(currencies)))
        if "status" in raw:
            data["status"] = coerce.status("internal_loss_events", raw["status"]) or raw["status"]
        if "near_miss" in raw:
            flag = coerce.boolean(raw["near_miss"])
            data["near_miss"] = flag if flag is not None else raw["near_miss"]
        if "boundary" in raw:
            data["boundary"] = _boundary(raw["boundary"])
        return [(data, meta, notes)]

    if kind == "org_scenarios":
        for field in ("org_ref", "title", "narrative", "owner", "org_taxonomy_code", "frequency_text"):
            _set(data, field, raw.get(field))
        if "frequency_text" in raw:
            text = raw["frequency_text"]
            if re.fullmatch(r"\d+(?:\.\d+)?", text) and re.search(r"(?i)1\s*-?\s*in\s*-?\s*n",
                                                                 hints.get("frequency_text", "")):
                text = f"1 in {text} years"  # the header states the 1-in-N convention
                data["frequency_text"] = text
            _set(data, "annual_frequency", coerce.annual_frequency(text))
            if "annual_frequency" in data:  # computed from frequency_text, which carries any conflict
                meta.setdefault("derived_fields", []).append("annual_frequency")
        currencies = {c for c in (money_field(f) for f in ("severity_p50", "severity_p90", "severity_p99")) if c}
        explicit = coerce.currency_of(raw.get("severity_currency", "")) or None
        if explicit or len(currencies) == 1:
            data["severity_currency"] = explicit or currencies.pop()
        date_field("last_workshop")
        _set(data, "status", raw.get("status", "").lower() or None)
        if raw.get("basel_event_type"):
            data["basel_event_type"] = coerce.basel_event_type(raw["basel_event_type"]) or raw["basel_event_type"]
        return [(data, meta, notes)]

    if kind == "taxonomy_crosswalk":
        for field in ("org_code", "org_label", "parent_code"):
            _set(data, field, raw.get(field))
        if "level" in raw:
            m = re.search(r"\d+", raw["level"])
            data["level"] = int(m[0]) if m else raw["level"]
        if raw.get("definition"):
            meta["definition"] = raw["definition"]
        return [(data, meta, notes)]
    raise ValueError(f"no assembler for {kind}")


def split_mentions(value: str) -> list[str]:
    """Service mention lists: ';', '|', newline or ',' separated."""
    return [coerce.clean(p) for p in re.split(r"[;|\n,]", str(value or "")) if coerce.clean(p)]


def _float(value: str) -> float | None:
    try:
        return float(value.replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _boundary(value: str) -> str:
    lowered = value.lower()
    if "credit" in lowered:
        return "credit"
    if "market" in lowered:
        return "market"
    flag = coerce.boolean(value)
    if flag is False or lowered in ("none", "n/a"):
        return "none"
    return "other" if flag else value


# --------------------------------------------------------------------------- tables


def _hints(table: Table, fmap: dict[str, int]) -> dict[str, str]:
    return {field: table.columns[i].header for field, i in fmap.items()}


_CONTEXT_DEFAULTS = {"dependencies": ("service",),
                     "impact_assessments": ("service", "observed_on", "tolerance_reference", "basis"),
                     "observations": ("service", "observed_on"), "exercises": ("service", "completed_on")}
# Route-specific context keys: an impact assessment's date is when it was assessed or approved.
_CONTEXT_KEY_FOR = {"impact_assessments": {"observed_on": "assessed_on"}}


def _context_defaults(result: DocumentResult, route: str, fmap: dict[str, int]) -> tuple[dict, dict]:
    from .triage import context_value

    raw, cells = {}, {}
    for field in _CONTEXT_DEFAULTS.get(route, ()):
        key = _CONTEXT_KEY_FOR.get(route, {}).get(field, field)
        if field not in fmap and (found := context_value(result, key)):
            raw[field], cells[field] = found
    return raw, cells


def group_date_orders(table: Table, date_columns: list[int]) -> tuple[list[str], list[bool | None]] | None:
    """Day/month order per value of a categorical column, for tables that mix conventions.

    An extract that merges two source systems (one writing 31/12/2025, the other
    12/31/2025) has no table-wide order. A column qualifies when it has 2 to 20
    distinct values, every group's unambiguous dates agree with each other, and
    at least two groups disagree with each other. Qualifying columns must never
    contradict each other on a row (one may know an order the other does not);
    otherwise None, and the ambiguous dates stay unparsed for the reviewer.
    Returns (headers used, per-row order aligned with ``table.rows``)."""
    if len(table.rows) < 10 or not date_columns:
        return None
    found: dict[tuple, str] = {}
    for column in table.columns:
        if column.index in date_columns:
            continue
        values = [r.values[column.index].strip() for r in table.rows]
        distinct = set(values)
        if not 2 <= len(distinct) <= 20:
            continue
        orders: dict[str, set[str]] = {}
        for row, value in zip(table.rows, values):
            for i in date_columns:
                if (o := coerce.date_order(row.values[i])) is not None:
                    orders.setdefault(value, set()).add(o)
        if any(len(o) > 1 for o in orders.values()):
            continue
        resolved = {g: next(iter(o)) == "dmy" for g, o in orders.items()}
        if len(set(resolved.values())) < 2:
            continue
        found.setdefault(tuple(resolved.get(v) for v in values), column.header)
    if not found:
        return None
    combined: list[bool | None] = [None] * len(table.rows)
    for signature in found:
        for n, order in enumerate(signature):
            if order is None:
                continue
            if combined[n] is not None and combined[n] != order:
                return None  # two groupings disagree about a row: refuse to guess
            combined[n] = order
    return sorted(set(found.values())), combined


def extract_table(result: DocumentResult, route: Route, table: Table) -> list[Candidate]:
    fmap = route.field_map
    if route.route == "taxonomy_crosswalk" and "l1_code" in fmap:
        return _explode_taxonomy(result, route, table)
    hints = _hints(table, fmap)
    ctx_raw, ctx_cells = _context_defaults(result, route.route, fmap)
    near_miss = re.search(r"(?i)near[\s_-]?miss|\bnm\b", f"{result.entry.path} {table.title} "
                          f"{table.locator_prefix} {hints.get('org_ref', '')}") is not None
    skipped_notes = 0
    date_fields = {f: i for f, i in fmap.items() if f in _DATE_FIELDS}
    dayfirst = {f: coerce.infer_dayfirst([r.values[i] for r in table.rows]) for f, i in date_fields.items()}
    table_order = coerce.infer_dayfirst([r.values[i] for r in table.rows for i in date_fields.values()])
    for f, order in dayfirst.items():
        if order is None and table_order is not None:
            dayfirst[f] = table_order
            result.issues.append(Issue("info", "date_order_inferred",
                                       f"{table.title}: {f} read {'day' if table_order else 'month'}-first, "
                                       "inferred from unambiguous dates elsewhere in this table",
                                       result.entry.path, table.title))
    grouped = None
    if table_order is None and any(v is None for v in dayfirst.values()):
        grouped = group_date_orders(table, list(date_fields.values()))
        if grouped is not None:
            group_label = " / ".join(grouped[0])
            result.issues.append(Issue("info", "date_order_inferred",
                                       f"{table.title}: day/month order differs by {group_label!r}; each group's "
                                       "ambiguous dates read like its own unambiguous ones",
                                       result.entry.path, table.title))
    row_index = {id(r): n for n, r in enumerate(table.rows)}
    candidates = []
    for row in table.rows:
        raw = {f: row.values[i] for f, i in fmap.items() if row.values[i].strip()}
        if not raw or _looks_like_total(raw):
            continue
        if len(table.columns) >= 3 and sum(bool(v.strip()) for v in row.values) == 1:
            skipped_notes += 1  # a footnote or caption row inside the table
            continue
        cells = {f: table.cell_locator(row, i) for f, i in fmap.items() if row.values[i].strip()}
        derived = []
        for field, value in ctx_raw.items():
            raw.setdefault(field, value)
            cells.setdefault(field, ctx_cells[field])
            derived.append(f"{field} from document context")
        if route.route == "internal_loss_events" and "near_miss" not in raw and (
                near_miss or "near miss" in raw.get("status", "").lower()):
            raw["near_miss"] = "yes"
            derived.append("near_miss from the near-miss log title or status text")
        if route.route == "observations" and "evidence_refs" not in raw:
            raw["evidence_refs"] = result.entry.path
            derived.append("evidence_refs (the source document)")
        row_order = coerce.infer_dayfirst([raw[f] for f in date_fields if f in raw])
        own_row_order = row_order is not None
        if row_order is None and grouped is not None:
            group_order = grouped[1][row_index[id(row)]]
            if group_order is not None and any(coerce.date_order(raw.get(f, "")) is None and f in raw
                                               for f in date_fields):
                row_order = group_order
                derived.append(f"ambiguous dates read {'day' if group_order else 'month'}-first like other "
                               f"rows with the same {group_label!r}")
        order = {f: (v if v is not None else row_order) for f, v in dayfirst.items()}
        if own_row_order and any(dayfirst.get(f) is None and coerce.date_order(raw.get(f, "")) is None
                                         and f in raw for f in date_fields):
            derived.append(f"ambiguous dates read {'day' if row_order else 'month'}-first like this row's other dates")
        for data, meta, notes in assemble(route.route, raw, hints=hints, dayfirst=order):
            if not data:
                continue
            if derived:
                meta["derived_fields"] = meta.get("derived_fields", []) + derived
            meta["table_rows"] = len(table.rows)
            meta["table"] = table.title
            candidates.append(Candidate(
                kind=route.route, data=data, meta=meta, warnings=notes,
                provenance=[Provenance(result.entry.path, row.locator, f"{POD}:{route.route}",
                                       "deterministic", cells=cells)]))
    if skipped_notes:
        result.issues.append(Issue("info", "note_rows_skipped",
                                   f"{table.title}: {skipped_notes} single-cell note row(s) not treated as records",
                                   result.entry.path, table.title))
    unmapped = [c.header for c in table.columns if c.index not in fmap.values()
                and c.index not in result.excluded_columns.get(int(route.target.split(":")[1]), set())]
    if unmapped:
        result.issues.append(Issue("info", "unmapped_columns",
                                   f"{table.title}: columns not mapped to {route.route}: " + ", ".join(unmapped[:20]),
                                   result.entry.path, table.title))
    return candidates


def _explode_taxonomy(result: DocumentResult, route: Route, table: Table) -> list[Candidate]:
    """Wide 'L1 Code | L1 Name | L2 Code | ...' exports -> one node per level, parents linked."""
    fmap = route.field_map
    levels = [n for n in range(1, 5) if f"l{n}_code" in fmap]
    last: dict[int, tuple[str, str, str]] = {}
    seen: set[str] = set()
    out = []
    for row in table.rows:
        present = [n for n in levels if row.values[fmap[f"l{n}_code"]].strip()]
        if not present:
            continue
        deepest = max(present)
        for n in levels:
            if n > deepest:
                break
            code = row.values[fmap[f"l{n}_code"]].strip()
            name = row.values[fmap[f"l{n}_name"]].strip() if f"l{n}_name" in fmap else ""
            if code:
                last[n] = (code, name, table.cell_locator(row, fmap[f"l{n}_code"]))
            elif n in last:
                code, name = last[n][0], last[n][1]  # merged or blank parent cells continue upward
            if not code or code in seen:
                continue
            seen.add(code)
            raw = {"org_code": code, "org_label": name, "level": str(n),
                   "parent_code": last[n - 1][0] if n > 1 and n - 1 in last else ""}
            if n == deepest and "definition" in fmap:
                raw["definition"] = row.values[fmap["definition"]]
            cells = {"org_code": last[n][2] if n in last else row.locator}
            for data, meta, notes in assemble("taxonomy_crosswalk", raw):
                meta["table_rows"] = len(table.rows)
                out.append(Candidate("taxonomy_crosswalk", data, [Provenance(
                    result.entry.path, row.locator, f"{POD}:taxonomy_crosswalk", "deterministic", cells=cells)],
                    meta=meta, warnings=notes))
    return out


def _looks_like_total(raw: dict[str, str]) -> bool:
    first = next(iter(raw.values()), "").strip().lower()
    return first in ("total", "totals", "grand total", "sum", "subtotal")


# --------------------------------------------------------------------------- narrative

_KV = re.compile(r"(?im)^\s*(?:[-*•]\s*)?([A-Za-z][A-Za-z /()]{1,40}?)\s*:\s*(.+?)\s*$")
_ACTION_START = re.compile(
    r"(?i)^\s*(?:[-*•]\s*)?(?:agreed\s+|management\s+|remediation\s+)?actions?\s*"
    r"(?P<ref>\d+|[A-Z]{1,5}-?\d+)?\s*[:\-–]\s*(?P<rest>.+)$")
_INLINE = re.compile(r"(?i)\b(owner|due(?:\s+date)?|target(?:\s+date)?|status)\s*[:=]\s*")
_OBS_KEYS = {
    "title": ("incident", "incident title", "title", "event"),
    "observed_on": ("incident date", "date of incident", "date", "occurred"),
    "description": ("summary", "incident summary", "what happened", "description"),
    "service": ("services affected", "service affected", "affected services", "service", "impacted service"),
}


def _split_inline(text: str) -> dict[str, str]:
    """'Fix X — Owner: Jo Bloggs; Due: 31/03/2025' -> {'title':..., 'owner':..., 'due_date':...}."""
    parts = _INLINE.split(text)
    fields = {"title": parts[0].strip(" -–—;,.|")}
    for key, value in zip(parts[1::2], parts[2::2]):
        key = key.lower()
        name = "owner" if key == "owner" else "status" if key == "status" else "due_date"
        fields[name] = value.strip(" -–—;,.|")
    return {k: v for k, v in fields.items() if v}


def extract_actions_text(result: DocumentResult) -> list[Candidate]:
    blocks = [b for b in result.document.blocks if b.locator not in result.excluded_blocks]
    candidates = []
    for bi, block in enumerate(blocks):
        lines = block.text.splitlines()
        for li, line in enumerate(lines):
            m = _ACTION_START.match(line)
            if not m:
                continue
            fields = _split_inline(m["rest"])
            spans = {"title": fields.get("title", "")}
            follow = lines[li + 1:li + 4] + [ln for b in blocks[bi + 1:bi + 3] for ln in b.text.splitlines()][:3]
            for extra in follow:
                if _ACTION_START.match(extra):
                    break
                for key, value in _split_inline("x " + extra).items():
                    if key != "title" and key not in fields:
                        fields[key] = value
            for key in ("owner", "due_date", "status"):
                if key in fields:
                    spans[key] = fields[key]
            if m["ref"]:
                fields["org_ref"] = m["ref"]
            for data, meta, notes in assemble("actions", fields):
                candidates.append(Candidate(
                    "actions", data, [Provenance(result.entry.path, block.locator, f"{POD}:actions",
                                                 "deterministic", spans={k: v for k, v in spans.items() if v})],
                    meta=meta, warnings=notes))
    return candidates


def extract_observation_text(result: DocumentResult) -> list[Candidate]:
    blocks = [b for b in result.document.blocks if b.locator not in result.excluded_blocks]
    found: dict[str, tuple[str, str]] = {}
    for block in blocks:
        for m in _KV.finditer(block.text):
            key = m[1].strip().lower()
            for field, keys in _OBS_KEYS.items():
                if key in keys and field not in found:
                    found[field] = (m[2].strip(), block.locator)
    if "title" not in found:
        heading = next((b for b in blocks if b.kind == "heading"), None)
        title = result.document.metadata.get("title") or (heading.text if heading else "")
        if title:
            found["title"] = (str(title), heading.locator if heading else "metadata")
    if not {"title", "observed_on"} <= set(found) or "description" not in found:
        return []
    raw = {k: v for k, (v, _) in found.items()}
    raw["evidence_refs"] = result.entry.path
    out = []
    for data, meta, notes in assemble("observations", raw):
        meta["derived_fields"] = ["evidence_refs"]
        locator = found["title"][1]
        out.append(Candidate("observations", data, [Provenance(
            result.entry.path, locator, f"{POD}:observations", "deterministic",
            spans={k: v for k, (v, _) in found.items()},
            cells={k: loc for k, (_, loc) in found.items()})], meta=meta, warnings=notes))
    return out


def _column_values(result: DocumentResult, words: tuple[str, ...]) -> list[tuple[str, str]]:
    from .synonyms import normalize_header

    values = []
    for table in result.document.tables:
        for column in table.columns:
            if normalize_header(column.label) in words:
                values.extend((r.values[column.index], table.cell_locator(r, column.index))
                              for r in table.rows if r.values[column.index].strip())
    return values


def extract_exercise_text(result: DocumentResult) -> list[Candidate]:
    """One exercise per report: title, date held, inject and outcome tables, services in scope."""
    from .triage import context_value

    held = context_value(result, "completed_on")
    heading = next((b for b in result.document.blocks if b.kind == "heading"), None)
    title = str(result.document.metadata.get("title") or (heading.text if heading else ""))
    if not held or not title:
        return []
    injects = _column_values(result, ("inject", "injects"))
    outcomes = _column_values(result, ("observation", "outcome", "outcomes", "finding", "findings"))
    raw = {"title": title, "completed_on": held[0], "evidence_refs": result.entry.path,
           "injects": "\n".join(v for v, _ in injects), "outcomes": "\n".join(v for v, _ in outcomes)}
    if "held" in next((k for k in result.context if result.context[k] == held), ""):
        raw["status"] = "completed"
    service = context_value(result, "service")
    mentions = [service[0]] if service else [v for v, _ in _column_values(result, ("services", "service"))]
    if mentions:
        raw["service"] = "; ".join(dict.fromkeys(m for v in mentions for m in split_mentions(v)))
    out = []
    for data, meta, notes in assemble("exercises", raw):
        meta["derived_fields"] = ["evidence_refs"] + (["status (from 'date held')"] if "status" in raw else [])
        cells = {"completed_on": held[1]}
        if injects:
            cells["injects"] = injects[0][1]
        if outcomes:
            cells["outcomes"] = outcomes[0][1]
        out.append(Candidate("exercises", data, [Provenance(
            result.entry.path, heading.locator if heading else "metadata", f"{POD}:exercises", "deterministic",
            spans={"title": title, "completed_on": held[0]}, cells=cells)], meta=meta, warnings=notes))
    return out


_SCENARIO_HEADING = re.compile(r"(?i)^\s*(?:\d+[.)]\s*)?(?:scenario|scn)[\s-]*(\d{1,4})\s*[-–—:]\s*(.+?)\s*$")
_SCENARIO_REF = re.compile(r"\b([A-Z]{2,6}(?:-\d{2,4})*-\d{1,4})\b")
_AMOUNT = r"[£$€]?\s?\d[\d.,]*\s?(?:bn|m|k|million|billion)?"
_REVISED = re.compile(rf"(?i)\b(P50|P90|P99)\s+(?:was\s+)?(?:raised|revised|increased|reduced|lowered|cut|changed|"
                      rf"updated|moved)\s+from\s+({_AMOUNT})\s+to\s+({_AMOUNT})")
_UNCHANGED_AT = re.compile(rf"(?i)\b(P50|P90|P99)\s+(?:unchanged|confirmed|kept|held)\s+at\s+({_AMOUNT})")
_FREQUENCY = re.compile(r"(?i)\bfrequency\s+(?:(?:raised|revised|changed|updated|moved)\s+from\s+\S+\s+to\s+)?"
                        r"(1[\s-]*in[\s-]*\d+(?:\s*(?:years?|yrs?))?)")


def extract_scenario_text(result: DocumentResult) -> list[Candidate]:
    """Scenario decisions in minutes: a 'Scenario 22 - RTGS / CHAPS outage' heading section whose text
    states 'P90 raised from £30m to £34m', 'P99 unchanged at £95m' or 'Frequency 1-in-20'.

    The decided (new) value is staged; the merge with the register then raises any
    difference as a conflict for the reviewer. Values come only from those explicit phrases."""
    blocks = [b for b in result.document.blocks if b.locator not in result.excluded_blocks]
    out = []
    for i, block in enumerate(blocks):
        m = _SCENARIO_HEADING.match(block.text) if block.kind == "heading" else None
        if not m:
            continue
        body = []
        for later in blocks[i + 1:]:
            if later.kind == "heading":
                break
            body.append(later)
        raw: dict[str, str] = {"title": m[2]}
        spans, cells = {"title": block.text}, {"title": block.locator}
        for b in body:
            if "org_ref" not in raw:
                # Only a reference that carries the heading's scenario number ('Scenario 22' -> 'SCN-22');
                # other refs in the text (action or workshop ids) are not the scenario's.
                ref = next((r for r in _SCENARIO_REF.finditer(b.text)
                            if int(re.findall(r"\d+", r[1])[-1]) == int(m[1])), None)
                if ref:
                    raw["org_ref"], spans["org_ref"], cells["org_ref"] = ref[1], ref[0], b.locator
            for found in _REVISED.finditer(b.text):
                field = f"severity_{found[1].lower()}"
                raw.setdefault(field, found[3].strip())
                spans.setdefault(field, found[0])
                cells.setdefault(field, b.locator)
            for found in _UNCHANGED_AT.finditer(b.text):
                field = f"severity_{found[1].lower()}"
                raw.setdefault(field, found[2].strip())
                spans.setdefault(field, found[0])
                cells.setdefault(field, b.locator)
            if "frequency_text" not in raw and (freq := _FREQUENCY.search(b.text)):
                raw["frequency_text"], spans["frequency_text"], cells["frequency_text"] = freq[1], freq[0], b.locator
        for data, meta, notes in assemble("org_scenarios", raw):
            out.append(Candidate("org_scenarios", data, [Provenance(
                result.entry.path, block.locator, f"{POD}:org_scenarios:minutes", "deterministic",
                spans=spans, cells=cells)], meta=meta, warnings=notes))
    return out


NARRATIVE_DETERMINISTIC = {"actions": extract_actions_text, "observations": extract_observation_text,
                           "exercises": extract_exercise_text, "org_scenarios": extract_scenario_text}


def collect_reference_names(result: DocumentResult) -> list[dict[str, str]]:
    """(service reference, service name) pairs from any table with both columns, exact headers only.

    The linker turns a name into an alias only when the reference already
    identifies exactly one service; nothing here decides a link."""
    from .synonyms import field_score

    hints = []
    for index, table in enumerate(result.document.tables):
        excluded = result.excluded_columns.get(index, set())
        ref_cols = [c.index for c in table.columns if c.index not in excluded
                    and field_score("services", "org_ref", c.label) == 3
                    and normalize_ref_header(c.label)]
        name_cols = [c.index for c in table.columns if c.index not in excluded
                     and field_score("services", "name", c.label) == 3]
        if len(ref_cols) != 1 or len(name_cols) != 1:
            continue
        seen = set()
        for row in table.rows:
            ref, name = coerce.clean(row.values[ref_cols[0]]), coerce.clean(row.values[name_cols[0]])
            if ref and name and (ref, name) not in seen:
                seen.add((ref, name))
                hints.append({"ref": ref, "name": name, "file": result.entry.path, "locator": row.locator})
    return hints


def normalize_ref_header(label: str) -> bool:
    """Generic 'ref'/'id'/'number' headers are too vague to key service names on."""
    from .synonyms import normalize_header

    return normalize_header(label) not in ("ref", "id", "number")


_CLOSURE_LINE = re.compile(r"(?im)^\s*(?:action\s+)?([A-Z]{1,6}-?\d{1,4})\s+closure evidence\s*[:\-–]\s*(.+?)\s*$")


def attach_closure_evidence(result: DocumentResult) -> None:
    """'R-05 closure evidence: training records ...' attaches to action R-05 of the same document."""
    actions = {coerce.clean(c.meta.get("org_ref", "")).casefold(): c for c in result.candidates
               if c.kind == "actions" and c.meta.get("org_ref")}
    if not actions:
        return
    for block in result.document.blocks:
        if block.locator in result.excluded_blocks:
            continue
        for m in _CLOSURE_LINE.finditer(block.text):
            target = actions.get(m[1].casefold())
            if target is not None and not target.data.get("closure_evidence"):
                target.data["closure_evidence"] = [m[2]]
                target.provenance[0].cells["closure_evidence"] = block.locator
                target.provenance[0].spans["closure_evidence"] = m[0].strip()


def extract_document(result: DocumentResult, *, llm: Any = None) -> None:
    if result.document is None or not result.extract:
        return
    result.alias_hints = collect_reference_names(result)
    _extract_routes(result, llm)
    attach_closure_evidence(result)


def _extract_routes(result: DocumentResult, llm: Any) -> None:
    for route in result.routes:
        if route.route in ("ignore", "quarantine"):
            continue
        if route.target.startswith("table:"):
            table = result.document.tables[int(route.target.split(":")[1])]
            result.candidates.extend(extract_table(result, route, table))
            continue
        if route.target == "sections":
            from .sections import extract_sections

            found = extract_sections(result, route)
        else:
            found = NARRATIVE_DETERMINISTIC[route.route](result) if route.route in NARRATIVE_DETERMINISTIC else []
        result.candidates.extend(found)
        if llm is not None and not found:
            candidates, issues = llm.extract(result, route.route)
            result.candidates.extend(candidates)
            result.issues.extend(issues)
        elif not found:
            result.issues.append(Issue(
                "info", "not_extracted",
                f"narrative {route.route} content was routed but not extracted (enable the LLM lane)",
                result.entry.path))
