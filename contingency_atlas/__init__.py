"""Contingency Atlas — LLM-driven operational risk intelligence pipeline.

Eight-phase pipeline orchestrated by LangGraph (six core research phases plus
two compound-analysis phases):
  1. Ingestion & Normalization (evidence → DB)
  2. Risk Monitoring (emerging risks → JSON)
  3. Scenario Generation (risks + evidence → hypotheses)
  4. Prioritization & Scoring (scenarios → ranked portfolio)
  5. Pattern Detection (cross-source causal analysis)
  6. Maintenance & Governance (library health)
  7. Compound Join (co-materializing reviewed risks → candidates)
  8. Compound Graduation (holdout-qualified candidates → scenario library)

LLM-backed phases support an oMLX-compatible endpoint. DriftLab integration
provides optional runtime tracing and baseline checks.
"""

__version__ = "0.2.0"
