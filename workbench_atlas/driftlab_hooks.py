"""DriftLab integration hooks for the Atlas Workbench pipeline.

DriftLab compares agent decision traces with a project-authored,
OSCAL-style governance baseline. We use DriftLabTraceHandler to capture
LangGraph node transitions and tool calls, then feed them to `run_cycle` for
diagnostic governance scoring. The result is not a certification.

Integration points:
  1. DriftLabTraceHandler attached to LangGraph invocations
  2. run_cycle for baseline comparison after each phase
  3. Pipeline run logging with DriftLab cycle IDs and scores
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# DriftLab imports are lazy — the pipeline works without DriftLab installed
_DriftLabTraceHandler = None
_run_cycle = None
_get_settings = None


def _import_driftlab():
    """Lazily import DriftLab. Returns True if available."""
    global _DriftLabTraceHandler, _run_cycle, _get_settings
    if _DriftLabTraceHandler is not None:
        return True
    try:
        from driftlab.ingest.langgraph_hook import DriftLabTraceHandler
        from driftlab.cycle import run_cycle
        from driftlab.config import get_settings as driftlab_get_settings

        _DriftLabTraceHandler = DriftLabTraceHandler
        _run_cycle = run_cycle
        _get_settings = driftlab_get_settings
        return True
    except ImportError as e:
        logger.warning(f"DriftLab not available: {e}")
        return False


def create_trace_handler(
    agent: str = "workbench-atlas-pipeline",
    run_id: str | None = None,
    sink: str | Path | None = None,
) -> Any:
    """Create a DriftLab trace handler for a LangGraph invocation.

    Returns a DriftLabTraceHandler, or None if DriftLab is not available.
    """
    if not _import_driftlab():
        return None
    import uuid
    run_id = run_id or str(uuid.uuid4())[:8]
    return _DriftLabTraceHandler(
        agent=agent,
        run_id=run_id,
        sink=sink,
    )


def run_governance_check(
    events: list[dict[str, Any]],
    agent: str = "workbench-atlas-pipeline",
    baseline_path: str | None = None,
) -> dict[str, Any] | None:
    """Run a DriftLab governance-baseline cycle on captured events.

    Returns a dict with score, cycle_id, and anomalies, or None if
    DriftLab is not available or no baseline is found.
    """
    if not _import_driftlab():
        return None

    from pathlib import Path as _Path

    # Prefer packaged workbench-atlas-pipeline baseline, then explicit arg, then DriftLab seed
    if baseline_path is None:
        packaged = (
            _Path(__file__).resolve().parent
            / "baselines"
            / "workbench-atlas-pipeline-baseline.json"
        )
        if packaged.is_file():
            baseline_path = str(packaged)
        else:
            try:
                import driftlab

                seed = _Path(driftlab.__file__).parent / "seed"
                candidates = []
                if seed.exists():
                    candidates.extend(seed.rglob("*.json"))
                    candidates.extend(seed.rglob("*.yaml"))
                baseline_path = str(candidates[0]) if candidates else None
            except ImportError:
                baseline_path = None

    # Guard: never pass a directory into run_cycle
    if not baseline_path or not _Path(baseline_path).is_file():
        logger.info("No DriftLab baseline file — using minimal inline baseline")
        return _minimal_governance_check(events, agent)

    # Convert raw events to DecisionEvents
    from driftlab.domain import DecisionEvent, EventKind

    decision_events = []
    for i, ev in enumerate(events):
        kind = ev.get("kind", "route")
        try:
            kind_enum = EventKind(kind)
        except ValueError:
            kind_enum = EventKind.ROUTE
        de = DecisionEvent(
            agent=agent,
            seq=i,
            kind=kind_enum,
            node=ev.get("node", "unknown"),
            tool=ev.get("tool"),
            route_from=ev.get("route_from"),
            route_to=ev.get("route_to"),
            payload=ev.get("payload", {}),
        )
        decision_events.append(de)

    try:
        result = _run_cycle(
            agent=agent,
            profile_path=baseline_path,
            events=decision_events,
            run_experiments=False,  # Skip experiments for speed
        )
        return {
            "agent": agent,
            "cycle_id": result.cycle_id,
            "score": result.score,
            "anomalies": len(result.anomalies),
            "anomaly_details": [
                {"control": a.control, "severity": a.severity.value}
                for a in result.anomalies
            ],
        }
    except Exception as e:
        logger.warning(f"DriftLab governance check failed: {e}")
        return None


def _minimal_governance_check(
    events: list[dict[str, Any]], agent: str
) -> dict[str, Any]:
    """Minimal diagnostic check without a full OSCAL-style baseline.

    The score is driven by errors ALONE. The docstring used to claim tool
    calls, routes and escalations produced it; they were counted and then
    discarded, so a run with escalations and zero errors scored a clean 100.
    They are now reported alongside the score rather than folded into it —
    deducting for an escalation would change what a governance score means,
    and that is a decision to take deliberately rather than to smuggle in
    while fixing a dead assignment.
    """
    tool_calls = sum(1 for e in events if e.get("kind") == "tool_call")
    routes = sum(1 for e in events if e.get("kind") == "route")
    escalations = sum(1 for e in events if e.get("kind") == "escalation")
    errors = sum(1 for e in events if e.get("kind") == "error")

    # Simple scoring: start at 100, deduct for errors
    score = max(0, 100 - errors * 20)
    anomalies = []
    if errors > 0:
        anomalies.append({
            "control": "DL-4",
            "severity": "high" if errors > 2 else "medium",
        })

    return {
        "agent": agent,
        "cycle_id": f"inline-{hash(tuple(str(e) for e in events)) & 0xFFFF:04x}",
        "score": score,
        "anomalies": len(anomalies),
        "anomaly_details": anomalies,
        "counts": {
            "tool_calls": tool_calls,
            "routes": routes,
            "escalations": escalations,
            "errors": errors,
        },
    }


def record_phase_events(
    handler: Any,
    phase: str,
    tools_called: list[str],
    routes: list[tuple[str, str]] | None = None,
) -> None:
    """Record phase events to a DriftLab trace handler.

    Call this at the start and end of each phase to capture the pipeline's
    decision trace for governance monitoring.
    """
    if handler is None:
        return

    # Record route entry
    handler.record("route", node=phase, route_from="__start__", route_to=phase)

    # Record tool calls
    for tool in tools_called:
        handler.record("tool_call", node=phase, tool=tool)

    # Record route exit
    if routes:
        for from_node, to_node in routes:
            handler.record("route", node=from_node, route_from=from_node, route_to=to_node)
    else:
        handler.record("route", node=phase, route_from=phase, route_to="__end__")
