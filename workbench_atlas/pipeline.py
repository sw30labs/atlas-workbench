"""LangGraph orchestration of the Atlas Workbench eight-phase pipeline.

Runs phases sequentially with DriftLab trace hooks and Ralph-loop QA where
applicable. Execution is autonomous after the separate human provenance-review
boundary has admitted evidence and emerging risks.

Graph topology:
  start → phase1 → phase2 → phase3 → phase4 → phase5 → phase6
        → compound_join → compound_graduate → end

Each node can record DriftLab decision events for governance monitoring.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .config import Settings, get_settings
from .llm import OMLXClient
from .schema import migrate, migrate_evidence_events
from .db import log_pipeline_run, now_iso
from .driftlab_hooks import (
    create_trace_handler,
    record_phase_events,
    run_governance_check,
)

logger = logging.getLogger(__name__)


class PipelineState(TypedDict, total=False):
    """Shared state for the LangGraph pipeline."""
    run_id: str
    settings: dict[str, Any]
    # Phase results (serializable summaries)
    phase1: dict[str, Any]
    phase2: dict[str, Any]
    phase3: dict[str, Any]
    phase4: dict[str, Any]
    phase5: dict[str, Any]
    phase6: dict[str, Any]
    compound_join: dict[str, Any]
    compound_graduate: dict[str, Any]
    errors: list[str]
    driftlab_events: list[dict[str, Any]]
    driftlab_score: int | None
    current_phase: str
    # Optional inputs
    evidence_texts: list[str]
    source_urls: list[str]
    risk_source_urls: list[str]
    source_text: str
    num_scenarios: int
    skip_llm: bool


@dataclass
class PipelineResult:
    """Final pipeline outcome."""
    run_id: str
    state: dict[str, Any]
    driftlab_score: int | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "phases": {
                k: self.state.get(k) for k in
                ("phase1", "phase2", "phase3", "phase4", "phase5", "phase6",
                 "compound_join", "compound_graduate")
            },
            "driftlab_score": self.driftlab_score,
            "errors": self.errors,
        }


def _settings_dict(settings: Settings) -> dict[str, Any]:
    return settings.to_safe_dict()


def _client_from_state(state: PipelineState) -> OMLXClient:
    """Build an OMLX client from state + environment.

    The API key is intentionally NOT read from the serialized state. It is
    resolved from the OMLX_API_KEY environment variable or the default.
    Telemetry rides along: every LLM call in every phase emits an event
    (node label = current pipeline phase).
    """
    from .scoring.telemetry import production_telemetry

    s = state.get("settings") or {}
    # Pass llm_provider ONLY when the state actually carries one. Passing it
    # marks the field explicitly set, and the ATLAS_LLM_PROVIDER / LLM_PROVIDER
    # override is skipped for explicitly-set fields — so a default here would
    # silently route every pipeline client to OMLX on a DGX box.
    provider = (
        {"llm_provider": s["llm_provider"]} if "llm_provider" in s else {}
    )
    settings = Settings(
        scenario_db_path=Path(s.get("scenario_db_path", "atlas.db")),
        evidence_db_path=Path(s.get("evidence_db_path", "event_evidence.db")),
        emerging_risks_path=Path(s.get("emerging_risks_path", "emerging_risks.json")),
        **provider,
        omlx_base_url=s.get("omlx_base_url", "http://127.0.0.1:8000/v1"),
        omlx_model=s.get("omlx_model", "DeepSeek-V4-Flash-0731-MXFP4-MLX"),
        omlx_model_judge=s.get("omlx_model_judge", "DeepSeek-V4-Flash-0731-MXFP4-MLX"),
        omlx_model_final_judge=s.get("omlx_model_final_judge", ""),
        omlx_timeout=s.get("omlx_timeout", 600.0),
        omlx_max_tokens=s.get("omlx_max_tokens", 16384),
        omlx_use_responses_api=s.get("omlx_use_responses_api", False),
        dgx_base_url=s.get("dgx_base_url", "http://127.0.0.1:8000/v1"),
        dgx_model=s.get("dgx_model", "Qwen/Qwen3.6-27B-FP8"),
        dgx_model_judge=s.get("dgx_model_judge", "Qwen/Qwen3.6-27B-FP8"),
        dgx_model_final_judge=s.get("dgx_model_final_judge", ""),
        dgx_timeout=s.get("dgx_timeout", 600.0),
        dgx_max_tokens=s.get("dgx_max_tokens", 4096),
        dgx_enable_thinking=s.get("dgx_enable_thinking", False),
    )
    return OMLXClient(
        settings,
        telemetry=production_telemetry(),
        node=str(state.get("current_phase") or "pipeline"),
    )


def _close_client(client: OMLXClient | None) -> None:
    """Close a per-node HTTP client without masking the node outcome."""
    if client is None:
        return
    try:
        client.close()
    except Exception as exc:
        logger.warning("Could not close OMLX client: %s", exc)


# ── Phase nodes ──

def node_phase1(state: PipelineState) -> PipelineState:
    """Phase 1: News ingestion."""
    from .phases.phase1_ingest import ingest_batch

    state = dict(state)
    state["current_phase"] = "phase1"
    errors = list(state.get("errors") or [])
    events = list(state.get("driftlab_events") or [])
    events.append({"kind": "route", "node": "phase1", "route_from": "__start__", "route_to": "phase1"})

    texts = state.get("evidence_texts") or []
    source_urls = state.get("source_urls") or []
    if not texts:
        state["phase1"] = {"status": "skipped", "reason": "no evidence_texts provided"}
        events.append({"kind": "route", "node": "phase1", "route_from": "phase1", "route_to": "phase2"})
        state["driftlab_events"] = events
        return state  # type: ignore[return-value]

    if state.get("skip_llm"):
        state["phase1"] = {"status": "skipped", "reason": "skip_llm"}
        events.append({"kind": "route", "node": "phase1", "route_from": "phase1", "route_to": "phase2"})
        state["driftlab_events"] = events
        return state  # type: ignore[return-value]

    client = None
    try:
        client = _client_from_state(state)
        s = state["settings"]
        result = ingest_batch(
            texts,
            client,
            s["evidence_db_path"],
            source_urls=source_urls,
        )
        events.append({"kind": "tool_call", "node": "phase1", "tool": "ingest_batch"})
        state["phase1"] = {
            "status": "error" if result.errors else "ok",
            "ingested": len(result.events),
            "skipped": result.skipped,
            "errors": result.errors,
        }
        errors.extend(f"phase1: {message}" for message in result.errors)
    except Exception as e:
        errors.append(f"phase1: {e}")
        state["phase1"] = {"status": "error", "error": str(e)}
        events.append({"kind": "error", "node": "phase1", "payload": {"error": str(e)}})
    finally:
        _close_client(client)

    events.append({"kind": "route", "node": "phase1", "route_from": "phase1", "route_to": "phase2"})
    state["errors"] = errors
    state["driftlab_events"] = events
    return state  # type: ignore[return-value]


def node_phase2(state: PipelineState) -> PipelineState:
    """Phase 2: Emerging risk monitoring."""
    from .phases.phase2_risks import run_monitoring_cycle

    state = dict(state)
    state["current_phase"] = "phase2"
    errors = list(state.get("errors") or [])
    events = list(state.get("driftlab_events") or [])

    source = state.get("source_text") or ""
    source_refs = state.get("risk_source_urls") or []
    if not source:
        state["phase2"] = {"status": "skipped", "reason": "no source_text"}
        events.append({"kind": "route", "node": "phase2", "route_from": "phase2", "route_to": "phase3"})
        state["driftlab_events"] = events
        return state  # type: ignore[return-value]

    if state.get("skip_llm"):
        state["phase2"] = {"status": "skipped", "reason": "skip_llm"}
        events.append({"kind": "route", "node": "phase2", "route_from": "phase2", "route_to": "phase3"})
        state["driftlab_events"] = events
        return state  # type: ignore[return-value]

    client = None
    try:
        client = _client_from_state(state)
        s = state["settings"]
        result = run_monitoring_cycle(
            source,
            s["emerging_risks_path"],
            client,
            source_refs=source_refs,
        )
        events.append({"kind": "tool_call", "node": "phase2", "tool": "run_monitoring_cycle"})
        state["phase2"] = {
            "status": "error" if result.errors else "ok",
            "new_risks": len(result.new_risks),
            "updated": len(result.updated_risks),
            "faded": len(result.retired_risks),
            "errors": result.errors,
        }
        errors.extend(f"phase2: {message}" for message in result.errors)
    except Exception as e:
        errors.append(f"phase2: {e}")
        state["phase2"] = {"status": "error", "error": str(e)}
        events.append({"kind": "error", "node": "phase2", "payload": {"error": str(e)}})
    finally:
        _close_client(client)

    events.append({"kind": "route", "node": "phase2", "route_from": "phase2", "route_to": "phase3"})
    state["errors"] = errors
    state["driftlab_events"] = events
    return state  # type: ignore[return-value]


def node_phase3(state: PipelineState) -> PipelineState:
    """Phase 3: Scenario generation with Ralph Loop QA."""
    from .phases.phase3_generate import run_generation_cycle

    state = dict(state)
    state["current_phase"] = "phase3"
    errors = list(state.get("errors") or [])
    events = list(state.get("driftlab_events") or [])

    if state.get("skip_llm"):
        state["phase3"] = {"status": "skipped", "reason": "skip_llm"}
        events.append({"kind": "route", "node": "phase3", "route_from": "phase3", "route_to": "phase4"})
        state["driftlab_events"] = events
        return state  # type: ignore[return-value]

    client = None
    try:
        client = _client_from_state(state)
        s = state["settings"]
        n = state.get("num_scenarios")
        n = 1 if n is None else max(0, int(n))
        result = run_generation_cycle(
            client,
            db_path=s["scenario_db_path"],
            emerging_risks_path=s["emerging_risks_path"],
            evidence_db_path=s.get("evidence_db_path"),
            num_scenarios=n,
            use_ralph_loop=True,
            judge_threshold=s.get("judge_score_threshold", 0.75),
            max_judge_iterations=s.get("max_judge_iterations", 3),
        )
        events.append({"kind": "tool_call", "node": "phase3", "tool": "run_generation_cycle"})
        events.append({"kind": "tool_call", "node": "phase3", "tool": "ralph_loop_qa"})
        state["phase3"] = {
            "status": "error" if result.errors else "ok",
            "generated": len(result.generated),
            "inserted": result.inserted,
            "rejected": result.rejected,
            "errors": result.errors,
        }
        errors.extend(f"phase3: {message}" for message in result.errors)
    except Exception as e:
        errors.append(f"phase3: {e}")
        state["phase3"] = {"status": "error", "error": str(e)}
        events.append({"kind": "error", "node": "phase3", "payload": {"error": str(e)}})
    finally:
        _close_client(client)

    events.append({"kind": "route", "node": "phase3", "route_from": "phase3", "route_to": "phase4"})
    state["errors"] = errors
    state["driftlab_events"] = events
    return state  # type: ignore[return-value]


def node_phase4(state: PipelineState) -> PipelineState:
    """Phase 4: Prioritization & scoring."""
    from .phases.phase4_prioritize import run_prioritization_cycle, get_prioritized_portfolio

    state = dict(state)
    state["current_phase"] = "phase4"
    errors = list(state.get("errors") or [])
    events = list(state.get("driftlab_events") or [])

    client = None
    try:
        client = None if state.get("skip_llm") else _client_from_state(state)
        s = state["settings"]
        result = run_prioritization_cycle(s["scenario_db_path"], client)
        portfolio = get_prioritized_portfolio(s["scenario_db_path"], top_n=10)
        events.append({"kind": "tool_call", "node": "phase4", "tool": "run_prioritization_cycle"})
        state["phase4"] = {
            "status": "error" if result.errors else "ok",
            "scored": result.updated,
            "top_ids": [p.get("scenario_id") for p in portfolio[:5]],
            "errors": result.errors,
        }
        errors.extend(f"phase4: {message}" for message in result.errors)
        events.extend(
            {
                "kind": "error",
                "node": "phase4",
                "payload": {"error": str(message)},
            }
            for message in result.errors
        )
    except Exception as e:
        errors.append(f"phase4: {e}")
        state["phase4"] = {"status": "error", "error": str(e)}
        events.append({"kind": "error", "node": "phase4", "payload": {"error": str(e)}})
    finally:
        _close_client(client)

    events.append({"kind": "route", "node": "phase4", "route_from": "phase4", "route_to": "phase5"})
    state["errors"] = errors
    state["driftlab_events"] = events
    return state  # type: ignore[return-value]


def node_phase5(state: PipelineState) -> PipelineState:
    """Phase 5: Pattern detection & causal analysis."""
    from .phases.phase5_patterns import run_pattern_analysis, persist_findings

    state = dict(state)
    state["current_phase"] = "phase5"
    errors = list(state.get("errors") or [])
    events = list(state.get("driftlab_events") or [])

    client = None
    try:
        client = None if state.get("skip_llm") else _client_from_state(state)
        s = state["settings"]
        result = run_pattern_analysis(
            s["scenario_db_path"],
            client=client,
            evidence_db_path=s["evidence_db_path"],
            emerging_risks_path=s.get("emerging_risks_path"),
            use_llm_synthesis=not state.get("skip_llm"),
            lag_days=s.get("temporal_lag_days"),
            min_samples=s.get("temporal_min_samples"),
            n_permutations=s.get("temporal_n_permutations"),
        )
        written = persist_findings(s["scenario_db_path"], result)
        events.append({"kind": "tool_call", "node": "phase5", "tool": "run_pattern_analysis"})
        events.append({"kind": "tool_call", "node": "phase5", "tool": "causal_deep_agents"})
        state["phase5"] = {
            "status": "error" if result.errors else "ok",
            "temporal": len(result.temporal_findings),
            "thematic": len(result.thematic_patterns),
            "llm_synthesis": len(result.llm_synthesis),
            "persisted": written,
            "errors": result.errors,
        }
        errors.extend(f"phase5: {message}" for message in result.errors)
        events.extend(
            {
                "kind": "error",
                "node": "phase5",
                "payload": {"error": str(message)},
            }
            for message in result.errors
        )
    except Exception as e:
        errors.append(f"phase5: {e}")
        state["phase5"] = {"status": "error", "error": str(e)}
        events.append({"kind": "error", "node": "phase5", "payload": {"error": str(e)}})
    finally:
        _close_client(client)

    events.append({"kind": "route", "node": "phase5", "route_from": "phase5", "route_to": "phase6"})
    state["errors"] = errors
    state["driftlab_events"] = events
    return state  # type: ignore[return-value]


def node_phase6(state: PipelineState) -> PipelineState:
    """Phase 6: Maintenance & governance."""
    from .phases.phase6_maintain import run_maintenance_cycle

    state = dict(state)
    state["current_phase"] = "phase6"
    errors = list(state.get("errors") or [])
    events = list(state.get("driftlab_events") or [])

    client = None
    try:
        client = None if state.get("skip_llm") else _client_from_state(state)
        s = state["settings"]
        # Distinct run_id so phase6 does not collide with full_pipeline UNIQUE key
        base_run = state.get("run_id") or "run"
        result = run_maintenance_cycle(
            s["scenario_db_path"],
            client=client,
            apply=True,
            # Retire only when ATLAS_AUTO_RETIRE=1 (settings.auto_retire)
            apply_retire=bool(s.get("auto_retire", False)),
            use_llm=not state.get("skip_llm"),
            run_id=f"{base_run}-phase6",
            evidence_db_path=s.get("evidence_db_path"),
        )
        events.append({"kind": "tool_call", "node": "phase6", "tool": "run_maintenance_cycle"})
        state["phase6"] = {
            "status": "error" if result.errors else "ok",
            "retired": result.retired,
            "stale": result.flagged_stale,
            "actions": len(result.actions),
            "signposts_evaluated": getattr(
                result,
                "signposts_evaluated",
                0,
            ),
            "errors": result.errors,
        }
        errors.extend(f"phase6: {message}" for message in result.errors)
        events.extend(
            {
                "kind": "error",
                "node": "phase6",
                "payload": {"error": str(message)},
            }
            for message in result.errors
        )
    except Exception as e:
        errors.append(f"phase6: {e}")
        state["phase6"] = {"status": "error", "error": str(e)}
        events.append({"kind": "error", "node": "phase6", "payload": {"error": str(e)}})
    finally:
        _close_client(client)

    # Optional DriftLab comparison against the project-authored baseline
    if state.get("settings", {}).get("driftlab_enabled", False):
        baseline = state.get("settings", {}).get("driftlab_baseline_path")
        governance = run_governance_check(
            events,
            agent="workbench-atlas-pipeline",
            baseline_path=baseline,
        )
        if governance:
            state["driftlab_score"] = governance.get("score")
            events.append({
                "kind": "route",
                "node": "phase6",
                "route_from": "phase6",
                "route_to": "__end__",
                "payload": {"governance_score": governance.get("score")},
            })
        else:
            events.append({
                "kind": "route",
                "node": "phase6",
                "route_from": "phase6",
                "route_to": "__end__",
            })
    else:
        events.append({
            "kind": "route",
            "node": "phase6",
            "route_from": "phase6",
            "route_to": "__end__",
        })

    state["errors"] = errors
    state["driftlab_events"] = events
    return state  # type: ignore[return-value]


def node_compound_join(state: PipelineState) -> PipelineState:
    """Monthly compound join: join emerging risks into scored compound
    scenarios (deterministic; self-gating on cadence and data sufficiency)."""
    from .compound_join import run_compound_join

    state = dict(state)
    state["current_phase"] = "compound_join"
    errors = list(state.get("errors") or [])
    events = list(state.get("driftlab_events") or [])
    events.append({"kind": "route", "node": "compound_join",
                   "route_from": "phase6", "route_to": "__end__"})

    s = state["settings"]

    try:
        result = run_compound_join(
            Settings(
                scenario_db_path=Path(s.get("scenario_db_path", "atlas.db")),
                evidence_db_path=Path(s.get("evidence_db_path", "event_evidence.db")),
                emerging_risks_path=Path(s.get("emerging_risks_path", "emerging_risks.json")),
            )
        )
        events.append({"kind": "tool_call", "node": "compound_join", "tool": "run_compound_join"})
        state["compound_join"] = {
            "status": result.status,
            "n_risks": result.n_risks,
            "n_pairs": result.n_pairs,
            "n_written": result.n_written,
            "note": result.note,
            "artifact": result.artifact,
        }
        if result.status == "error":
            errors.extend(f"compound_join: {e}" for e in result.errors)
    except Exception as e:  # pragma: no cover - defensive; run_compound_join records internally
        errors.append(f"compound_join: {e}")
        state["compound_join"] = {"status": "error", "error": str(e)}
        events.append({"kind": "error", "node": "compound_join", "payload": {"error": str(e)}})

    state["errors"] = errors
    state["driftlab_events"] = events
    return state  # type: ignore[return-value]


def node_compound_graduate(state: PipelineState) -> PipelineState:
    """Phase 8: graduate proven compounds into the scenario library
    (LLM-drafted document, judge-gated, numbers from stored realizations)."""
    from .compound_graduate import run_compound_graduation

    state = dict(state)
    state["current_phase"] = "compound_graduate"
    errors = list(state.get("errors") or [])
    events = list(state.get("driftlab_events") or [])
    events.append({"kind": "route", "node": "compound_graduate",
                   "route_from": "compound_join", "route_to": "__end__"})

    if state.get("skip_llm"):
        state["compound_graduate"] = {"status": "skipped", "reason": "skip_llm"}
        state["driftlab_events"] = events
        return state  # type: ignore[return-value]

    s = state["settings"]
    client = None
    try:
        client = _client_from_state(state)
        result = run_compound_graduation(
            Settings(
                scenario_db_path=Path(s.get("scenario_db_path", "atlas.db")),
                evidence_db_path=Path(s.get("evidence_db_path", "event_evidence.db")),
                emerging_risks_path=Path(s.get("emerging_risks_path", "emerging_risks.json")),
            ),
            client=client,
        )
        events.append({"kind": "tool_call", "node": "compound_graduate",
                       "tool": "run_compound_graduation"})
        state["compound_graduate"] = {
            "status": result.status,
            "n_eligible": result.n_eligible,
            "n_graduated": result.n_graduated,
            "n_judge_rejected": result.n_judge_rejected,
            "graduated": result.graduated,
            "note": result.note,
        }
        if result.status == "error":
            errors.extend(f"compound_graduate: {e}" for e in result.errors)
    except Exception as e:
        errors.append(f"compound_graduate: {e}")
        state["compound_graduate"] = {"status": "error", "error": str(e)}
        events.append({"kind": "error", "node": "compound_graduate",
                       "payload": {"error": str(e)}})
    finally:
        _close_client(client)

    state["errors"] = errors
    state["driftlab_events"] = events
    return state  # type: ignore[return-value]


def build_pipeline() -> Any:
    """Build and compile the LangGraph StateGraph."""
    graph = StateGraph(PipelineState)
    graph.add_node("phase1", node_phase1)
    graph.add_node("phase2", node_phase2)
    graph.add_node("phase3", node_phase3)
    graph.add_node("phase4", node_phase4)
    graph.add_node("phase5", node_phase5)
    graph.add_node("phase6", node_phase6)
    graph.add_node("compound_join", node_compound_join)
    graph.add_node("compound_graduate", node_compound_graduate)

    graph.set_entry_point("phase1")
    graph.add_edge("phase1", "phase2")
    graph.add_edge("phase2", "phase3")
    graph.add_edge("phase3", "phase4")
    graph.add_edge("phase4", "phase5")
    graph.add_edge("phase5", "phase6")
    graph.add_edge("phase6", "compound_join")
    graph.add_edge("compound_join", "compound_graduate")
    graph.add_edge("compound_graduate", END)

    return graph.compile()


def run_pipeline(
    settings: Settings | None = None,
    *,
    evidence_texts: list[str] | None = None,
    source_urls: list[str] | None = None,
    source_text: str = "",
    risk_source_urls: list[str] | None = None,
    num_scenarios: int = 1,
    skip_llm: bool = False,
    run_id: str | None = None,
) -> PipelineResult:
    """Execute the full Atlas Workbench pipeline.

    Args:
        settings: Pipeline settings (defaults to get_settings())
        evidence_texts: Optional source-text packets for Phase 1 ingestion
        source_urls: One canonical public URL aligned to each evidence text
        source_text: Intelligence text for Phase 2 risk monitoring
        risk_source_urls: Canonical public URLs supporting the Phase 2 packet
        num_scenarios: How many scenarios to generate in Phase 3
        skip_llm: If True, skip LLM-heavy phases (for offline testing)
        run_id: Optional run identifier

    Returns:
        PipelineResult with per-phase summaries and DriftLab score
    """
    settings = settings or get_settings()
    run_id = run_id or f"atlas-{uuid.uuid4().hex[:10]}"
    started = now_iso()

    # Ensure schema is migrated
    migrate(str(settings.scenario_db_path))
    migrate_evidence_events(str(settings.evidence_db_path))

    # Optional DriftLab handler (observability only)
    handler = None
    if settings.driftlab_enabled:
        handler = create_trace_handler(
            agent=settings.driftlab_agent_name,
            run_id=run_id,
            sink=settings.driftlab_data_dir / "traces" / f"{run_id}.jsonl",
        )
        if handler:
            record_phase_events(handler, "pipeline_start", ["build_pipeline"],
                                routes=[("pipeline_start", "phase1")])

    initial: PipelineState = {
        "run_id": run_id,
        "settings": _settings_dict(settings),
        "errors": [],
        "driftlab_events": [],
        "evidence_texts": evidence_texts or [],
        "source_urls": source_urls or [],
        "source_text": source_text,
        "risk_source_urls": risk_source_urls or [],
        "num_scenarios": num_scenarios,
        "skip_llm": skip_llm,
    }

    app = build_pipeline()

    # Invoke the graph. If DriftLab is enabled, attach its LangChain callback
    # so node starts and tool calls are captured in real time.
    invoke_config = {"run_id": run_id}
    if handler:
        invoke_config["callbacks"] = [handler.as_langchain_handler()]

    final_state = app.invoke(initial, config=invoke_config)

    if handler:
        # Record semantic events the generic callback cannot infer
        # (phase transitions, high-level tool calls, errors, escalations).
        for ev in final_state.get("driftlab_events") or []:
            handler.record(
                ev.get("kind", "route"),
                node=ev.get("node", "pipeline"),
                tool=ev.get("tool"),
                route_from=ev.get("route_from"),
                route_to=ev.get("route_to"),
                payload=ev.get("payload", {}),
            )
        handler.commit()

    # Log overall run
    try:
        log_pipeline_run(
            str(settings.scenario_db_path),
            run_id=run_id,
            phase="full_pipeline",
            status="completed" if not final_state.get("errors") else "completed_with_errors",
            started_at=started,
            completed_at=now_iso(),
            items_processed=6,
            errors="; ".join(final_state.get("errors") or []) or None,
            driftlab_cycle_id=None,
            driftlab_score=final_state.get("driftlab_score"),
        )
    except Exception as e:
        logger.warning(f"Could not log full pipeline run: {e}")

    return PipelineResult(
        run_id=run_id,
        state=dict(final_state),
        driftlab_score=final_state.get("driftlab_score"),
        errors=list(final_state.get("errors") or []),
    )
