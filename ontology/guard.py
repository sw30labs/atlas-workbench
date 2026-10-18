#!/usr/bin/env python3
"""
guard.py — two-gate validator (instantiated from the ontologist skill template).

    producer (LLM/scraper/pipeline)  ->  GATE 1: Pydantic at the door
                                     ->  projection to RDF (staging)
                                     ->  inference (OWL-RL: sameAs, chains)
                                     ->  GATE 2: SHACL at the ledger
                                     ->  ADMIT / AUTO_MERGE / FLAG / REJECT / REJECTED_AT_DOOR

The producer proposes. The ontology permits. Nothing touches the store until it
has passed both gates.

TEMPLATE STRUCTURE — two blocks:
  GENERATED: namespaces, record models, projection. Regenerate per project.
             The shipped block is the Contingency Atlas implementation.
  FIXED:     orchestration, inference harvest, SHACL runner, verdicts, outputs.
             Do not modify. If a project seems to need changes here, the need
             is almost always expressible in the GENERATED block or the TTLs.

Inference note (see the skill's footguns.md #1): the OWL closure runs on a
scratch copy; only trusted inferences (owl:sameAs identity, property-chain
shortcuts) are injected into the validation graph. Domain/range typing is NOT
materialized before validation — that would type mistakes into correctness.

Usage:
    python guard.py fixtures/candidates.json [--existing existing.ttl] [--out out]
Exit code 1 if anything was rejected (CI/Makefile-friendly).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal as Lit, Optional, Union

from pydantic import (BaseModel, ConfigDict, Field, StringConstraints,
                      TypeAdapter, ValidationError, field_validator)
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, SKOS, XSD

# ===========================================================================
# GENERATED BLOCK — regenerate per project (models + projection).
# ===========================================================================

NS = Namespace("https://contingency-atlas.local/ontology#")  # ontology namespace
DATA = Namespace("https://contingency-atlas.local/id/")      # instance namespace
CAT = Namespace("https://contingency-atlas.local/ontology/cat/")

# Object properties whose *derived* edges are worth showing in the report
# (materialized by property chains in domain.ttl; never asserted directly).
DERIVED_DISPLAY_PROPS = [NS.resolvesToRisk]

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_RISK_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EVT_REF_RE = re.compile(r"^(EVT|DEMO-EVT)(-[A-Za-z0-9]+)+$")
_URL_RE = re.compile(r"^https?://.+$")  # minimal shape; deep publicness checks live in provenance.py
_SLUG_RE = re.compile(r"[^a-z0-9]+")

from urllib.parse import quote as _urlquote  # noqa: E402 — GENERATED-block local


def _url_to_iri(url: str) -> URIRef:
    """IRI-safe form of a stored URL (percent-encodes spaces etc.). Identity
    works on this canonical form: '.../a b.pdf' and '.../a%20b.pdf' are the
    same node. For example, hypothetical URLs that differ only by an encoded
    space resolve to the same node."""
    return URIRef(_urlquote(url, safe=":/?#[]@!$&'()*+,;=%"))

# Closed vocabularies mirrored from the codebase (single sources of truth:
# categories.py, scoring/models.py, phase prompts). The door enforces them
# on write; shapes.ttl mirrors them for legacy rows and other producers.
EXTERNAL_DRIVERS = ("Technology", "Institutional change", "Demographic change",
                    "Environment", "Societal values", "Cross-driver convergence")
RISK_CATEGORIES = ("Financial crime controls", "Third-party dependency",
                   "Digital resilience", "Model risk", "Data governance",
                   "Processing integrity", "Workforce and conduct",
                   "Physical resilience", "Legal and regulatory",
                   "Market infrastructure", "Geopolitical disruption",
                   "Multi-risk convergence")
EVENT_DRIVER_CLASSES = ("Technology disruption", "Third-party failure",
                        "Process failure", "Human action", "External disruption",
                        "Physical disruption", "Model failure", "Data integrity",
                        "Cross-domain convergence")
SCENARIO_ORIGINS = ("operator_authored", "pipeline_generated",
                    "compound_graduation", "synthetic_fixture",
                    "legacy_unknown")


def _slugify(title: str) -> str:
    """LOSSLESS slug: the whole title, lowercased, non-alnum runs to hyphens.
    Kept in step with scoring/inventory.py:slugify, which is now lossless too
    (its 8-word cap was the C-02 dangling-reference bug). The guard carries its
    own copy because it must run standalone, without the package importable."""
    return _SLUG_RE.sub("-", title.strip().lower()).strip("-")


def _normalized_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().lower()


def _normalized_category(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


def seed_risk_category_scheme(graph: Graph) -> None:
    """Seed stable C-04 reference data into a validation data graph.

    Canonical categories never depend on a runtime-store snapshot. Reviewed
    aliases are loaded from the shipped project map when present; invalid
    targets are ignored rather than creating an unrecognized concept.
    """
    aliases: dict[str, list[str]] = {}
    alias_path = Path(__file__).with_name("category-map.json")
    try:
        raw_map = json.loads(alias_path.read_text()).get("map", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        raw_map = {}
    if isinstance(raw_map, dict):
        for raw_label, target in raw_map.items():
            if target in RISK_CATEGORIES:
                aliases.setdefault(str(target), []).append(str(raw_label))

    scheme = CAT["risk-categories"]
    graph.add((scheme, RDF.type, SKOS.ConceptScheme))
    for label in RISK_CATEGORIES:
        concept = CAT[_normalized_category(label)]
        graph.add((concept, RDF.type, SKOS.Concept))
        graph.add((concept, SKOS.prefLabel, Literal(label)))
        graph.add((concept, SKOS.inScheme, scheme))
        graph.add(
            (concept, NS.normalizedLabel, Literal(_normalized_category(label)))
        )
        for alias in sorted(aliases.get(label, [])):
            graph.add((concept, SKOS.altLabel, Literal(alias)))
            graph.add(
                (
                    concept,
                    NS.normalizedLabel,
                    Literal(_normalized_category(alias)),
                )
            )


def _parse_dt(v: object, *, date_only: bool = False) -> tuple:
    """Parse a stored date/datetime -> (value, drift).

    Canonical forms (C-05): datetime fields = ISO-8601 with timezone;
    date_only fields = bare YYYY-MM-DD. Other parseable forms are accepted
    but marked drift=True (a ledger Warning, not a reject). Unparseable
    input raises — the door does not negotiate with free text."""
    if v is None or isinstance(v, (date, datetime)):
        return v, False
    s = str(v).strip()
    if not s:
        return None, False
    if date_only:
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
            if s == d.strftime("%Y-%m-%d"):
                return d, False
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(s).date(), True
        except ValueError:
            raise ValueError(f"unparseable date {s!r} — expected YYYY-MM-DD")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"unparseable datetime {s!r} — expected ISO-8601 with timezone")
    return dt, dt.tzinfo is None  # naive (incl. bare date) = drift


def _empty_to_none(v):
    """Legacy rows store '' in enum fields; absent is allowed, junk is not."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


class _Rec(BaseModel):
    # extra="ignore": producers send full register/db records (20-55 fields);
    # the door constrains the modeled subset and ignores the rest.
    model_config = ConfigDict(extra="ignore")
    id: NonEmpty


class RiskRec(_Rec):
    kind: Lit["risk"]
    issue: NonEmpty
    status: Lit["active", "monitoring", "faded"] = "monitoring"
    review_status: Lit["pending", "approved", "rejected", "needs_update",
                       "superseded", "approved_synthetic"] = "pending"
    trend: Optional[Lit["rising", "stable", "fading"]] = None
    signal_maturity: Optional[Lit["weak", "emerging", "accelerating",
                                  "established", "materialized", "fading"]] = None
    momentum: Optional[Lit["accelerating", "stable", "slowing"]] = None
    evidence_strength: Optional[Lit["unrated", "limited", "moderate", "strong"]] = None
    source_type: Optional[str] = None
    external_drivers: list[Lit[EXTERNAL_DRIVERS]] = []
    evidence_refs: list[str] = []
    first_seen: Optional[str] = None
    last_reviewed: Optional[str] = None
    related_scenarios: list[str] = []

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v):
        if not _RISK_ID_RE.match(v):
            raise ValueError(
                f"risk id {v!r} is not a slug id (C-03): lowercase alphanumerics "
                "separated by single hyphens, as minted from the title"
            )
        return v

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _typed_refs(cls, v):
        # C-10 door half: every ref is a project event id XOR an http(s) URL.
        if v is None:
            return []
        out = []
        for r in v:
            s = str(r).strip()
            if _EVT_REF_RE.match(s) or _URL_RE.match(s):
                out.append(s)
            else:
                raise ValueError(
                    f"evidence ref {s!r} is neither an event id nor an http(s) URL (C-10)")
        return out

    @field_validator("first_seen", "last_reviewed", mode="before")
    @classmethod
    def _dates_parseable(cls, v):
        if v is None or not str(v).strip():
            return None
        s = str(v).strip()
        _parse_dt(s)  # raises on free text; drift is decided at projection
        return s


class ScenarioRec(_Rec):
    kind: Lit["scenario"]
    title: NonEmpty
    origin: Lit[SCENARIO_ORIGINS] = "legacy_unknown"
    status: Lit["draft", "active", "retired"] = "draft"
    risk_category: Optional[Lit[RISK_CATEGORIES]] = None
    likelihood: Optional[Lit["Very Low", "Low", "Moderate", "High", "Very High"]] = None
    impact_band: Optional[Lit["Critical", "High", "Medium", "Low"]] = None
    financial_impact_band: Optional[Lit["Extreme", "Major", "Significant",
                                        "Moderate", "Limited"]] = None
    controllability: Optional[Lit["internal", "mixed", "external"]] = None
    signpost_status: Optional[Lit["not_evaluated", "strengthened", "unchanged",
                                  "weakened", "materialized", "superseded"]] = None
    event_driver_class: Optional[Lit[EVENT_DRIVER_CLASSES]] = None

    _enums_empty_to_none = field_validator(
        "risk_category", "likelihood", "impact_band", "financial_impact_band",
        "controllability", "signpost_status", "event_driver_class",
        mode="before")(_empty_to_none)


class EvidenceEventRec(_Rec):
    kind: Lit["evidence_event"]
    headline: NonEmpty
    risk_category: Optional[str] = None  # free-form as stored; C-04 maps at the ledger
    source_url: Optional[str] = None
    source_date: Optional[str] = None
    retrieved_at: Optional[str] = None
    provenance_class: Optional[Lit["public_primary", "public_secondary", "synthetic"]] = None
    review_status: Lit["pending", "approved", "rejected",
                       "approved_synthetic", "needs_update"] = "pending"
    record_type: Lit["event"] = "event"

    @field_validator("risk_category", "source_url", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        return _empty_to_none(v)

    @field_validator("source_url")
    @classmethod
    def _url_shape(cls, v):
        if v is not None and not _URL_RE.match(v):
            raise ValueError(f"source_url {v!r} is not an http(s) URL")
        return v

    @field_validator("source_date", mode="before")
    @classmethod
    def _source_date_parseable(cls, v):
        if v is None or not str(v).strip():
            return None
        s = str(v).strip()
        _parse_dt(s, date_only=True)
        return s

    @field_validator("retrieved_at", mode="before")
    @classmethod
    def _retrieved_parseable(cls, v):
        if v is None or not str(v).strip():
            return None
        s = str(v).strip()
        _parse_dt(s)
        return s


class CompoundRec(_Rec):
    kind: Lit["compound"]
    pair_id: NonEmpty
    run_id: NonEmpty
    risk_ids: list[NonEmpty] = Field(min_length=2)
    p_hat: float = Field(ge=0.0, le=1.0)
    uncertainty_class: Lit["statistical", "mixed", "judgment_dominant"]
    loss_units: Lit["activation", "dollars"]
    status: Lit["candidate", "superseded"]
    data_regime: Optional[str] = None


class CompoundRunRec(_Rec):
    kind: Lit["compound_run"]
    status: Lit["ok", "insufficient_data", "skipped_not_due", "error"]


class ChannelProposalRec(_Rec):
    kind: Lit["channel_proposal"]
    proposal_kind: Lit["tag_add", "edge_add", "new_channel"]
    status: Lit["pending", "approved", "rejected", "auto_rejected"]
    citation_status: Lit["unverified", "resolved", "unresolvable"] = "unverified"
    channel: Optional[str] = None
    risk_ids: list[str] = []
    reviewer: Optional[str] = None
    reason: Optional[str] = None
    graph_version_applied: Optional[int] = None

    _blank_to_none = field_validator("channel", "reviewer", "reason",
                                     mode="before")(_empty_to_none)


class MappingProposalRec(_Rec):
    kind: Lit["mapping_proposal"]
    framework: Lit["mitre-atlas"]
    scenario_id: NonEmpty
    content_version: NonEmpty
    technique_id: NonEmpty
    applicability: Lit["hypothesized"]
    origin: Lit["manual"]
    status: Lit["pending", "approved", "rejected"]
    reviewer: str | None = None
    reason: str | None = None
    graph_version_applied: int | None = None

    _blank_to_none = field_validator(
        "reviewer", "reason", mode="before"
    )(_empty_to_none)


Candidate = Annotated[
    Union[RiskRec, ScenarioRec, EvidenceEventRec, CompoundRec, CompoundRunRec,
          ChannelProposalRec, MappingProposalRec],
    Field(discriminator="kind"),
]
_ADAPTER: TypeAdapter = TypeAdapter(Candidate)


def project(rec, g: Graph) -> tuple[URIRef, list]:
    """Candidates -> staged RDF. Mechanical, no cleverness: ids map to IRIs
    verbatim, so a wrong reference stays wrong for the ledger to catch.
    Normalization (slugs, titles, categories, date drift) happens HERE, at
    the door — ledger SPARQL stays a plain join (footgun #7)."""
    iri = DATA[rec.id]
    t: list = []

    def add(s, p, o):
        t.append((s, p, o))
        g.add((s, p, o))

    if rec.kind == "risk":
        add(iri, RDF.type, NS.EmergingRisk)
        slug = DATA[f"slug/{_slugify(rec.issue)}"]
        add(iri, NS.slugId, slug)
        add(slug, NS.slugFor, iri)  # keeps the resolvesToRisk chain forward-only
        add(iri, NS.normalizedTitle, Literal(_normalized_title(rec.issue)))
        add(iri, NS.riskStatus, Literal(rec.status))
        add(iri, NS.reviewStatus, Literal(rec.review_status))
        for ref in rec.evidence_refs:
            add(iri, NS.evidenceRef, _url_to_iri(ref) if _URL_RE.match(ref) else DATA[ref])
        for prop, raw in ((NS.firstSeen, rec.first_seen),
                          (NS.lastReviewed, rec.last_reviewed)):
            dt, drift = _parse_dt(raw)
            if dt:
                add(iri, prop, Literal(dt, datatype=XSD.dateTime))
            if drift:
                add(iri, NS.formatDrift, Literal(str(prop).rsplit("#", 1)[-1]))
    elif rec.kind == "scenario":
        add(iri, RDF.type, NS.Scenario)
        add(iri, NS.origin, Literal(rec.origin))
        add(iri, NS.scenarioStatus, Literal(rec.status))
        for prop, val in ((NS.riskCategory, rec.risk_category),
                          (NS.likelihood, rec.likelihood),
                          (NS.impactBand, rec.impact_band),
                          (NS.financialImpactBand, rec.financial_impact_band),
                          (NS.controllability, rec.controllability),
                          (NS.signpostStatus, rec.signpost_status),
                          (NS.eventDriverClass, rec.event_driver_class)):
            if val is not None:
                add(iri, prop, Literal(val))
    elif rec.kind == "evidence_event":
        add(iri, RDF.type, NS.EvidenceEvent)
        add(iri, NS.headline, Literal(rec.headline))
        add(iri, NS.reviewStatus, Literal(rec.review_status))
        if rec.risk_category:
            add(iri, NS.riskCategoryLabel, Literal(rec.risk_category))
            add(iri, NS.normalizedCategory, Literal(_normalized_category(rec.risk_category)))
        if rec.provenance_class:
            add(iri, NS.provenanceClass, Literal(rec.provenance_class))
        if rec.source_url:
            add(iri, NS.sourceUrl, _url_to_iri(rec.source_url))  # IRI-valued: IFP (footgun #3)
        d, drift = _parse_dt(rec.source_date, date_only=True)
        if d:
            add(iri, NS.sourceDate, Literal(d, datatype=XSD.date))
        if drift:
            add(iri, NS.formatDrift, Literal("sourceDate"))
        dt, drift = _parse_dt(rec.retrieved_at)
        if dt:
            add(iri, NS.retrievedAt, Literal(dt, datatype=XSD.dateTime))
        if drift:
            add(iri, NS.formatDrift, Literal("retrievedAt"))
    elif rec.kind == "compound":
        add(iri, RDF.type, NS.CompoundScenario)
        add(iri, NS.pairId, Literal(rec.pair_id))
        add(iri, NS.runId, Literal(rec.run_id))
        add(iri, NS.pHat, Literal(rec.p_hat))
        add(iri, NS.uncertaintyClass, Literal(rec.uncertainty_class))
        add(iri, NS.lossUnits, Literal(rec.loss_units))
        add(iri, NS.compoundStatus, Literal(rec.status))
        for slug in rec.risk_ids:
            add(iri, NS.riskRef, DATA[f"slug/{slug}"])
    elif rec.kind == "compound_run":
        add(iri, RDF.type, NS.CompoundJoinRun)
        add(iri, NS.runId, Literal(rec.id))
    elif rec.kind == "channel_proposal":
        add(iri, RDF.type, NS.ChannelProposal)
        add(iri, NS.proposalStatus, Literal(rec.status))
        add(iri, NS.proposalKind, Literal(rec.proposal_kind))
        add(iri, NS.citationStatus, Literal(rec.citation_status))
        for prop, val in ((NS.channel, rec.channel),
                          (NS.reviewer, rec.reviewer),
                          (NS.reason, rec.reason)):
            if val is not None:
                add(iri, prop, Literal(val))
        if rec.graph_version_applied is not None:
            add(iri, NS.graphVersionApplied, Literal(rec.graph_version_applied))
    elif rec.kind == "mapping_proposal":
        add(iri, RDF.type, NS.FrameworkMappingProposal)
        add(iri, NS.proposalStatus, Literal(rec.status))
        for prop, val in ((NS.reviewer, rec.reviewer),
                          (NS.reason, rec.reason)):
            if val is not None:
                add(iri, prop, Literal(val))
        if rec.graph_version_applied is not None:
            add(iri, NS.graphVersionApplied, Literal(rec.graph_version_applied))
    return iri, t

# ===========================================================================
# FIXED BLOCK — do not modify below this line.
# ===========================================================================

HERE = Path(__file__).parent
ONTOLOGY_FILE = HERE / "domain.ttl"
SHAPES_FILE = HERE / "shapes.ttl"
SH = Namespace("http://www.w3.org/ns/shacl#")


def run_inference(asserted: Graph, ontology: Graph):
    """OWL-RL on a scratch copy; harvest only what we trust (footguns.md #1)."""
    from owlrl import DeductiveClosure, OWLRL_Semantics

    scratch = Graph()
    for tr in asserted:
        scratch.add(tr)
    for tr in ontology:
        scratch.add(tr)
    DeductiveClosure(OWLRL_Semantics, axiomatic_triples=False, datatype_axioms=False).expand(scratch)

    same_as = set()
    for s, o in scratch.subject_objects(OWL.sameAs):
        if (s != o and isinstance(s, URIRef) and isinstance(o, URIRef)
                and s.startswith(str(DATA)) and o.startswith(str(DATA))):
            same_as.add(tuple(sorted((s, o))))

    derived = set()
    for prop in DERIVED_DISPLAY_PROPS:
        for s, o in scratch.subject_objects(prop):
            if (s, prop, o) not in asserted:
                derived.add((s, prop, o))
    return same_as, derived


def run_shacl(data: Graph, ontology: Graph, shapes: Graph) -> list[dict]:
    from pyshacl import validate

    _, results_graph, _ = validate(
        data_graph=data, shacl_graph=shapes, ont_graph=ontology,
        inference="none", advanced=True,
    )
    findings = []
    for r in results_graph.subjects(RDF.type, SH.ValidationResult):
        sev = results_graph.value(r, SH.resultSeverity)
        findings.append({
            "focus": results_graph.value(r, SH.focusNode),
            "severity": "REJECT" if sev == SH.Violation else "FLAG",
            "message": str(results_graph.value(r, SH.resultMessage) or "constraint violated"),
        })
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="two-gate validator")
    ap.add_argument("candidates", help="JSON array of producer output records")
    ap.add_argument("--existing", help="Turtle snapshot of the current store", default=None)
    ap.add_argument("--out", default="out", help="output directory")
    args = ap.parse_args()

    raw = json.loads(Path(args.candidates).read_text())
    ontology = Graph().parse(ONTOLOGY_FILE)
    shapes = Graph().parse(SHAPES_FILE)
    existing = Graph()
    if args.existing:
        existing.parse(args.existing)
    seed_risk_category_scheme(existing)

    # ---- GATE 1 ----------------------------------------------------------
    records: list = []
    for item in raw:
        try:
            records.append((item, _ADAPTER.validate_python(item), []))
        except ValidationError as e:
            errs = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
            records.append((item, None, errs))

    # ---- projection ------------------------------------------------------
    staged = Graph()
    iri_to_records: dict[URIRef, list[int]] = {}
    rec_triples: dict[int, list] = {}
    for i, (_, rec, _) in enumerate(records):
        if rec is None:
            continue
        iri, triples = project(rec, staged)
        iri_to_records.setdefault(iri, []).append(i)
        rec_triples[i] = triples

    asserted = existing + staged

    # ---- inference -------------------------------------------------------
    same_as, derived = run_inference(asserted, ontology)
    validation_graph = Graph()
    for tr in asserted:
        validation_graph.add(tr)
    for a, b in same_as:
        validation_graph.add((a, OWL.sameAs, b))
        validation_graph.add((b, OWL.sameAs, a))

    # ---- GATE 2 ----------------------------------------------------------
    findings = run_shacl(validation_graph, ontology, shapes)

    # ---- verdicts (batch vs graph-side — footguns.md #9) -----------------
    verdicts = []
    graph_side = []
    batch_iris = set(iri_to_records)
    by_focus: dict[URIRef, list[dict]] = {}
    for f in findings:
        if f["focus"] in batch_iris:
            by_focus.setdefault(f["focus"], []).append(f)
        else:
            graph_side.append(f)

    merged_iris: dict[URIRef, list] = {}
    for a, b in same_as:
        if a in batch_iris:
            merged_iris.setdefault(a, []).append(b)
        if b in batch_iris:
            merged_iris.setdefault(b, []).append(a)

    for i, (item, rec, door_errors) in enumerate(records):
        rid = item.get("id", f"<record {i}>")
        kind = item.get("kind", "?")
        if rec is None:
            verdicts.append((i, rid, kind, "REJECTED_AT_DOOR", door_errors))
            continue
        iri = DATA[rec.id]
        hits = by_focus.get(iri, [])
        reasons = [h["message"] for h in hits]
        if any(h["severity"] == "REJECT" for h in hits):
            verdicts.append((i, rid, kind, "REJECT", reasons))
        elif iri in merged_iris:
            tgt = ", ".join(t.rsplit("/", 1)[-1] for t in merged_iris[iri])
            verdicts.append((i, rid, kind, "AUTO_MERGE",
                             [f"identity derived (owl:sameAs {tgt}) — merge into canonical node"] + reasons))
        elif hits:
            verdicts.append((i, rid, kind, "FLAG", reasons))
        else:
            verdicts.append((i, rid, kind, "ADMIT", []))

    # ---- outputs ---------------------------------------------------------
    out = Path(args.out)
    out.mkdir(exist_ok=True)

    admitted = Graph()
    admitted.bind("ns", NS); admitted.bind("data", DATA); admitted.bind("skos", SKOS); admitted.bind("owl", OWL)
    for i, _rid, _k, verdict, _r in verdicts:
        if verdict in ("ADMIT", "AUTO_MERGE", "FLAG"):
            for tr in rec_triples.get(i, []):
                admitted.add(tr)
    for a, b in same_as:
        admitted.add((a, OWL.sameAs, b))
    admitted.serialize(out / "admitted.ttl")

    (out / "verdicts.json").write_text(json.dumps(
        {f"{kind}:{rid}": {"verdict": v, "reasons": reasons}
         for _i, rid, kind, v, reasons in verdicts}, indent=2))

    (out / "review_queue.json").write_text(json.dumps(
        [{"id": rid, "kind": kind, "reasons": reasons}
         for _i, rid, kind, v, reasons in verdicts if v == "FLAG"]
        + [{"id": str(f["focus"]).rsplit("/", 1)[-1], "kind": "existing-node", "reasons": [f["message"]]}
           for f in graph_side if f["severity"] == "FLAG"], indent=2))

    (out / "repair.json").write_text(json.dumps(
        [{"id": rid, "kind": kind, "gate": ("door" if v == "REJECTED_AT_DOOR" else "ledger"),
          "record": records[i][0], "errors": reasons,
          "instruction": "Re-emit this record with the errors fixed. Do not change any other field."}
         for i, rid, kind, v, reasons in verdicts if v in ("REJECT", "REJECTED_AT_DOOR")], indent=2))

    # ---- report ----------------------------------------------------------
    W = {"ADMIT": "✓", "AUTO_MERGE": "⇒", "FLAG": "⚠", "REJECT": "✗", "REJECTED_AT_DOOR": "✗"}
    print(f"\nguard — {len(records)} candidate records")
    print("─" * 78)
    for _i, rid, kind, v, reasons in verdicts:
        print(f" {W[v]} {v:<16} {kind:<12} {rid}")
        for r in reasons:
            print(f"     · {r}")
    if derived:
        print("─" * 78)
        print(" derived by the reasoner (unasked):")
        for s, p, o in sorted(derived):
            print(f"     {s.rsplit('/', 1)[-1]} —{p.rsplit('#', 1)[-1]}→ {o.rsplit('/', 1)[-1]}")
    if graph_side:
        print("─" * 78)
        print(" graph-side findings (existing nodes implicated by this batch):")
        for f in graph_side:
            print(f"   {'⚠' if f['severity'] == 'FLAG' else '✗'} {str(f['focus']).rsplit('/', 1)[-1]}: {f['message']}")

    counts: dict[str, int] = {}
    for _i, _rid, _k, v, _r in verdicts:
        counts[v] = counts.get(v, 0) + 1
    print("─" * 78)
    print(" " + "  ".join(f"{k}: {n}" for k, n in sorted(counts.items())))
    print(f" admitted → {out}/admitted.ttl · review → {out}/review_queue.json · repair → {out}/repair.json\n")

    return 1 if any(v in ("REJECT", "REJECTED_AT_DOOR") for _i, _r, _k, v, _re in verdicts) else 0


if __name__ == "__main__":
    sys.exit(main())
