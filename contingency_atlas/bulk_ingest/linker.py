"""Linker pod: resolve names to ids, dedup across files, validate contracts.

Resolution is deterministic and conservative: an ambiguous name becomes an
issue, never a guess. Ids derive from content so re-running a batch yields the
same ids. Validation failures are attached to candidates, never dropped, and
no field is fabricated to make a business rule pass.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..categories import CATEGORY_RULES, RISK_CATEGORIES
from ..context import ContextError, validate_record
from ..provenance import canonical_public_url, is_public_http_url
from .coerce import annual_frequency, clean, currency_of, format_money, parse_duration_minutes
from .models import ORG_CONTEXT_ROUTES, Candidate, Issue

_STOP = frozenset({"the", "of", "and", "for", "a", "an", "to", "in", "on", "&"})
_ABBREVIATIONS = {"mgmt": "management", "mgt": "management", "svc": "service", "svcs": "services",
                  "ops": "operations", "gw": "gateway", "acct": "account", "txn": "transaction",
                  "pmt": "payment", "pmts": "payments", "intl": "international", "reg": "regulatory",
                  "tech": "technology", "admin": "administration", "dept": "department", "info": "information"}
_GENERIC_TOKENS = frozenset({"payment", "service", "platform", "banking", "management", "system", "gateway", "hub",
                             "engine", "app", "data", "operation", "customer", "corporate", "retail", "processing",
                             "centre", "center", "online", "digital", "core", "group", "bank"})


def _singular(token: str) -> str:
    return token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token
_LEGAL = re.compile(r"\b(?:ltd|limited|inc|incorporated|plc|llc|llp|gmbh|corp|corporation|co|company|"
                    r"sa|ag|nv|bv|pty|srl|spa|group|holdings)\b\.?")
ID_PREFIX = {
    "services": "svc", "dependencies": "dep", "mechanisms": "mech", "controls": "ctl", "assumptions": "asm",
    "actions": "act", "exercises": "exr", "observations": "obs", "impact_assessments": "imp",
    "evidence_events": "evd", "internal_loss_events": "ile", "org_scenarios": "osc", "taxonomy_crosswalk": "txw",
}


def _spelling(text: str) -> str:
    text = re.sub(r"isation\b", "ization", text)
    text = re.sub(r"(?<=[a-z]{3})ise\b", "ize", text)
    return re.sub(r"(?<=[a-z]{3})ised\b", "ized", text)


def name_key(value: str, *, keep_qualifier: bool = True) -> str:
    text = clean(value).casefold().replace("&", " and ")
    if not keep_qualifier:
        text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(_spelling(t) for t in text.split())


def tokens(value: str) -> list[str]:
    words = []
    for t in name_key(value, keep_qualifier=False).split():
        words.extend(_ABBREVIATIONS.get(t, t).split())
    return [_singular(t) for t in words if t not in _STOP]


def acronym(value: str) -> str:
    return "".join(t[0] for t in tokens(value)).upper()


_GEO = frozenset({"UK", "EU", "US", "USA", "IE", "EMEA", "APAC", "AMER", "NA", "DE", "FR", "NL", "LU", "SG", "HK",
                  "IN", "CH", "ROI", "GB", "LEGACY", "NEW", "OLD"})


def declared_aliases(name: str) -> list[str]:
    """Acronym-like parentheticals ('Retail Payments Hub (RPH)') are aliases; geography is a qualifier."""
    return [q for q in re.findall(r"\(([A-Za-z0-9&/.-]{2,8})\)", clean(name))
            if q.upper() not in _GEO and (q.isupper() or q.upper() == acronym(name))]


def _prefix_match(mention: list[str], name: list[str]) -> bool:
    if len(mention) < 2 or not name or mention[0] != name[0] or len(mention) > len(name):
        return False
    j = 0
    for token in mention:
        while j < len(name) and not (name[j] == token or (len(token) >= 3 and name[j].startswith(token))):
            j += 1
        if j == len(name):
            return False
        j += 1
    return True


_SEGMENT_BREAK = re.compile(r"\s*(?:&|\band\b|/|,)\s*", re.IGNORECASE)


def _segments(value: str) -> list[str]:
    """'CHAPS & High-Value Payments' -> ['CHAPS', 'High-Value Payments'] (parentheticals dropped)."""
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", clean(value))
    return [s for s in _SEGMENT_BREAK.split(text) if s.strip()]


def segment_acronyms(name: str) -> set[str]:
    """Acronyms of multi-word segments: 'CHAPS & High-Value Payments' -> {'HVP'}."""
    out = set()
    for segment in _segments(name):
        toks = tokens(segment)
        if len(toks) >= 2:
            out.add("".join(t[0] for t in toks).upper())
    return out


def _consume(words: list[str], name: list[str]) -> int:
    """Name tokens consumed, in order, by mention words (or -1).

    A word matches a token exactly, as a prefix either way (both at least 3
    letters: 'Coll' / 'collateral', 'Documentary' / 'doc'), or, when it is an
    upper-case abbreviation, as the initials of that many consecutive tokens
    ('FP' / 'faster payment')."""
    j = 0
    for word in words:
        if j >= len(name):
            return -1
        token = _singular(_ABBREVIATIONS.get(word.casefold(), word.casefold()))
        if name[j] == token or (len(token) >= 3 and len(name[j]) >= 3
                                and (name[j].startswith(token) or token.startswith(name[j]))):
            j += 1
            continue
        span = len(word)
        if word.isupper() and 2 <= span <= 5 and j + span <= len(name) and \
                "".join(t[0] for t in name[j:j + span]) == word.casefold():
            j += span
            continue
        return -1
    return j


def abbreviation_cover(mention: str, name: str) -> bool:
    """Every mention word maps, in order, onto the name; segment by segment.

    Mention and name must have the same number of '&'/'and'/'/' segments. Each
    name segment is covered at least at its start, and at least one segment is
    covered completely ('Fraud & TM' -> 'Fraud Detection & Transaction
    Monitoring'). A one-segment name must be covered completely ('FP Gateway'
    -> 'Faster Payments Gateway', 'TF Docs' -> 'Trade Finance Docs')."""
    m_segments, n_segments = _segments(mention), _segments(name)
    if not m_segments or len(m_segments) != len(n_segments):
        return False
    full = 0
    for m_seg, n_seg in zip(m_segments, n_segments):
        words = [w for w in re.findall(r"[A-Za-z0-9]+", m_seg) if w.casefold() not in _STOP]
        name_tokens = tokens(n_seg)
        used = _consume(words, name_tokens) if words else -1
        if used <= 0:
            return False
        if used == len(name_tokens):
            full += 1
        elif len(n_segments) == 1:
            return False
    if len(n_segments) == 1:
        return len(re.findall(r"[A-Za-z0-9]+", mention)) >= 2 and full == 1
    return full >= 1


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def short_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


@dataclass
class Entity:
    id: str
    canonical: str
    aliases: set[str] = field(default_factory=set)
    group: str = ""

    def names(self) -> set[str]:
        return {self.canonical} | self.aliases


class Registry:
    """Name -> entity resolution with exact, acronym, prefix and Jaccard strategies."""

    def __init__(self, kind: str):
        self.kind = kind
        self.entities: list[Entity] = []
        self.resolutions: dict[str, dict[str, Any]] = {}
        # Leading words that name the organization itself ('ACME Mobile' -> 'Mobile'),
        # derived from the batch's own legal-entity values, never configured per pack.
        self.org_words: set[str] = set()

    def add(self, name: str, aliases: list[str] = (), group: str = "") -> Entity:
        key = name_key(name)
        entity = Entity(f"bulk-{ID_PREFIX.get(self.kind, self.kind[:4])}-{short_hash(self.kind, key)}",
                        clean(name), group=group)
        entity.aliases.update(declared_aliases(name))
        entity.aliases.update(a for a in aliases if clean(a))
        self.entities.append(entity)
        return entity

    def match(self, mention: str, *, exclude_group: str = "") -> tuple[list[Entity], str]:
        """Strategies on the mention as written, then on derived variants; the first hit set wins."""
        mention = clean(mention)
        hits, strategy = self._match_one(mention, exclude_group)
        if hits:
            return hits, strategy
        for variant, label in self._variants(mention):
            hits, strategy = self._match_one(variant, exclude_group)
            if hits:
                return hits, f"{label}+{strategy}"
        parts = [p for p in re.split(r"\s*/\s*", mention) if p.strip()] if "/" in mention else []
        if len(parts) > 1:  # 'CLM/KYC': alternatives that must agree on one entity
            found: dict[str, Entity] = {}
            for part in parts:
                part_hits, _ = self.match(part, exclude_group=exclude_group)
                found.update({e.id: e for e in part_hits})
            if found:
                return list(found.values()), "slash_alternatives"
        return [], "none"

    def _variants(self, mention: str) -> list[tuple[str, str]]:
        out = []
        words = mention.split()
        if len(words) > 1 and words[0].casefold() in self.org_words:
            out.append((" ".join(words[1:]), "org_prefix"))
        camel = re.sub(r"(?<=[a-z]{2})(?=[A-Z][a-z])", " ", mention)
        if camel != mention:
            out.append((camel, "camel_case"))
        return out

    def _match_one(self, mention: str, exclude_group: str) -> tuple[list[Entity], str]:
        pool = [e for e in self.entities if not exclude_group or e.group != exclude_group]
        key_full, key_base = name_key(mention), name_key(mention, keep_qualifier=False)
        strategies = (
            ("exact", lambda e: any(name_key(n) == key_full for n in e.names())),
            ("exact_base", lambda e: any(name_key(n, keep_qualifier=False) == key_base for n in e.names())),
            ("acronym", lambda e: _acronym_like(mention) and any(
                acronym(n) == mention.upper() or n.upper() == mention.upper() for n in e.names())),
            ("normalized", lambda e: any(tokens(n) == tokens(mention) for n in e.names())),
            ("prefix", lambda e: any(_prefix_match(tokens(mention), tokens(n)) for n in e.names())),
            ("subset", lambda e: _subset(tokens(mention), [tokens(n) for n in e.names()])),
            ("jaccard", lambda e: any(_jaccard(tokens(mention), tokens(n)) >= 0.6 for n in e.names())),
            # Only reached when everything above failed, so earlier resolutions never change.
            ("abbreviation", lambda e: any(abbreviation_cover(mention, n) for n in e.names())),
            ("segment_acronym", lambda e: _acronym_like(mention) and len(mention) >= 3 and any(
                mention.upper() in segment_acronyms(n) for n in e.names())),
        )
        for strategy, test in strategies:
            hits = [e for e in pool if test(e)]
            if hits:
                return hits, strategy
        return [], "none"

    def resolve(self, mention: str) -> tuple[str | None, str]:
        """Return (entity id or None, strategy|'ambiguous'|'none'); remember the alias."""
        if clean(mention) in self.resolutions:
            known = self.resolutions[clean(mention)]
            return known.get("id"), known["strategy"]
        hits, strategy = self.match(mention)
        record = {"mention": clean(mention), "strategy": strategy, "candidates": [e.id for e in hits]}
        if len(hits) == 1:
            if clean(mention) != hits[0].canonical:
                hits[0].aliases.add(clean(mention))
            hits[0].aliases.update(declared_aliases(mention))
            record["id"] = hits[0].id
            self.resolutions[clean(mention)] = record
            return hits[0].id, strategy
        if hits:
            record["id"] = None
            self.resolutions[clean(mention)] = {**record, "strategy": "ambiguous"}
            return None, "ambiguous"
        record["id"] = None
        self.resolutions[clean(mention)] = record
        return None, "none"

    def to_dict(self) -> dict[str, Any]:
        return {"entities": [{"id": e.id, "canonical": e.canonical, "aliases": sorted(e.aliases)}
                             for e in sorted(self.entities, key=lambda e: e.id)],
                "resolutions": dict(sorted(self.resolutions.items()))}


def _subset(mention: list[str], names: list[list[str]]) -> bool:
    """All mention tokens appear in a name; one-token mentions must be distinctive."""
    if not mention:
        return False
    if len(mention) == 1 and (len(mention[0]) < 3 or mention[0] in _GENERIC_TOKENS):
        return False
    return any(set(mention) <= set(n) and len(n) > len(mention) for n in names)


def _acronym_like(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{2,6}", value)) and (value.isupper() or len(value) <= 4)


_VENDOR_NOISE = re.compile(r"\b(?:uk|emea|europe|european|ireland|international|united kingdom|eu|us|usa|global|"
                          r"sarl|s a r l|operations|dac|se|sas|bv|ab|oy|as)\b")


# Descriptor words dropped from the end of a supplier name ('LexisNexis Risk Solutions',
# 'Euroclear Bank', 'BT Business'), only while something distinctive remains.
_VENDOR_TRAILING = frozenset({"bank", "solutions", "solution", "technologies", "technology", "business",
                              "lp", "sc"})


def vendor_key(value: str) -> str:
    text = clean(value)
    # Dotted abbreviations collapse: 'S.W.I.F.T.' -> 'SWIFT', 'L.P.' -> 'LP'.
    text = re.sub(r"\b(?:[A-Za-z]\.){2,}", lambda m: m.group(0).replace(".", ""), text)
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"\.(?:com|net|org|io)\b", " ", text)  # 'salesforce.com'
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = _VENDOR_NOISE.sub(" ", text)
    previous = None
    while previous != text:
        previous, text = text, _LEGAL.sub(" ", text).strip()
    words = text.split()
    while len(words) > 1 and words[-1] in _VENDOR_TRAILING:
        words.pop()
    return " ".join(words)


class VendorRegistry:
    def __init__(self):
        self.groups: dict[str, list[str]] = {}
        self.mapping: dict[str, str] = {}

    def build(self, names: list[str], register_ids: list[str | None] | None = None) -> None:
        """Group supplier spellings. ``register_ids`` (parallel to ``names``) are
        'file#supplier-id' declarations: two names a single register lists under
        different supplier ids are different suppliers and are never merged."""
        keyed: dict[str, list[str]] = {}
        declared: dict[str, set[str]] = {}
        for n, name in enumerate(names):
            key = vendor_key(name) or clean(name).casefold()
            keyed.setdefault(key, []).append(clean(name))
            rid = register_ids[n] if register_ids else None
            if rid:
                declared.setdefault(key, set()).add(rid)
        keys = sorted(keyed, key=lambda k: (-len(k), k))
        merged: dict[str, list[str]] = {}
        merged_ids: dict[str, set[str]] = {}
        for key in keys:
            targets = [m for m in merged if _vendor_alias(key, m)
                       and not _distinct_register_entries(declared.get(key, set()), merged_ids.get(m, set()))]
            target = targets[0] if len(targets) == 1 else key
            merged.setdefault(target, []).extend(keyed[key])
            merged_ids.setdefault(target, set()).update(declared.get(key, set()))
        for key, variants in merged.items():
            counts: dict[str, int] = {}
            for v in variants:
                counts[v] = counts.get(v, 0) + 1
            canonical = sorted(counts, key=lambda v: (-counts[v], -len(v), v))[0]
            self.groups[canonical] = sorted(set(variants))
            for v in variants:
                self.mapping[v] = canonical

    def canonical(self, name: str) -> str:
        return self.mapping.get(clean(name), clean(name))


def _distinct_register_entries(a: set[str], b: set[str]) -> bool:
    """True when one register declares the two names under different supplier ids."""
    files_a = {}
    for rid in a:
        file, _, sid = rid.partition("#")
        files_a.setdefault(file, set()).add(sid)
    for rid in b:
        file, _, sid = rid.partition("#")
        if file in files_a and sid not in files_a[file]:
            return True
    return False


def _vendor_alias(short: str, long: str) -> bool:
    st, lt = short.split(), long.split()
    if not st or short == long:
        return short == long
    if len(st) == 1 and _acronym_like(short) and "".join(t[0] for t in lt) == short:
        return True
    return _prefix_match(st, lt) or (len(st) == 1 and lt[0] == st[0] and len(st[0]) >= 4)


# --------------------------------------------------------------------------- identity / merge


def _identity_keys(c: Candidate) -> list[str]:
    d, m = c.data, c.meta
    keys = []
    if m.get("org_ref") or d.get("org_ref"):
        ref = str(m.get("org_ref") or d.get("org_ref"))
        # Short refs ('A1', 'R3', 'AF-1') are local to their document; longer ones are global.
        scope = "" if global_ref(ref) else c.provenance[0].file + "#"
        keys.append("ref:" + scope + name_key(ref))
    k = c.kind
    if k == "dependencies":
        dest = d.get("to_service_id") or vendor_key(d.get("external_provider", ""))
        mentions = m.get("service_mentions") or {}
        source = d.get("from_service_id") or "?" + name_key(" ".join(mentions.get("from_service_id", [])))
        dest = dest or "?" + name_key(" ".join(mentions.get("to_service_id", [])))
        keys.append(f"dep:{source}:{dest}:{name_key(d.get('category', ''))}")
    elif k in ("controls", "mechanisms"):
        keys.append("name:" + name_key(d.get("name", "")))
    elif k == "assumptions":
        keys.append("stmt:" + name_key(d.get("statement", "")))
    elif k in ("actions", "org_scenarios"):
        keys.append("title:" + name_key(d.get("title", "")))
    elif k in ("exercises", "observations"):
        keys.append(f"title:{name_key(d.get('title', ''))}:{d.get('completed_on') or d.get('observed_on') or ''}")
    elif k == "impact_assessments":
        keys.append(f"imp:{d.get('service_id', '')}:{name_key(d.get('title', ''))}:{d.get('observed_on', '')}")
    elif k == "evidence_events":
        url = d.get("source_url", "")
        keys.append("url:" + (canonical_public_url(url) if is_public_http_url(url) else url) + ":"
                    + name_key(d.get("headline", "")))
    elif k == "internal_loss_events" and not keys:
        keys.append(f"loss:{d.get('occurrence_date', '')}:{name_key(d.get('description', ''))[:80]}")
    elif k == "taxonomy_crosswalk":
        keys.append("code:" + name_key(d.get("org_code", "")))
    return [key for key in keys if key.split(":", 1)[1].strip(":")]


def global_ref(ref: str) -> bool:
    ref = clean(ref)
    return len(ref) >= 5 and bool(re.search(r"\d", ref)) and bool(re.search(r"[A-Za-z]", ref))


def _round_sig(value: float, digits: int = 2) -> float:
    return float(f"{value:.{digits}g}")


_MONEY = frozenset({"gross_loss", "recoveries", "net_loss", "potential_impact", "severity_p50", "severity_p90",
                    "severity_p99", "loss_amount_usd"})
_CURRENCY_FIELDS = frozenset({"currency", "severity_currency"})


def _currency(c: Candidate) -> str | None:
    return c.data.get("currency") or c.data.get("severity_currency")


def _display(field: str, value: Any, c: Candidate) -> str:
    raw = c.meta.get("raw", {})
    if field in _MONEY and isinstance(value, (int, float)) and _currency(c) and not currency_of(str(raw.get(field, ""))):
        return format_money(float(value), _currency(c))
    source_field = {"from_service_id": "service", "service_ids": "service"}.get(field, field)
    if source_field in raw:
        return str(raw[source_field])
    if isinstance(value, float):
        return format_money(value, c.data.get("currency") or c.data.get("severity_currency"))
    return str(value)


_DURATION_FIELDS = frozenset({"impact_tolerance", "tolerance_reference"})
_DURATION_PART = re.compile(r"(?i)^\s*(?:(rto|rpo|mtd|mtpd|tolerance|impact tolerance|max(?:imum)? tolerable "
                            r"(?:duration|period))\s*[:=-]?\s*)?(.+?)\s*$")


def duration_profile(text: str) -> dict[str, float]:
    """'RTO 4 hours (240 minutes); RPO 1 hour' -> {'rto': 240, 'rpo': 60}; '4 hrs' -> {'': 240}.

    Empty when any part is not a plain duration, so free text is never coerced."""
    profile: dict[str, float] = {}
    for part in re.split(r"[;,]", clean(text)):
        part = re.sub(r"\([^)]*\)", " ", part).strip()
        if not part:
            continue
        m = _DURATION_PART.match(part)
        label = (m[1] or "").casefold()
        label = "" if label not in ("rto", "rpo") else label
        minutes, _ = parse_duration_minutes(m[2])
        if minutes is None:
            return {}
        profile[label] = minutes
    return profile


def _same_quantity(field: str, a: str, b: str) -> bool | None:
    """Compare on the stated quantity; None when the texts are not comparable quantities."""
    if field in _DURATION_FIELDS:
        pa, pb = duration_profile(a), duration_profile(b)
        if pa and pb:
            common = set(pa) & set(pb)
            if not common:
                # A bare tolerance is compared with the recovery time objective.
                if "" in pa and "rto" in pb:
                    return pa[""] == pb["rto"]
                if "" in pb and "rto" in pa:
                    return pb[""] == pa["rto"]
                return None
            return all(pa[k] == pb[k] for k in common)
    if field == "frequency_text":
        fa, fb = annual_frequency(a), annual_frequency(b)
        if fa is not None and fb is not None:
            return abs(fa - fb) <= 1e-9
    return None


def _same(a: Any, b: Any, field: str = "") -> bool:
    if isinstance(a, str) and isinstance(b, str) and field:
        verdict = _same_quantity(field, a, b)
        if verdict is not None:
            return verdict
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        a, b = float(a), float(b)
        if abs(a - b) <= 1e-9 * max(1.0, abs(a)):
            return True
        # Presentation rounding ('£1.2m' in a report vs 1,234,567 in the ledger) is not a conflict.
        coarse = [v for v in (a, b) if v == _round_sig(v)]
        return bool(coarse) and _round_sig(a) == _round_sig(b)
    if isinstance(a, str) and isinstance(b, str):
        return name_key(a) == name_key(b)
    return a == b


def merge_candidates(candidates: list[Candidate], issues: list[Issue]) -> list[Candidate]:
    merged: list[Candidate] = []
    index: dict[tuple[str, str], Candidate] = {}
    for c in candidates:
        keys = _identity_keys(c)
        target = next((index[(c.kind, k)] for k in keys if (c.kind, k) in index), None)
        if target is not None and _refs_differ(target, c):
            target = None
        if target is None and _ref(c) and global_ref(_ref(c)):
            # 'SCN-2025-007' in a report is 'ACME-SCN-2025-007' in the register; merge only when unique.
            equivalent = [t for t in merged if t.kind == c.kind and _ref(t) and global_ref(_ref(t))
                          and t.provenance[0].file != c.provenance[0].file
                          and _ref(t) != _ref(c) and refs_equivalent(_ref(t), _ref(c))]
            target = equivalent[0] if len(equivalent) == 1 else None
            if len(equivalent) > 1:
                issues.append(Issue("warning", "ambiguous_reference",
                                    f"{c.kind} {_ref(c)!r} abbreviates several references "
                                    f"({', '.join(sorted(_ref(t) for t in equivalent))}); kept separate",
                                    c.provenance[0].file, c.provenance[0].locator))
        if target is None and c.kind == "org_scenarios" and c.data.get("title"):
            similar = [t for t in merged if t.kind == "org_scenarios" and t.data.get("title")
                       and t.provenance[0].file != c.provenance[0].file and not _refs_differ(t, c)
                       and _jaccard(tokens(t.data["title"]), tokens(c.data["title"])) >= 0.6]
            target = similar[0] if len(similar) == 1 else None
        if target is None:
            merged.append(c)
            for k in keys:
                index.setdefault((c.kind, k), c)
            continue
        # Same table rows are distinct declarations unless their identity is identical.
        _merge_into(target, c, issues)
        for k in keys:
            index.setdefault((c.kind, k), target)
    return merged


def _ref(c: Candidate) -> str:
    return name_key(str(c.meta.get("org_ref") or c.data.get("org_ref") or ""))


def _refs_differ(a: Candidate, b: Candidate) -> bool:
    return bool(_ref(a) and _ref(b) and not refs_equivalent(_ref(a), _ref(b)))


def refs_equivalent(a: str, b: str) -> bool:
    """Same reference written in full or abbreviated: 'SCN-22' ~ 'ACME-SCN-2025-022'.

    The shorter ref's letter groups must all appear in the longer one, its final
    number must equal the longer one's final number (leading zeros ignored), and
    its other numbers must appear in the longer one. Callers treat several
    equivalent candidates as ambiguous."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    parts = sorted((re.findall(r"[a-z]+|\d+", ka), re.findall(r"[a-z]+|\d+", kb)), key=len)
    short, long = parts
    alpha_s, alpha_l = [t for t in short if not t.isdigit()], [t for t in long if not t.isdigit()]
    nums_s, nums_l = [int(t) for t in short if t.isdigit()], [int(t) for t in long if t.isdigit()]
    if not alpha_s or not nums_s or not nums_l or nums_s[-1] != nums_l[-1]:
        return False
    return set(alpha_s) <= set(alpha_l) and all(n in nums_l[:-1] for n in nums_s[:-1])


def _merge_into(target: Candidate, other: Candidate, issues: list[Issue]) -> None:
    target.provenance.extend(other.provenance)
    target.warnings.extend(w for w in other.warnings if w not in target.warnings)
    for key in ("aliases",):
        if other.meta.get(key):
            target.meta.setdefault(key, [])
            target.meta[key] = sorted(set(target.meta[key]) | set(other.meta[key]))
    for field_name, value in other.data.items():
        if field_name in ("source", "source_refs"):
            if field_name == "source_refs":
                target.data["source_refs"] = sorted(set(target.data.get("source_refs", [])) | set(value))
            continue
        current = target.data.get(field_name)
        if current in (None, "", []):
            target.data[field_name] = value
            target.meta.setdefault("raw", {}).setdefault(field_name, other.meta.get("raw", {}).get(field_name))
        elif isinstance(current, list) and isinstance(value, list):
            target.data[field_name] = sorted(set(current) | set(value), key=str)
        elif field_name in ("name", "title") and not _same(current, value):
            target.meta["aliases"] = sorted(set(target.meta.get("aliases", [])) | {str(value)})
        elif field_name == "org_ref" and isinstance(value, str) and refs_equivalent(str(current), value):
            if name_key(str(current)) != name_key(value):  # an abbreviated form of the same reference
                target.meta["aliases"] = sorted(set(target.meta.get("aliases", [])) | {value})
        elif field_name in target.meta.get("derived_fields", []) or field_name in other.meta.get("derived_fields", []):
            continue
        elif field_name in _CURRENCY_FIELDS or (field_name in _MONEY and _currency(target) != _currency(other)):
            if field_name in _CURRENCY_FIELDS:
                issues.append(Issue("warning", "currency_mismatch",
                                    f"{target.kind} {target.meta.get('org_ref') or target.data.get('org_ref', '')}: "
                                    f"reported in {current} and {value}; amounts are not compared",
                                    other.provenance[0].file, other.provenance[0].locator))
            continue
        elif not _same(current, value, field_name):
            conflict = {"field": field_name,
                        "values": [_display(field_name, current, target), _display(field_name, value, other)],
                        "files": [target.provenance[0].file, other.provenance[0].file],
                        "locators": [target.provenance[0].locator, other.provenance[0].locator]}
            target.meta.setdefault("conflicts", []).append(conflict)
            issues.append(Issue(
                "error", "conflict",
                f"{target.kind} {target.data.get('name') or target.data.get('title') or target.meta.get('org_ref', '')}"
                f": {field_name} differs across sources ({conflict['values'][0]} vs {conflict['values'][1]}); "
                "the first value is kept until a reviewer decides",
                target.provenance[0].file, target.provenance[0].locator))


# --------------------------------------------------------------------------- linking


@dataclass
class LinkResult:
    candidates: list[Candidate]
    issues: list[Issue]
    services: Registry
    vendors: VendorRegistry
    mechanisms: Registry

    def to_dict(self) -> dict[str, Any]:
        return {"services": self.services.to_dict(), "mechanisms": self.mechanisms.to_dict(),
                "vendors": {"groups": self.vendors.groups, "mapping": dict(sorted(self.vendors.mapping.items()))}}


def _seed_services(services: list[Candidate], registry: Registry, issues: list[Issue]) -> list[Candidate]:
    """Largest service table seeds canonical names; later mentions resolve against it."""
    ordered = sorted(services, key=lambda c: (-int(c.meta.get("table_rows", 0)), c.provenance[0].file,
                                              c.provenance[0].locator))
    kept: list[Candidate] = []
    by_id: dict[str, Candidate] = {}
    for c in ordered:
        name = clean(c.data.get("name", ""))
        if not name:
            kept.append(c)
            continue
        group = f"{c.provenance[0].file}#{c.meta.get('table', '')}"
        hits, strategy = registry.match(name, exclude_group=group)
        same_group_exact = [e for e in registry.entities if e.group == group and name_key(e.canonical) == name_key(name)]
        hits = same_group_exact or hits
        if c.meta.get("org_ref"):
            ref_hits = [e for e in registry.entities if any(name_key(a) == name_key(c.meta["org_ref"]) for a in e.aliases)]
            hits = ref_hits or hits
        if len(hits) == 1:
            entity = hits[0]
            if name != entity.canonical:
                entity.aliases.add(name)
            entity.aliases.update(c.meta.get("aliases", []))
            registry.resolutions[name] = {"mention": name, "strategy": strategy, "id": entity.id,
                                          "candidates": [entity.id]}
            c.data["name"] = entity.canonical  # a resolved alias, not a conflicting name
            _merge_into(by_id[entity.id], c, issues)
            continue
        if len(hits) > 1:
            issues.append(Issue("warning", "ambiguous_link",
                                f"service {name!r} resembles several services ({', '.join(e.canonical for e in hits)}); "
                                "kept as a separate service for review", c.provenance[0].file, c.provenance[0].locator))
            c.meta["ambiguous"] = True
        entity = registry.add(name, c.meta.get("aliases", []), group=group)
        c.id = entity.id
        by_id[entity.id] = c
        kept.append(c)
    return kept


def _resolve_links(c: Candidate, services: Registry, mechanisms: Registry, issues: list[Issue]) -> None:
    unresolved = []
    for field_name, mentions in (c.meta.get("service_mentions") or {}).items():
        ids = []
        for mention in mentions:
            resolved, strategy = services.resolve(mention)
            if resolved:
                ids.append(resolved)
                if strategy not in ("exact", "exact_base"):
                    c.meta.setdefault("link_strategies", {})[mention] = strategy
            else:
                unresolved.append(mention)
                issues.append(Issue(
                    "warning", "ambiguous_link" if strategy == "ambiguous" else "orphan_link",
                    f"{c.kind}: service {mention!r} " + ("matches several services" if strategy == "ambiguous"
                                                        else "is not in any service list of this batch"),
                    c.provenance[0].file, c.provenance[0].locator))
        if field_name in ("service_ids",):
            if ids:
                c.data["service_ids"] = sorted(set(ids))
        elif ids:
            c.data[field_name] = ids[0]
    mechanism_ids = []
    for mention in c.meta.get("mechanism_mentions", []):
        resolved, strategy = mechanisms.resolve(mention)
        if resolved:
            mechanism_ids.append(resolved)
        else:
            unresolved.append(mention)
            issues.append(Issue("warning", "orphan_link", f"{c.kind}: mechanism {mention!r} is not declared in this batch",
                                c.provenance[0].file, c.provenance[0].locator))
    if mechanism_ids:
        c.data["mechanism_ids"] = sorted(set(mechanism_ids))
    if unresolved:
        c.meta["unresolved_links"] = unresolved


def crosswalk(c: Candidate) -> None:
    """Map an organization taxonomy node onto an Atlas category with a rationale; reviewer stays empty."""
    label = str(c.data.get("org_label", ""))
    text = f"{label} {c.meta.get('definition', '')}".casefold()
    label_key = name_key(label)
    for category in RISK_CATEGORIES:
        if name_key(category) == label_key:
            c.data.update(atlas_category=category, mapping_type="exact",
                          rationale="label equals the Atlas category name")
            break
    else:
        for category, terms in CATEGORY_RULES:
            term = next((t for t in terms if t in text), None)
            if term:
                level = c.data.get("level")
                mapping = "narrower" if isinstance(level, int) and level >= 2 else "related"
                c.data.update(atlas_category=category, mapping_type=mapping,
                              rationale=f"keyword {term!r} matches the Atlas {category} rule; "
                                        f"{'a level-' + str(level) + ' node is treated as narrower' if mapping == 'narrower' else 'scope overlap only'}")
                break
        else:
            c.warnings.append("no Atlas category matched; mapping left for the reviewer")
    # Pods never approve: a staged crosswalk row is always proposed and unreviewed,
    # whatever the source file claims.
    c.data["status"] = "proposed"
    c.data["reviewer"] = ""
    c.data.pop("reviewed_on", None)


def mint_id(c: Candidate) -> str:
    keys = _identity_keys(c) or [repr(sorted(c.data.items()))]
    return f"bulk-{ID_PREFIX[c.kind]}-{short_hash(c.kind, keys[0])}"


def _validate_evidence(data: dict) -> list[str]:
    errors = []
    for field_name in ("headline", "source_url", "source_date", "independently_authored_summary"):
        if not data.get(field_name):
            errors.append(f"{field_name}: Field required")
    if data.get("source_url") and not is_public_http_url(data["source_url"]):
        errors.append("source_url: must be a public HTTP(S) URL")
    if data.get("source_date") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data["source_date"])):
        errors.append("source_date: must be YYYY-MM-DD")
    return errors


def validate(c: Candidate) -> None:
    payload = {"id": c.id, **{k: v for k, v in c.data.items() if k != "id"}}
    try:
        if c.kind in ORG_CONTEXT_ROUTES:
            c.data = validate_record(c.kind, payload)
        else:
            c.errors.extend(_validate_evidence(payload))
            c.data = payload
    except ValidationError as exc:
        c.data = payload
        c.errors.extend(f"{'.'.join(map(str, e['loc'])) or 'record'}: {e['msg']}"
                        for e in exc.errors(include_url=False, include_input=False))
    except (ContextError, ValueError, TypeError) as exc:
        c.data = payload
        c.errors.append(str(exc))


def _org_words(services: list[Candidate]) -> set[str]:
    """The organization's own leading name word, when most services share one legal-entity prefix."""
    firsts = [clean(c.data.get("legal_entity", "")).split()[0].casefold()
              for c in services if clean(c.data.get("legal_entity", ""))]
    if len(firsts) < 3:
        return set()
    word, count = max(((w, firsts.count(w)) for w in set(firsts)), key=lambda x: (x[1], x[0]))
    return {word} if count * 2 > len(firsts) and len(word) >= 2 and word not in _STOP else set()


def apply_reference_aliases(registry: Registry, hints: list[dict[str, str]], issues: list[Issue]) -> int:
    """Name columns keyed by a service reference elsewhere in the batch ('IBS1001003 | FPS Gateway').

    The reference must already identify exactly one service (its org_ref). The name
    then becomes an alias of that service, unless it already names a different
    service, which is raised as an issue instead. Returns aliases added."""
    added = 0
    for hint in hints:
        ref, name = clean(hint.get("ref", "")), clean(hint.get("name", ""))
        if not ref or not name:
            continue
        owners = [e for e in registry.entities if any(name_key(a) == name_key(ref) for a in e.aliases)]
        if len(owners) != 1:
            continue
        entity = owners[0]
        if any(name_key(n) == name_key(name) for n in entity.names()):
            continue
        clash = [e for e in registry.entities if e is not entity
                 and any(name_key(n) == name_key(name) for n in e.names())]
        if clash:
            issues.append(Issue("warning", "ambiguous_link",
                                f"service reference {ref} is named {name!r}, which is also the name of "
                                f"{clash[0].canonical!r}; alias not added", hint.get("file", ""),
                                hint.get("locator", "")))
            continue
        entity.aliases.add(name)
        registry.resolutions[name] = {"mention": name, "strategy": "reference", "id": entity.id,
                                      "candidates": [entity.id], "via": f"{ref} ({hint.get('file', '')}"
                                                                        f" {hint.get('locator', '')})".strip()}
        added += 1
    return added


def _resolve_scenarios(final: list[Candidate], issues: list[Issue]) -> None:
    """scenario_ids from scenario mentions, against the organization scenarios in this batch."""
    scenarios = [c for c in final if c.kind == "org_scenarios"]
    for c in final:
        mentions = c.meta.get("scenario_mentions") or []
        if not mentions:
            continue
        ids, unresolved = [], []
        for mention in mentions:
            key = name_key(mention)
            hits = [s for s in scenarios if _ref(s) and _ref(s) == key]
            if not hits and re.search(r"\d", mention) and re.search(r"[A-Za-z]", mention):
                hits = [s for s in scenarios if _ref(s) and refs_equivalent(_ref(s), key)]
            if not hits:
                hits = [s for s in scenarios if name_key(s.data.get("title", "")) == key]
            if len(hits) == 1:
                ids.append(hits[0].id)
                continue
            unresolved.append(mention)
            issues.append(Issue("warning", "ambiguous_link" if hits else "orphan_link",
                                f"{c.kind}: scenario {mention!r} " + (
                                    "matches several organization scenarios" if hits else
                                    "is not in any scenario register of this batch"),
                                c.provenance[0].file, c.provenance[0].locator))
        if ids:
            c.data["scenario_ids"] = sorted(set(c.data.get("scenario_ids", [])) | set(ids))
        if unresolved:
            c.meta["unresolved_links"] = sorted(set(c.meta.get("unresolved_links", [])) | set(unresolved))


def link_batch(candidates: list[Candidate], *, classification: str = "organization",
               alias_hints: list[dict[str, str]] | None = None) -> LinkResult:
    issues: list[Issue] = []
    services = Registry("services")
    mechanisms = Registry("mechanisms")
    vendors = VendorRegistry()
    service_candidates = [c for c in candidates if c.kind == "services"]
    others = [c for c in candidates if c.kind != "services"]
    kept_services = _seed_services(service_candidates, services, issues)
    services.org_words = _org_words(kept_services)
    apply_reference_aliases(services, alias_hints or [], issues)
    for c in others:
        if c.kind == "mechanisms" and c.data.get("name"):
            hits, _ = mechanisms.match(c.data["name"])
            ref = c.meta.get("org_ref")
            if not hits:
                mechanisms.add(c.data["name"], [ref] if ref else [])
            elif len(hits) == 1 and ref:
                hits[0].aliases.add(clean(ref))
    with_vendor = [c for c in others if c.meta.get("vendor")]
    vendors.build([c.meta["vendor"] for c in with_vendor],
                  [f"{c.provenance[0].file}#{c.data['supplier_external_id']}"
                   if c.data.get("supplier_external_id") else None for c in with_vendor])
    for c in others:
        if c.kind == "dependencies" and c.data.get("external_provider"):
            canonical = vendors.canonical(c.data["external_provider"])
            if canonical != c.data["external_provider"]:
                c.meta["vendor_alias"] = c.data["external_provider"]
            c.data["external_provider"] = canonical
        _resolve_links(c, services, mechanisms, issues)
        if c.kind == "dependencies" and "name" in c.meta.get("derived_fields", []) and c.data.get("from_service_id"):
            entity = next(e for e in services.entities if e.id == c.data["from_service_id"])
            target = c.data.get("external_provider") or next(
                (e.canonical for e in services.entities if e.id == c.data.get("to_service_id")), None) or " / ".join(
                (c.meta.get("service_mentions") or {}).get("to_service_id", [])) or "an unresolved target"
            c.data["name"] = f"{entity.canonical} depends on {target}"
        if c.kind == "taxonomy_crosswalk":
            crosswalk(c)
    others = merge_candidates(others, issues)
    final = kept_services + others
    for c in final:
        if c.kind == "mechanisms" and c.data.get("name"):
            hits, _ = mechanisms.match(c.data["name"])
            c.id = hits[0].id if len(hits) == 1 else mint_id(c)
        elif not c.id:
            c.id = mint_id(c)
    _dedupe_ids(final, issues)
    _resolve_scenarios(final, issues)  # needs the final organization-scenario ids
    for c in final:
        c.data.setdefault("classification", classification)
        prov = c.provenance[0]
        c.data["source"] = f"Bulk ingest: {prov.file} ({prov.locator})"
        refs = [f"{p.file}#{p.locator}" for p in c.provenance]
        c.data["source_refs"] = sorted(set(c.data.get("source_refs", [])) | set(refs))
        validate(c)
        _net_check(c, issues)
        c.status = "invalid" if c.errors else (
            "needs_review" if c.meta.get("conflicts") or c.meta.get("unresolved_links") or c.meta.get("ambiguous")
            or any(p.method == "llm" for p in c.provenance) else "ready")
    final.sort(key=lambda c: (c.kind, c.id))
    return LinkResult(final, issues, services, vendors, mechanisms)


def _net_check(c: Candidate, issues: list[Issue]) -> None:
    if c.kind != "internal_loss_events":
        return
    gross, rec, net = (c.data.get(k) for k in ("gross_loss", "recoveries", "net_loss"))
    if all(isinstance(v, (int, float)) for v in (gross, rec, net)) and abs(gross - rec - net) > 0.01 * max(1, gross):
        c.warnings.append("net loss differs from gross minus recoveries")
        issues.append(Issue("warning", "inconsistent_amounts",
                            f"loss {c.data.get('org_ref')}: gross {gross:g} - recoveries {rec:g} != net {net:g}",
                            c.provenance[0].file, c.provenance[0].locator, [c.id]))


def _dedupe_ids(candidates: list[Candidate], issues: list[Issue]) -> None:
    seen: dict[str, Candidate] = {}
    for c in candidates:
        if c.id in seen:
            base, n = c.id, 2
            while f"{base}-{n}" in seen:
                n += 1
            c.id = f"{base}-{n}"
            c.data["id"] = c.id
            c.warnings.append("distinct record with an identical identity key; id suffixed")
        seen[c.id] = c
