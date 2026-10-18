# Contingency Atlas Risk Taxonomy 1.0

The Contingency Atlas Risk Taxonomy is a compact, independently authored
classification used by this project. Earlier documentation may call it the
“Atlas taxonomy”; the qualified name is now preferred to distinguish it from
MITRE ATLAS™. It exists to make demo behavior deterministic and reviewable.
It is not an industry standard, regulatory classification, loss data taxonomy,
MITRE taxonomy, or compatibility claim.

The implementation lives in `contingency_atlas/categories.py`. Broad keyword
rules provide a suggested category; a human reviewer owns the final
classification. Categories may overlap, and `Multi-risk convergence` is the
fallback when no single domain is sufficiently supported.

## Risk categories

| Category | Scope |
|---|---|
| Digital resilience | Cyber events, identity controls, credentials, cloud or system availability |
| Third-party dependency | Vendors, suppliers, outsourcing, and shared-service concentration |
| Financial crime controls | Transaction monitoring, screening, sanctions, and fraud controls |
| Processing integrity | Processing, reconciliation, settlement, payment, change, and control failures |
| Data governance | Data quality, lineage, reference data, integrity, and record accuracy |
| Model risk | Model drift, algorithmic decisions, machine learning, and model failure |
| Workforce and conduct | Human action, insider risk, staffing, conduct, and customer treatment |
| Physical resilience | Facilities, severe weather, natural hazards, power, and site access |
| Legal and regulatory | Litigation, legal obligations, regulatory change, and compliance duties |
| Market infrastructure | Clearing, settlement, liquidity, collateral, and payment rails |
| Geopolitical disruption | Conflict, civil unrest, trade restrictions, and cross-border disruption |
| Multi-risk convergence | No single supported primary category or several co-equal domains |

## Event-driver classes

The driver class describes the immediate mechanism used by a scenario:

- `Technology disruption`
- `Third-party failure`
- `Process failure`
- `Human action`
- `External disruption`
- `Physical disruption`
- `Model failure`
- `Data integrity`
- `Cross-domain convergence`

## Primary-cause classes

The primary-cause class is a research hypothesis, not a finding of fact:

- `Architecture concentration`
- `Supplier concentration`
- `Change failure`
- `Monitoring gap`
- `Access control`
- `Data quality`
- `Site dependency`
- `Governance weakness`
- `External dependency`
- `Process design`

## External-driver overlay

The optional radar overlay classifies the broad change applying pressure to a
banking capability. It is separate from the risk category, event-driver class,
and primary-cause class:

- `Technology`
- `Institutional change`
- `Demographic change`
- `Environment`
- `Societal values`
- `Cross-driver convergence`

A record can carry several external drivers. `Cross-driver convergence` is
used when the interaction is itself the relevant signal, not as a default for
missing classification.

Emerging risks can also carry a maturity value (`weak`, `emerging`,
`accelerating`, `established`, `materialized`, or `fading`) and a momentum
value (`accelerating`, `stable`, or `slowing`). These labels describe research
workflow state; they are not probabilities or claims that an event will occur.

See [Radar, Signposts, and Atlas Briefs](docs/RADAR_AND_BRIEFS.md) for the
complete lifecycle and field semantics.

## Relationship to MITRE ATLAS

MITRE ATLAS describes adversary behavior against AI-enabled systems through
external tactics, techniques, mitigations, and case studies. The Contingency
Atlas Risk Taxonomy instead describes the banking operational-risk domain and
consequence. Neither vocabulary replaces the other.

The optional MITRE ATLAS integration therefore stores reviewed
scenario-to-technique mappings as a separate external-framework overlay. A
mapping does not change a scenario's native category, driver, cause, maturity,
likelihood, impact, urgency, or compound score. It records hypothesized
applicability, not observed adversary attribution. An ATLAS case study related
to a mapped technique is an upstream example and is not evidence that the
Contingency Atlas scenario occurred.

## Impact bands

Generated financial language is normalized for prioritization:

| Band | Illustrative parser threshold |
|---|---:|
| `Extreme` | USD 10 billion or more |
| `Major` | USD 1 billion to below USD 10 billion |
| `Significant` | USD 100 million to below USD 1 billion |
| `Moderate` | USD 10 million to below USD 100 million |
| `Limited` | A positive amount below USD 10 million |

Text such as “material,” “major,” or “significant” can also select a band when
no parseable amount exists. These thresholds are software heuristics for a
research demo. They are not calibrated severity estimates, capital measures,
forecasts, or advice.

The combined scenario impact uses `Critical`, `High`, `Medium`, and `Low`.
Likelihood remains qualitative and must not be read as a calibrated
probability.

## Versioning

Records identify the operational-risk taxonomy as `atlas-1.0`. The orthogonal
radar overlay is identified as `atlas-radar-1.0`. A change to category meaning,
driver or cause classes, impact thresholds, external-driver meaning, maturity
meaning, or matching precedence requires a new applicable version and
migration notes.

The stable `atlas-1.0` identifier is retained for compatibility even though the
display name is now “Contingency Atlas Risk Taxonomy.” MITRE ATLAS releases use
separate `mitre-atlas/<content-version>` coordinates. Their content versions,
format versions, technique maturity labels, and object identifiers never alter
or imply a new version of this taxonomy.
