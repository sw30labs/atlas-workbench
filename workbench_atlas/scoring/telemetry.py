"""Telemetry (docs/research/redesignv1/telemetry-spec.md) — Phase 1 subset.

Every LLM call must go through the instrumented wrapper; the 100% coverage
invariant is testable (telemetry-spec section 9). Deterministic score jobs
and egress decisions also emit events. Events are JSONL; run reports
aggregate per ``run_id``.

Phase 1 runs local/det-only, so ``cost_usd`` is normally 0 with
``cost_shadow_usd`` populated from the pricing config when available.
Prices are never hardcoded in application logic: the default table is all
zeros with a version stamp, and a YAML file can override it.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import validate_scoring_plane

DEFAULT_PRICING: dict[str, Any] = {
    "version": "2026-08-01",
    "models": {},  # model_id -> {input_per_mtok, output_per_mtok, ...}
}

DEFAULT_PRODUCTION_LOG = Path("generated/telemetry/llm.jsonl")

_PRODUCTION_LOGGER: "TelemetryLogger | None" = None


def production_telemetry() -> "TelemetryLogger":
    """The production LLM telemetry sink (singleton): every instrumented
    OMLXClient in the app writes here. Costs page aggregates this file
    along with the other run logs in generated/telemetry/."""
    global _PRODUCTION_LOGGER
    if _PRODUCTION_LOGGER is None:
        _PRODUCTION_LOGGER = TelemetryLogger(DEFAULT_PRODUCTION_LOG, run_id="llm")
    return _PRODUCTION_LOGGER

PHASES = ("generation", "evaluation", "merge", "control", "eval_harness")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TelemetryLogger:
    """Append-only JSONL event log + derived run reports."""

    path: Path | str
    run_id: str = "run-local"
    pricing: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_PRICING))
    _events_written: int = 0

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- event emission ---------------------------------------------------

    def emit(
        self,
        *,
        node: str,
        phase: str,
        provider: str = "none",
        model_id: str = "",
        plane: str = "public",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        latency_ms: int = 0,
        retries: int = 0,
        status: str = "ok",
        error_class: str | None = None,
        cycle_id: str = "",
        branch_id: str = "main",
        checkpoint_id: str | None = None,
        scenario_id: str | None = None,
        realization_id: str | None = None,
        parent_event_id: str | None = None,
        config_hash: str = "",
        tags: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plane = validate_scoring_plane(plane)
        cost, shadow = self._costs(
            model_id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
        event = {
            "event_id": uuid.uuid4().hex,
            "ts": _utcnow(),
            "run_id": self.run_id,
            "cycle_id": cycle_id,
            "branch_id": branch_id,
            "checkpoint_id": checkpoint_id,
            "node": node,
            "phase": phase,
            "scenario_id": scenario_id,
            "realization_id": realization_id,
            "provider": provider,
            "model_id": model_id,
            "model_version": None,
            "plane": plane,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "latency_ms": latency_ms,
            "retries": retries,
            "status": status,
            "error_class": error_class,
            "unit_price": {
                "pricing_version": self.pricing.get("version", "unknown"),
                **self._model_pricing(model_id),
            },
            "cost_usd": cost,
            "cost_shadow_usd": shadow,
            "parent_event_id": parent_event_id,
            "config_hash": config_hash,
            "tags": tags or {},
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        self._events_written += 1
        return event

    def _model_pricing(self, model_id: str) -> dict[str, float]:
        return dict(self.pricing.get("models", {}).get(model_id, {}))

    def _costs(
        self,
        model_id: str,
        n_in: int,
        n_out: int,
        n_cache_read: int,
        n_cache_write: int,
    ) -> tuple[float, float]:
        p = self._model_pricing(model_id)
        cost = (
            n_in / 1e6 * p.get("input_per_mtok", 0.0)
            + n_out / 1e6 * p.get("output_per_mtok", 0.0)
            + n_cache_read / 1e6 * p.get("cache_read_per_mtok", 0.0)
            + n_cache_write / 1e6 * p.get("cache_write_per_mtok", 0.0)
        )
        shadow = (
            n_in / 1e6 * p.get("shadow_input_per_mtok", 0.0)
            + n_out / 1e6 * p.get("shadow_output_per_mtok", 0.0)
        )
        return cost, shadow

    # -- instrumented wrapper (the only sanctioned LLM path) ---------------

    def instrumented_call(
        self,
        fn: Callable[..., Any],
        *args: Any,
        node: str,
        phase: str,
        provider: str,
        model_id: str,
        plane: str = "public",
        **kwargs: Any,
    ) -> Any:
        """Wrap any LLM-ish callable; emit exactly one event per call.

        If the result is a dict carrying ``usage`` with token counts, those
        are recorded; otherwise tokens are 0 (e.g. stubs, local det paths).
        """
        start = time.perf_counter()
        status = "ok"
        error_class = None
        result: Any = None
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as exc:
            status = "error"
            error_class = type(exc).__name__
            raise
        finally:
            latency_ms = int((time.perf_counter() - start) * 1000)
            usage = result.get("usage", {}) if isinstance(result, dict) else {}
            self.emit(
                node=node,
                phase=phase,
                provider=provider,
                model_id=model_id,
                plane=plane,
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cache_read_tokens=int(usage.get("cache_read_tokens", 0)),
                cache_write_tokens=int(usage.get("cache_write_tokens", 0)),
                latency_ms=latency_ms,
                status=status,
                error_class=error_class,
            )

    # -- reporting ----------------------------------------------------------

    def load_events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # A process killed mid-append leaves a torn final line;
                # one bad line must not take down every telemetry consumer.
                continue
        return events

    def run_report(self, n_promoted: int = 0) -> dict[str, Any]:
        events = [e for e in self.load_events() if e["run_id"] == self.run_id]
        spend = sum(e["cost_usd"] for e in events)
        shadow = sum(e["cost_shadow_usd"] for e in events)
        by_phase: dict[str, float] = {}
        by_model: dict[str, float] = {}
        for e in events:
            by_phase[e["phase"]] = by_phase.get(e["phase"], 0.0) + e["cost_usd"]
            key = e["model_id"] or e["provider"]
            by_model[key] = by_model.get(key, 0.0) + e["cost_usd"]
        n_in = sum(e["input_tokens"] for e in events)
        n_out = sum(e["output_tokens"] for e in events)
        return {
            "run_id": self.run_id,
            "spend_usd": round(spend, 6),
            "spend_shadow_usd": round(shadow, 6),
            "n_promoted": n_promoted,
            "cost_per_promoted_usd": (round(spend / n_promoted, 6) if n_promoted else None),
            "tokens": {"input": n_in, "output": n_out},
            "by_phase": {k: round(v, 6) for k, v in sorted(by_phase.items())},
            "by_model": {k: round(v, 6) for k, v in sorted(by_model.items())},
            "cache_hit_rate": None,
            "marginal_last_5_cycles": None,
            "egress_denies": sum(1 for e in events if e["status"] == "denied_egress"),
            "n_events": len(events),
        }

    def write_run_report(self, out_path: str | Path, n_promoted: int = 0) -> Path:
        report = self.run_report(n_promoted=n_promoted)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True))
        return out


def load_pricing(path: str | Path) -> dict[str, Any]:
    """Load a pricing YAML (never hardcode prices in logic)."""
    import yaml

    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict) or "version" not in data:
        raise ValueError(f"pricing file {path} must be a mapping with a version key")
    return data
