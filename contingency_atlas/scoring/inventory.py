"""Risk inventory loading: emerging_risks.json -> RiskAtom rows.

Phase 1 migration path (data-model.md section 6): the existing
``emerging_risks.json`` artifacts become ``RiskAtom`` records. Channel tags
are assigned deterministically (keyword rules, no LLM) so that the channel
graph and every downstream hash are reproducible.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import RiskAtom

# ---------------------------------------------------------------------------
# Channel taxonomy (algorithm-spec.md section 4.3) and deterministic tagger
# ---------------------------------------------------------------------------

#: Canonical causal channels used for channel-blocked generation and the
#: common-factor dependence story. Order matters for the keyword tagger:
#: the first matching rule wins for each keyword group.
CHANNELS: tuple[str, ...] = (
    "identity",
    "cloud",
    "payments",
    "model",
    "credit-chain",
    "physical",
    "energy",
    "legal",
    "third-party",
    "geopolitical",
    "data",
)

#: channel -> keyword patterns matched (case-insensitive) against
#: "title + description". Rules are deliberately boring and auditable.
_CHANNEL_RULES: dict[str, tuple[str, ...]] = {
    "identity": (
        r"\bidentity\b",
        r"\bauthentication\b",
        r"\bcredential",
        r"\baccess control",
    ),
    "cloud": (
        r"\bcloud\b",
        r"\bhyperscal",
        r"\bcompute supply",
        r"\bregion(s|al)?\b",
        r"\bdatacenter",
    ),
    "payments": (
        r"\bpayment",
        r"\bsettlement",
        r"\btransaction",
        r"\bliquidity",
        r"\breconciliation",
    ),
    "model": (
        r"\bmodel",
        r"\bai\b",
        r"\bdistill",
        r"\bfrontier-",
        r"\bpatch window",
        # AI-agent cyber capability / automated vuln work (Black Hat-class
        # entries often name agents and zero-days without the word "model")
        r"\bvulnerabilit",
        r"\bzero[- ]?day",
        r"\bremediation",
        r"\bagentic\b",
        r"\bagent collective",
        r"\bcritical cyber",
    ),
    "credit-chain": (
        r"\bcredit\b",
        r"\bspv",
        r"\bfinancing",
        r"\bcapital expenditure",
        r"\bcapex",
    ),
    "physical": (
        r"\bphysical\b",
        r"\brecovery[- ]site",
        r"\bfacilit",
        r"\bon-?site\b",
    ),
    "energy": (
        r"\bgrid\b",
        r"\bpower\b",
        r"\belectric",
        r"\benergy\b",
    ),
    "legal": (
        r"\blegal\b",
        r"\blitigation",
        r"\bsanction",
        r"\bregulat",
    ),
    "third-party": (
        r"\bthird[- ]party",
        r"\bvendor",
        r"\bcounterpart",
        r"\bsupply[- ]chain",
        r"\boutsource",
    ),
    "geopolitical": (
        r"\bgeopolit",
        r"\bstate capital",
        r"\bsovereign",
        r"\btariff",
        r"\bexport control",
    ),
    "data": (
        r"\breference[- ]data",
        r"\bdata divergence",
        r"\bdata quality",
        r"\bdata pipeline",
        r"\bmaster data",
    ),
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(title: str, max_words: int | None = None) -> str:
    """Deterministic slug from a risk title — lossless by default.

    The old 8-word cap truncated long titles and produced risk_ids that no
    longer derived from any register title (dangling compound references —
    ontology constraint C-02). Slugs are join keys, so they must stay a total
    function of the title; ``max_words`` remains for display-only callers."""
    words = [w for w in _SLUG_RE.split(title.lower()) if w]
    if max_words is not None:
        words = words[:max_words]
    return "-".join(words) or "risk"


def tag_channels(title: str, description: str = "") -> list[str]:
    """Assign causal channels from keyword rules. Deterministic, no LLM."""
    text = f"{title} {description}".lower()
    tagged = [ch for ch in CHANNELS if any(re.search(p, text) for p in _CHANNEL_RULES[ch])]
    return sorted(tagged)


def load_risk_atoms(path: str | Path, *, assign_channels: bool = True) -> list[RiskAtom]:
    """Load the legacy emerging_risks.json artifact into RiskAtom records.

    Legacy items use ``issue`` for the title and carry no channel tags;
    channels are derived with :func:`tag_channels` unless the record already
    has an explicit ``channels`` list (e.g. produced by the R0 generator).
    """
    raw = json.loads(Path(path).read_text())
    atoms: list[RiskAtom] = []
    seen: dict[str, int] = {}
    for item in raw:
        title = item.get("issue") or item.get("title") or "untitled-risk"
        base = slugify(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        # A persisted risk_id is the record's identity (C-03): recomputing from
        # the title would move the join key every time a title is edited.
        risk_id = str(item.get("risk_id") or "").strip() or (
            base if n == 0 else f"{base}-{n + 1}"
        )
        channels = list(item.get("channels", []))
        if not channels and assign_channels:
            channels = tag_channels(title, item.get("description", ""))
        atoms.append(
            RiskAtom(
                risk_id=risk_id,
                title=title,
                description=item.get("description", ""),
                scoring_cell_code=item.get("scoring_cell_code", ""),
                atlas_category=item.get("atlas_category", item.get("risk_category", "")),
                channels=channels,
                external_drivers=list(item.get("external_drivers", [])),
                signal_maturity=item.get("signal_maturity", ""),
                momentum=item.get("momentum", ""),
                trend=item.get("trend", ""),
                evidence_strength=item.get("evidence_strength", ""),
                status=item.get("status", "monitoring"),
                review_status=item.get("review_status", ""),
                plane=item.get("plane", "public"),
                first_seen=item.get("first_seen", item.get("first_observed", "")),
                last_reviewed=item.get("last_reviewed", item.get("last_confirmed", "")),
                source_type=item.get("source_type", ""),
                provenance={"note": item.get("provenance_note", "")},
            )
        )
    return atoms
