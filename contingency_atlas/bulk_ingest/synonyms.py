"""Header synonym tables per contract route, and header normalization.

Field names on the left are extraction fields. Most equal the target contract
field; a few (``service``, ``rto``, ``rpo``, ``org_ref``) are intermediate
fields the extractor turns into contract fields after linking or coercion.
"""

from __future__ import annotations

import re
import unicodedata

FIELD_SYNONYMS: dict[str, dict[str, tuple[str, ...]]] = {
    "services": {
        "name": ("service", "service name", "business service", "important business service",
                 "ibs", "ibs name", "service title", "application service", "critical service"),
        "org_ref": ("service id", "service ref", "service reference", "service code", "ibs id", "ibs number",
                    "ibs reference", "ibs ref", "service number", "number", "ref", "id"),
        "owner": ("owner", "service owner", "accountable owner", "business owner",
                  "accountable executive", "accountable", "smf owner"),
        "criticality": ("criticality", "tier", "service tier", "importance", "criticality tier",
                        "classification tier", "priority"),
        "impact_tolerance": ("impact tolerance", "tolerance", "max tolerable disruption",
                             "maximum tolerable duration", "max tolerable duration",
                             "maximum tolerable period of disruption", "mtpd", "mtd", "tolerance statement"),
        "rto": ("rto", "recovery time objective", "recovery time", "rto hours", "target recovery time"),
        "rpo": ("rpo", "recovery point objective", "recovery point"),
        "legal_entity": ("legal entity", "entity", "booking entity"),
        "region": ("region", "geography", "market", "country"),
        "process_names": ("processes", "process", "key processes", "supporting processes"),
        "source_refs": ("references", "refs", "source refs", "documents"),
        "notes": ("notes", "comments", "remarks", "description"),
    },
    "dependencies": {
        "name": ("dependency", "dependency name", "arrangement", "contract name", "arrangement name"),
        "service": ("supported service", "supported services", "dependent service", "business service",
                    "business services", "consuming service", "service", "services supported",
                    "supports service", "impacted services", "from service", "supports ibs", "supported ibs",
                    "linked ibs", "ibs supported", "ibs"),
        "to_service": ("depends on service", "upstream service", "to service", "internal provider service"),
        "target": ("name", "depends on", "dependency on", "component", "provider or service"),
        "dependency_type": ("type", "dependency type", "kind", "dependency kind"),
        "direction": ("direction", "flow"),
        "external_provider": ("vendor", "supplier", "provider", "third party", "third-party",
                              "vendor name", "supplier name", "provider name", "third party name",
                              "counterparty", "outsourcer"),
        "category": ("category", "service provided", "services provided", "type of service", "service type",
                     "dependency category"),
        "common_cause": ("common cause", "concentration", "shared dependency", "concentration risk"),
        "supplier_external_id": ("vendor id", "supplier id", "vendor ref", "supplier ref", "contract ref",
                                 "contract reference", "contract id"),
        "notes": ("notes", "comments", "what is needed", "purpose"),
    },
    "mechanisms": {
        "name": ("mechanism", "mechanism name", "failure mode", "failure mechanism", "threat"),
        "description": ("description", "mechanism description", "how it fails", "details"),
        "org_ref": ("mechanism id", "mechanism ref", "failure mode id", "failure mode ref", "ref", "id"),
    },
    "controls": {
        "name": ("control", "control name", "control title", "control description", "key control"),
        "org_ref": ("control id", "control ref", "control reference", "ctl id", "ref", "id"),
        "owner": ("control owner", "owner", "accountable owner", "performer"),
        "service": ("service", "services", "business service", "applies to", "in scope services",
                    "related service", "service in scope", "linked ibs", "ibs"),
        "mechanism": ("mechanism", "failure mode", "mitigates"),
        "validation_status": ("test result", "last test result", "validation status", "test status",
                              "effectiveness", "design effectiveness", "operating effectiveness", "status",
                              "testing status"),
        "validated_on": ("last tested", "test date", "validated on", "last validated", "date tested",
                         "tested on", "last test date"),
        "evidence_refs": ("evidence", "test evidence", "evidence ref", "evidence reference", "evidence link",
                          "test evidence ref"),
        "valid_until": ("valid until", "next test due", "retest due"),
    },
    "assumptions": {
        "statement": ("assumption", "assumption statement", "planning assumption", "statement"),
        "owner": ("owner", "assumption owner"),
        "service": ("service", "services", "business service"),
        "expires_on": ("expires", "expiry", "expires on", "review by", "review date", "valid to", "validate by",
                       "validate before"),
        "test": ("test", "how to test", "validation test", "test method", "how tested", "test approach",
                 "how it will be tested", "how it will be validated", "validation method"),
        "status": ("status", "assumption status"),
        "evidence_refs": ("evidence", "evidence ref"),
        "org_ref": ("assumption id", "assumption ref", "ref", "id"),
        "scenario": ("scenario", "scenario ref", "linked scenario", "related scenario"),
    },
    "actions": {
        "title": ("action", "action title", "remediation action", "action description", "finding",
                  "issue", "recommendation", "management action", "remediation"),
        "org_ref": ("action id", "action ref", "finding id", "issue id", "ref", "id"),
        "owner": ("owner", "action owner", "assigned to", "accountable", "responsible"),
        "due_date": ("due", "due date", "target date", "deadline", "target completion", "agreed date"),
        "status": ("status", "action status", "rag", "progress"),
        "service": ("service", "services", "business service", "affected service"),
        "closure_evidence": ("closure evidence", "evidence of closure", "closure note"),
        "rationale": ("rationale", "commentary"),
        "scenario": ("scenario", "scenario ref", "linked scenario", "related scenario"),
    },
    "exercises": {
        "title": ("exercise", "exercise name", "exercise title", "test name", "simulation", "title",
                  "test title"),
        "scenario": ("scenario", "scenario ref", "scenario id", "linked scenario", "scenarios"),
        "service": ("service", "services", "services in scope", "business service"),
        "injects": ("injects", "inject", "scenario injects"),
        "outcomes": ("outcomes", "outcome", "findings", "result", "results", "lessons"),
        "status": ("status", "exercise status"),
        "completed_on": ("date", "exercise date", "completed on", "date held", "run date"),
        "evidence_refs": ("evidence", "report", "report ref", "evidence ref"),
    },
    "observations": {
        "title": ("observation", "finding", "incident", "incident title", "title", "issue title",
                  "vulnerability", "weakness", "control gap"),
        "service": ("service", "services", "affected service", "impacted service", "business service",
                    "services affected", "linked ibs", "ibs"),
        "observed_on": ("observed on", "date", "incident date", "date observed", "event date"),
        "description": ("description", "summary", "details", "detail", "what happened"),
        "evidence_refs": ("evidence", "reference", "report ref", "ticket"),
        "org_ref": ("observation id", "finding id", "finding ref", "ref", "id"),
        "mechanism": ("failure mode", "mechanism"),
        "scenario": ("scenario", "scenario ref", "linked scenario", "related scenario"),
    },
    "impact_assessments": {
        "title": ("assessment", "impact assessment", "title"),
        "service": ("service", "business service"),
        "observed_on": ("date", "assessed on", "observed on"),
        "time_since_onset_minutes": ("time since onset", "minutes since onset", "elapsed minutes",
                                     "time since disruption", "time since incident", "elapsed time"),
        "downtime_minutes": ("downtime", "downtime minutes", "outage minutes", "outage duration"),
        "customers": ("customers", "customers affected", "customers impacted", "customers unable to pay",
                      "customers unable to transact"),
        "backlog": ("backlog", "payments backlog", "backlog count", "queued items"),
        "capacity_loss": ("capacity loss", "capacity loss percent"),
        "tolerance_reference": ("tolerance reference", "impact tolerance"),
    },
    "evidence_events": {
        "headline": ("headline", "event", "event title", "title", "incident"),
        "organization": ("organization", "organisation", "firm", "company", "entity name", "affected firm",
                         "entity"),
        "source_url": ("source url", "url", "link", "source link", "citation url"),
        "source_date": ("source date", "date", "published", "publication date", "event date"),
        "source_publisher": ("publisher", "source publisher", "source", "citation publisher"),
        "source_title": ("source title", "article title", "citation title"),
        "loss_amount_usd": ("loss usd", "loss amount usd", "loss amount", "loss", "reported loss"),
        "summary": ("summary", "description", "independently authored summary", "notes"),
        "country": ("country", "entity country"),
        "region": ("region", "entity region"),
        "basel_event_type": ("basel event type", "event type", "basel et", "et"),
    },
    "internal_loss_events": {
        "org_ref": ("loss id", "event id", "loss ref", "incident id", "loss reference", "event ref", "nm ref",
                    "near miss ref", "ref", "id"),
        "title": ("event title", "loss title", "event name", "title"),
        "basel_event_type": ("basel event type", "basel et", "basel category", "event type", "et",
                             "basel level 1"),
        "org_event_type_code": ("internal event type", "org event type", "event type code",
                                "event sub type", "sub type", "local event type", "acme event type"),
        "business_line": ("business line", "bl", "division", "business unit"),
        "occurrence_date": ("occurrence date", "date of occurrence", "occurred", "occurred on",
                            "event date", "date occurred"),
        "discovery_date": ("discovery date", "date discovered", "detected", "discovered", "detection date",
                           "date identified", "identified"),
        "accounting_date": ("accounting date", "booking date", "posted", "posting date", "date booked"),
        "gross_loss": ("gross loss", "gross amount", "gross", "gross loss amount"),
        "recoveries": ("recoveries", "recovery", "recovered", "total recoveries", "insurance recovery"),
        "other_recoveries": ("other recovery", "other recoveries"),
        "potential_impact": ("potential impact", "potential loss", "estimated impact", "potential exposure"),
        "units": ("units", "unit", "scale", "amount units"),
        "net_loss": ("net loss", "net", "net amount", "net loss amount"),
        "currency": ("currency", "ccy"),
        "status": ("status", "event status"),
        "near_miss": ("near miss", "near-miss", "near miss flag"),
        "boundary": ("boundary", "boundary event", "credit boundary", "market boundary", "boundary flag"),
        "description": ("description", "event description", "summary", "narrative", "what happened"),
        "root_cause": ("root cause", "cause", "causal factor"),
        "service": ("service", "affected service", "impacted service", "business service", "services",
                    "linked service", "linked services", "linked ibs"),
        "legal_entity": ("legal entity", "booking entity", "entity"),
        "incident_ref": ("incident ref", "incident reference", "incident id", "incident number", "inc ref"),
        "scenario": ("scenario", "scenario ref", "linked scenario", "scenario id", "scenario reference",
                     "related scenario"),
    },
    "org_scenarios": {
        "org_ref": ("scenario id", "scenario ref", "scenario reference", "ref", "id"),
        "title": ("scenario", "scenario title", "scenario name", "title"),
        "narrative": ("narrative", "scenario narrative", "description", "scenario description"),
        "owner": ("owner", "scenario owner", "risk owner"),
        "org_taxonomy_code": ("taxonomy code", "risk taxonomy", "risk category code", "taxonomy",
                              "risk code", "l2 code", "risk category", "nfr code", "taxonomy ref"),
        "frequency_text": ("frequency", "likelihood", "return period", "estimated frequency"),
        "severity_p50": ("p50", "median", "typical", "severity p50", "p50 severity"),
        "severity_p90": ("p90", "severity p90", "p90 severity"),
        "severity_p99": ("p99", "severity p99", "p99 severity", "severe", "1 in 100", "extreme"),
        "severity_currency": ("currency", "ccy"),
        "units": ("units", "unit", "scale", "amount units"),
        "last_workshop": ("last workshop", "workshop date", "last reviewed", "last review", "review date"),
        "basel_event_type": ("basel et", "basel event type", "basel category", "basel level 1"),
        "status": ("status", "scenario status"),
        "service": ("service", "services", "linked services", "affected services", "business services",
                    "linked ibs", "ibs"),
    },
    "taxonomy_crosswalk": {
        "org_code": ("code", "taxonomy code", "risk code", "node id", "category code", "id"),
        "org_label": ("label", "name", "risk name", "category", "node", "risk category", "title"),
        "level": ("level", "tier", "taxonomy level"),
        "parent_code": ("parent", "parent code", "parent id"),
        "definition": ("definition", "description"),
        **{f"l{n}_code": (f"l{n} code", f"level {n} code", f"l{n} id") for n in range(1, 5)},
        **{f"l{n}_name": (f"l{n} name", f"level {n} name", f"l{n} label", f"l{n}") for n in range(1, 5)},
    },
}

# A table qualifies for a route only when every anchor group has a hit.
ANCHORS: dict[str, tuple[tuple[str, ...], ...]] = {
    "services": (("name",), ("criticality", "impact_tolerance", "rto", "rpo")),
    "dependencies": (("external_provider", "to_service", "target"),
                     ("service", "category", "supplier_external_id", "dependency_type")),
    "mechanisms": (("name",), ("description",)),
    "controls": (("name",), ("owner", "validation_status", "validated_on", "service")),
    "assumptions": (("statement",), ("expires_on", "test")),
    "actions": (("title",), ("due_date",), ("owner", "status")),
    "exercises": (("title",), ("completed_on", "injects", "outcomes")),
    "observations": (("title",), ("observed_on", "description", "service")),
    "impact_assessments": (("time_since_onset_minutes", "downtime_minutes"),
                           ("customers", "backlog", "capacity_loss", "downtime_minutes")),
    "evidence_events": (("source_url",), ("headline", "summary")),
    "internal_loss_events": (("gross_loss", "net_loss", "potential_impact"),
                             ("org_ref", "occurrence_date", "discovery_date", "accounting_date")),
    "org_scenarios": (("title",), ("severity_p50", "severity_p90", "severity_p99", "frequency_text")),
    "taxonomy_crosswalk": (("org_code", "l1_code"), ("org_label", "l1_name"), ("level", "parent_code", "l2_code")),
}

# Field weights; anchors are worth more than incidental hits.
STRONG_FIELDS = {
    "services": {"impact_tolerance", "rto", "rpo", "criticality"},
    "dependencies": {"external_provider"},
    "controls": {"validation_status", "validated_on"},
    "actions": {"due_date"},
    "internal_loss_events": {"gross_loss", "net_loss", "recoveries", "basel_event_type", "discovery_date"},
    "org_scenarios": {"severity_p99", "severity_p90", "severity_p50", "frequency_text"},
    "evidence_events": {"source_url"},
    "taxonomy_crosswalk": {"parent_code", "level", "l1_code", "l2_code"},
    "impact_assessments": {"time_since_onset_minutes", "customers", "backlog"},
    "exercises": {"injects", "outcomes"},
    "assumptions": {"expires_on", "test"},
}

# Column headers that identify personal data; such columns are never staged.
PERSONAL_HEADERS = frozenset({
    "employee", "employee name", "full name", "first name", "forename", "given name", "surname",
    "last name", "family name", "staff name", "name of employee", "date of birth", "dob",
    "home address", "personal email", "email", "email address", "e mail", "mobile",
    "mobile number", "mobile phone", "phone", "phone number", "telephone", "tel",
    "national insurance", "national insurance number", "ni number", "nino", "ssn",
    "social security number", "salary", "next of kin", "passport number", "personal phone",
    "contact email", "contact phone", "contact name", "home phone", "employee id", "staff id",
    "payroll number", "bank account", "sort code", "iban",
})

_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def normalize_header(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold().replace("&", " and ")
    text = _PAREN.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _phrase_in(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


# A contained synonym does not name the column when the header adds one of these
# attribute words: "Dependency RTO" is a recovery time, not the dependency's name.
_ATTRIBUTE_TOKENS = frozenset({"rto", "rpo", "mtd", "date", "id", "ref", "reference", "number", "no", "code",
                               "owner", "status", "type", "count", "amount", "tier", "level", "criticality",
                               "version", "url"})


def _contains(syn: str, text: str) -> bool:
    """Contained-phrase match with the attribute-word and decimal guards."""
    if not _phrase_in(syn, text):
        return False
    if re.search(r"\d", syn) and re.search(rf"(?<![a-z0-9]){re.escape(syn)} \d", text):
        return False  # 'p99' is not 'p99 9' (P99.9)
    rest = list(text.split())
    for token in syn.split():
        if token in rest:
            rest.remove(token)
    return not any(t in _ATTRIBUTE_TOKENS for t in rest if t not in syn.split())


def field_score(route: str, field: str, label: str, group: str = "") -> int:
    """3 exact child label, 2 exact combined, 1 contained phrase (len>=4)."""
    if len(label) > 60:
        return 0  # long cell text is content, not a header
    label_n, group_n = normalize_header(label), normalize_header(group)
    combined = f"{group_n} {label_n}".strip()
    best = 0
    for syn in FIELD_SYNONYMS[route][field]:
        if label_n == syn:
            return 3
        if combined == syn:
            best = max(best, 2)
        elif (len(syn) >= 4 or (len(syn) >= 3 and re.search(r"\d", syn))) and (
                _contains(syn, label_n) or (group_n and _contains(syn, combined))):
            best = max(best, 1)
    return best


def map_columns(route: str, columns: list[tuple[int, str, str]],
                excluded: set[int] | None = None) -> dict[str, int]:
    """Greedy one-to-one header -> field mapping for one route."""
    excluded = excluded or set()
    scored = []
    for field in FIELD_SYNONYMS[route]:
        for index, label, group in columns:
            if index in excluded:
                continue
            score = field_score(route, field, label, group)
            if score:
                scored.append((-score, index, list(FIELD_SYNONYMS[route]).index(field), field))
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for _, index, _, field in sorted(scored):
        if field not in mapping and index not in used:
            mapping[field] = index
            used.add(index)
    return mapping


def header_hits(label: str) -> int:
    """How many routes recognise this header; used for header-row detection."""
    norm = normalize_header(label)
    if not norm:
        return 0
    return sum(any(norm == syn for syn in fields_syns) for fields in FIELD_SYNONYMS.values()
               for fields_syns in fields.values())


def is_personal_header(label: str) -> bool:
    return normalize_header(label) in PERSONAL_HEADERS
