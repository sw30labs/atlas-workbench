"""Deterministic alignment to the independent Atlas 1.0 taxonomy.

The cleanup proposes transparent category changes from generic keyword rules.
It does not call an LLM or consult an external scenario library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..categories import infer_category
from ..db import list_scenarios, update_scenario_fields

logger = logging.getLogger(__name__)


@dataclass
class CategoryChange:
    scenario_id: str
    old_category: str
    new_category: str
    matched_rule: str
    title: str = ""


@dataclass
class CleanupResult:
    changed: list[CategoryChange] = field(default_factory=list)
    unchanged: int = 0
    applied: int = 0
    errors: list[str] = field(default_factory=list)


def propose_category_cleanup(
    scenarios: list[dict[str, Any]],
) -> CleanupResult:
    """Propose category changes where inferred category differs from stored."""
    result = CleanupResult()
    for s in scenarios:
        sid = s.get("scenario_id") or ""
        old = s.get("risk_category") or ""
        new, kw = infer_category(s)
        if new is None or new == old:
            result.unchanged += 1
            continue
        result.changed.append(CategoryChange(
            scenario_id=sid,
            old_category=old,
            new_category=new,
            matched_rule=kw,
            title=(s.get("title") or "")[:80],
        ))
    return result


def apply_category_cleanup(
    db_path: str,
    *,
    dry_run: bool = False,
) -> CleanupResult:
    """Load scenarios, propose cleanup, optionally apply updates."""
    scenarios = list_scenarios(db_path, status=None)
    result = propose_category_cleanup(scenarios)
    if dry_run:
        logger.info(
            "Category cleanup dry-run: %d changes, %d unchanged",
            len(result.changed), result.unchanged,
        )
        return result

    for ch in result.changed:
        try:
            ok = update_scenario_fields(
                db_path, ch.scenario_id,
                {"risk_category": ch.new_category},
            )
            if ok:
                result.applied += 1
                logger.info(
                    "Category %s: %s → %s (rule=%s)",
                    ch.scenario_id, ch.old_category, ch.new_category, ch.matched_rule,
                )
        except Exception as e:
            result.errors.append(f"{ch.scenario_id}: {e}")

    logger.info(
        "Category cleanup applied: %d/%d changes",
        result.applied, len(result.changed),
    )
    return result
