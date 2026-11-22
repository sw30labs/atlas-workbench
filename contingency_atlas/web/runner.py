"""Background job runner for manual phase execution from the dashboard.

Runs one pipeline job at a time through a durable, bounded SQLite queue.
The worker holds a process-safe lock; abandoned runs become interrupted
and are never automatically replayed. The HTTP layer polls job status.
LLM phases (1–3) require a
reachable OMLX endpoint; phases 4–6 run offline. Phase 6 retirements stay
gated behind ATLAS_AUTO_RETIRE — the dashboard cannot override that.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from contingency_atlas.config import Settings, get_settings
from contingency_atlas.operations import (
    JobJournal,
    OperationsError,
    exclusive_file_lock,
    job_journal_path,
    settings_fingerprint,
    worker_lock_path,
)
from contingency_atlas.provenance import is_public_http_url

MAX_TEXT_CHARS = 200_000
MAX_EVIDENCE_TEXTS = 50
MAX_SCENARIOS = 10
VALID_PHASES = (
    "phase1", "phase2", "phase3", "phase4", "phase5", "phase6",
    "compound_join", "compound_graduate", "review_analyze",
    "review_research", "review_research_all", "full",
    # hybrid-scoring program triggers (dashboard cards): the four eval
    # phases are deterministic; the three elicitation phases are OMLX-gated
    "eval_r0_suite", "eval_r0_multiseed", "eval_ablations", "judgment_ledger",
    "channel_audit", "analogue_elicitation", "compound_join_llm",
    # organization onboarding: stage one reviewable batch from an allowed folder
    # (staging only; writes happen later through the import gate)
    "bulk_ingest",
)

logger = logging.getLogger(__name__)
_WORKERS: dict[str, threading.Thread] = {}
_WORKERS_LOCK = threading.Lock()
_EXECUTION_SETTINGS = threading.local()
_PARAMETER_NAMES = frozenset({
    "evidence_texts", "source_urls", "source_text", "risk_source_urls", "num_scenarios",
    "force", "kind", "record_id", "seed_domains", "steer", "canary", "with_refutations",
    "k", "max_joins", "mc_draws", "seeds", "top_n",
    "folder", "use_llm", "classification",
})
# Live state of in-process jobs that report progress and accept a cooperative
# cancel (bulk_ingest). Not durable: the journal stays the source of truth.
_LIVE: dict[str, dict[str, Any]] = {}
_LIVE_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Analysis phases recorded in the pipeline_runs history, mapped to the result
# key that counts their items. Phase 6 and the full pipeline log their own
# rows; review, research and eval jobs are not pipeline runs.
_RUN_HISTORY_ITEMS = {
    "phase1": "ingested", "phase2": "new_risks", "phase3": "inserted",
    "phase4": "scored", "phase5": "persisted",
    "compound_join": "n_written", "compound_graduate": "n_graduated",
}


def _log_run_history(settings: Settings, job: dict[str, Any], started: str,
                     status: str, result: dict[str, Any], error: str | None) -> None:
    if job["phase"] not in _RUN_HISTORY_ITEMS:
        return
    from contingency_atlas.db import log_pipeline_run

    items = result.get(_RUN_HISTORY_ITEMS[job["phase"]])
    errors = "; ".join(str(e) for e in result.get("errors") or []) or error
    try:
        log_pipeline_run(
            str(settings.scenario_db_path),
            run_id=job["id"],
            phase=job["phase"],
            status=status,
            started_at=started,
            completed_at=_now(),
            items_processed=items if isinstance(items, int) and not isinstance(items, bool) else 0,
            errors=errors,
        )
    except Exception as exc:  # history is a view; never fail the job over it
        logger.warning("Could not log pipeline run for job %s: %s", job["id"], exc)


def omlx_reachable(settings: Settings | None = None, timeout: float = 2.0) -> bool:
    """TCP-level reachability check for the active LLM endpoint (OMLX or DGX)."""
    settings = settings or get_settings()
    url = urlparse(settings.llm_base_url)
    host = url.hostname or "127.0.0.1"
    port = url.port or (443 if url.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def busy() -> bool:
    path = job_journal_path(get_settings())
    counts = JobJournal(path).counts() if path.is_file() else {}
    return bool(counts.get("queued") or counts.get("running"))


def get_job(job_id: str) -> dict[str, Any] | None:
    path = job_journal_path(get_settings())
    job = JobJournal(path).get(job_id) if path.is_file() else None
    if job is not None:
        with _LIVE_LOCK:
            live = _LIVE.get(job_id)
            if live is not None:
                job["progress"] = dict(live["progress"])
                job["cancel_requested"] = live["cancel"].is_set()
    return job


def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
    """Read recent durable jobs, without exposing their input text or secrets."""
    path = job_journal_path(get_settings())
    return JobJournal(path).list(limit) if path.is_file() else []


def _parameters(params: dict[str, Any]) -> dict[str, Any]:
    retained = {}
    for name in _PARAMETER_NAMES & params.keys():
        value = params[name]
        if not isinstance(value, (str, bool, int, float, type(None))):
            raise OperationsError(f"Job parameter {name!r} must be a scalar value")
        if isinstance(value, str) and len(value) > MAX_TEXT_CHARS:
            raise OperationsError(f"Job parameter {name!r} exceeds the text limit")
        retained[name] = value
    return retained


def start_job(phase: str, params: dict[str, Any]) -> tuple[str | None, str | None]:
    """Durably queue a phase. Returns (job_id, None) or (None, error)."""
    if phase not in VALID_PHASES:
        return None, f"unknown phase {phase!r}"
    settings = get_settings()
    try:
        journal = JobJournal(job_journal_path(settings))
        job_id = journal.enqueue(
            phase, _parameters(params),
            actor=str(params.get("actor") or "local operator"),
            settings_hash=settings_fingerprint(settings),
        )
        try:
            _ensure_worker(settings, journal)
        except RuntimeError:
            journal.note_queued(job_id, "Worker could not start; the job is saved. Use job recovery or restart.")
    except (OSError, ValueError, sqlite3.Error) as exc:
        return None, str(exc)
    return job_id, None


def _ensure_worker(settings: Settings, journal: JobJournal) -> None:
    key = str(journal.path.resolve())
    with _WORKERS_LOCK:
        if (existing := _WORKERS.get(key)) is not None and existing.is_alive():
            return
        thread = threading.Thread(
            target=_dispatch, args=(settings, journal), daemon=True,
            name=f"atlas-jobs-{journal.path.parent.name}",
        )
        _WORKERS[key] = thread
        try:
            thread.start()
        except BaseException:
            _WORKERS.pop(key, None)
            raise


def wait_for_jobs(timeout: float | None = None) -> None:
    """Keep CLI-hosted dispatchers alive; dashboard callers need not wait."""
    key = str(job_journal_path(get_settings()).resolve())
    with _WORKERS_LOCK:
        thread = _WORKERS.get(key)
    if thread is not None:
        thread.join(timeout=timeout)


def _dispatch(settings: Settings, journal: JobJournal) -> None:
    key = str(journal.path.resolve())
    current = threading.current_thread()
    try:
        with exclusive_file_lock(worker_lock_path(settings)):
            journal.interrupt_running()
            _EXECUTION_SETTINGS.settings = settings
            while True:
                # Synchronize the empty-queue exit with submission. Otherwise
                # a submission could see an exiting worker and remain orphaned.
                with _WORKERS_LOCK:
                    job = journal.claim()
                    if job is None:
                        if _WORKERS.get(key) is current:
                            _WORKERS.pop(key)
                        return
                if job["phase"] not in VALID_PHASES:
                    journal.finish(job["id"], "interrupted", error="The queued phase is no longer supported.")
                    continue
                if job["settings_hash"] != settings_fingerprint(settings):
                    journal.finish(job["id"], "interrupted", error=(
                        "Configuration changed after submission; explicitly retry to use the current settings."
                    ))
                    continue
                _EXECUTION_SETTINGS.job = {"id": job["id"], "actor": job.get("actor")}
                started = _now()
                try:
                    result = execute_phase(job["phase"], job["params"])
                    errors = result.get("errors") or []
                    failed = bool(errors) or result.get("status") in {"error", "failed", "completed_with_errors"}
                    error = "Phase reported errors; some outputs may have been saved." if failed else None
                    journal.finish(job["id"], "failed" if failed else "completed", result=result, error=error)
                    _log_run_history(settings, job, started,
                                     "completed_with_errors" if failed else "completed", result, error)
                except Exception as exc:
                    journal.finish(job["id"], "failed", error=str(exc))
                    _log_run_history(settings, job, started, "failed", {}, str(exc))
                finally:
                    _EXECUTION_SETTINGS.job = None
                    with _LIVE_LOCK:
                        _LIVE.pop(job["id"], None)
    except Exception:
        logger.exception("durable job worker stopped; recovery will reconcile its journal")
    finally:
        _EXECUTION_SETTINGS.settings = None
        with _WORKERS_LOCK:
            if _WORKERS.get(key) is current:
                _WORKERS.pop(key)


def recover_jobs(*, resume_queued: bool = True) -> dict[str, Any]:
    """Startup reconciliation; never replay a previously running phase."""
    settings = get_settings()
    path = job_journal_path(settings)
    if not path.is_file():
        return {"interrupted": 0, "queued": 0, "worker_active": False}
    journal = JobJournal(path)
    try:
        with exclusive_file_lock(worker_lock_path(settings), blocking=False):
            interrupted = journal.interrupt_running()
    except OperationsError:
        return {"interrupted": 0, "worker_active": True}
    queued = journal.counts().get("queued", 0)
    if resume_queued and queued:
        _ensure_worker(settings, journal)
    return {"interrupted": interrupted, "queued": queued, "worker_active": False}


def cancel_job(job_id: str, *, actor: str) -> dict[str, Any]:
    """Cancel a queued job, or ask a running cooperative job (bulk_ingest) to stop.

    Other running phases cannot be safely preempted. A cancelled bulk ingest
    stages nothing; it finishes with ``result.status == "cancelled"``.
    """
    with _LIVE_LOCK:
        live = _LIVE.get(job_id)
        if live is not None:
            if not isinstance(actor, str) or not actor.strip():
                raise OperationsError("A named operator is required")
            live["cancel"].set()
            live["progress"]["cancel_requested_by"] = actor.strip()[:160]
    if live is not None:
        job = get_job(job_id) or {"id": job_id}
        return {**job, "cancel_requested": True}
    path = job_journal_path(get_settings())
    if not path.is_file():
        raise OperationsError("Job was not found")
    return JobJournal(path).cancel(job_id, actor=actor)


def retry_job(job_id: str, *, actor: str) -> tuple[str | None, str | None]:
    """Explicitly submit retained arguments as a new, linked job."""
    settings = get_settings()
    path = job_journal_path(settings)
    if not path.is_file():
        return None, "Job was not found"
    journal = JobJournal(path)
    previous = journal.get(job_id, include_params=True)
    if previous is None or previous["state"] not in {"failed", "interrupted", "cancelled"}:
        return None, "Only failed, interrupted or cancelled jobs can be explicitly retried"
    try:
        if previous["phase"] not in VALID_PHASES:
            raise OperationsError("This phase is not supported by the current application")
        new_id = journal.enqueue(
            previous["phase"], _parameters(previous["params"]), actor=actor,
            settings_hash=settings_fingerprint(settings), retry_of=job_id,
        )
        _ensure_worker(settings, journal)
        return new_id, None
    except (OSError, ValueError, sqlite3.Error) as exc:
        return None, str(exc)


# ── parameter hygiene ──

def _texts(params: dict[str, Any]) -> list[str]:
    raw = str(params.get("evidence_texts") or "")[:MAX_TEXT_CHARS]
    return [t.strip() for t in raw.split("\n\n") if t.strip()][:MAX_EVIDENCE_TEXTS]


def _urls(params: dict[str, Any], key: str) -> list[str]:
    raw = str(params.get(key) or "")[:MAX_TEXT_CHARS]
    urls = [value.strip() for value in raw.splitlines() if value.strip()]
    valid: list[str] = []
    for value in urls[:MAX_EVIDENCE_TEXTS]:
        if not is_public_http_url(value):
            raise ValueError(f"Invalid public source URL: {value!r}")
        valid.append(value)
    return valid


def _text(params: dict[str, Any], key: str) -> str:
    return str(params.get(key) or "")[:MAX_TEXT_CHARS].strip()


def _num_scenarios(params: dict[str, Any]) -> int:
    raw = params.get("num_scenarios")
    if raw is None or raw == "":
        raw = 1
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 1
    return max(0, min(n, MAX_SCENARIOS))


def _int(params: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    """Clamped integer run parameter (mc_draws, seeds, k, top_n)."""
    try:
        n = int(params.get(key) or default)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


def _bulk_ingest(settings: Settings, params: dict[str, Any], *, reachable) -> dict[str, Any]:
    """Run the bulk-ingest pods over an allowed folder with live progress and cancel."""
    from contingency_atlas.bulk_ingest import run_bulk_ingest_job
    from contingency_atlas.web.bulk_ingest_web import resolve_ingest_folder

    folder = resolve_ingest_folder(settings, params.get("folder"))
    classification = str(params.get("classification") or "organization")
    if classification not in ("organization", "synthetic"):
        raise ValueError("classification must be 'organization' or 'synthetic'")
    use_llm = params.get("use_llm") is True
    if use_llm and not reachable(settings):
        raise RuntimeError(
            f"LLM backend ({settings.llm_provider}) unreachable at {settings.llm_base_url}; "
            "start it or run without the local LLM"
        )
    job = getattr(_EXECUTION_SETTINGS, "job", None) or {}
    job_id = job.get("id") or f"inline-{uuid.uuid4().hex[:8]}"
    actor = str(job.get("actor") or "local operator")
    cancel = threading.Event()
    live = {"progress": {"stage": "queued", "status": "running", "done": 0, "total": 0, "file": ""},
            "cancel": cancel}
    with _LIVE_LOCK:
        _LIVE[job_id] = live

    def progress(event: dict[str, Any]) -> None:
        keep = {k: event[k] for k in ("stage", "status", "file", "done", "total") if k in event}
        with _LIVE_LOCK:
            state = live["progress"]
            if keep.get("stage") != state.get("stage"):
                state.update(file="", done=0, total=0)
            state.update({k: (str(v)[:300] if isinstance(v, str) else v) for k, v in keep.items()})
            state["events"] = int(state.get("events") or 0) + 1

    client = None
    if use_llm:
        from contingency_atlas.llm import OMLXClient

        client = OMLXClient(settings, node="bulk_ingest")
    try:
        return run_bulk_ingest_job(
            {"folder": str(folder), "actor": actor, "use_llm": use_llm, "classification": classification},
            settings, progress=progress, client=client, cancel=cancel)
    finally:
        if client is not None:
            client.close()


# ── phase dispatch ──

def execute_phase(phase: str, params: dict[str, Any]) -> dict[str, Any]:
    from contingency_atlas.llm import OMLXClient
    from contingency_atlas.schema import migrate, migrate_evidence_events

    settings = getattr(_EXECUTION_SETTINGS, "settings", None) or get_settings()
    migrate(str(settings.scenario_db_path))
    migrate_evidence_events(str(settings.evidence_db_path))

    def llm_client() -> OMLXClient:
        if not omlx_reachable(settings):
            raise RuntimeError(
                f"LLM backend ({settings.llm_provider}) unreachable at "
                f"{settings.llm_base_url} — start it or pick an offline phase (4–6)"
            )
        from contingency_atlas.scoring.telemetry import production_telemetry

        return OMLXClient(settings, telemetry=production_telemetry(), node=phase)

    if phase == "bulk_ingest":
        return _bulk_ingest(settings, params, reachable=omlx_reachable)

    if phase == "phase1":
        from contingency_atlas.phases.phase1_ingest import ingest_batch

        texts = _texts(params)
        if not texts:
            raise ValueError(
                "Phase 1 needs at least one evidence text "
                "(blank line between items)"
            )
        source_urls = _urls(params, "source_urls")
        if len(source_urls) != len(texts):
            raise ValueError(
                "Phase 1 needs one source URL per evidence-text block"
            )
        client = llm_client()
        try:
            r = ingest_batch(
                texts,
                client,
                str(settings.evidence_db_path),
                source_urls=source_urls,
            )
            return {"ingested": len(r.events), "skipped": r.skipped, "errors": r.errors}
        finally:
            client.close()

    if phase == "phase2":
        from contingency_atlas.phases.phase2_risks import run_monitoring_cycle

        source = _text(params, "source_text")
        if not source:
            raise ValueError("Phase 2 needs source intelligence text")
        source_refs = _urls(params, "risk_source_urls")
        if not source_refs:
            raise ValueError("Phase 2 needs at least one canonical source URL")
        client = llm_client()
        try:
            r = run_monitoring_cycle(
                source,
                str(settings.emerging_risks_path),
                client,
                source_refs=source_refs,
            )
            return {
                "new_risks": len(r.new_risks),
                "updated": len(r.updated_risks),
                "faded": len(r.retired_risks),
            }
        finally:
            client.close()

    if phase == "phase3":
        from contingency_atlas.phases.phase3_generate import run_generation_cycle

        scenario_count = _num_scenarios(params)
        if scenario_count == 0:
            return {
                "generated": 0,
                "inserted": 0,
                "rejected": 0,
                "errors": [],
                "note": "No new scenarios requested; approved corpus was left unchanged.",
            }
        client = llm_client()
        try:
            r = run_generation_cycle(
                client,
                db_path=str(settings.scenario_db_path),
                emerging_risks_path=str(settings.emerging_risks_path),
                evidence_db_path=str(settings.evidence_db_path),
                num_scenarios=scenario_count,
                use_ralph_loop=True,
                judge_threshold=settings.judge_score_threshold,
                max_judge_iterations=settings.max_judge_iterations,
            )
            return {
                "generated": len(r.generated),
                "inserted": r.inserted,
                "rejected": r.rejected,
                "errors": r.errors,
            }
        finally:
            client.close()

    if phase == "phase4":
        from contingency_atlas.phases.phase4_prioritize import (
            get_prioritized_portfolio,
            run_prioritization_cycle,
        )

        db = str(settings.scenario_db_path)
        r = run_prioritization_cycle(db, None)  # offline deterministic scoring
        top = get_prioritized_portfolio(db, top_n=5)
        return {
            "scored": r.updated,
            "top": [p.get("scenario_id") for p in top],
            "errors": r.errors,
        }

    if phase == "phase5":
        from contingency_atlas.phases.phase5_patterns import persist_findings, run_pattern_analysis

        db = str(settings.scenario_db_path)
        r = run_pattern_analysis(
            db,
            client=None,
            evidence_db_path=str(settings.evidence_db_path),
            emerging_risks_path=str(settings.emerging_risks_path),
            use_llm_synthesis=False,
            lag_days=settings.temporal_lag_days,
            min_samples=settings.temporal_min_samples,
            n_permutations=settings.temporal_n_permutations,
        )
        return {
            "temporal": len(r.temporal_findings),
            "thematic": len(r.thematic_patterns),
            "persisted": persist_findings(db, r),
            "errors": r.errors,
        }

    if phase == "phase6":
        from contingency_atlas.phases.phase6_maintain import run_maintenance_cycle

        r = run_maintenance_cycle(
            str(settings.scenario_db_path),
            client=None,
            apply=True,
            apply_retire=bool(settings.auto_retire),  # env-gated, UI cannot override
            use_llm=False,
            run_id=f"web-{uuid.uuid4().hex[:8]}-phase6",
        )
        return {
            "merged": r.merged,
            "retired": r.retired,
            "kept": getattr(r, "kept", None),
            "stale": getattr(r, "flagged_stale", None),
            "actions": len(r.actions),
            "retire_applied": bool(settings.auto_retire),
            "errors": r.errors,
        }

    if phase == "compound_join":
        from contingency_atlas.compound_join import run_compound_join

        # Manual RUN forces the join (the monthly due gate applies to
        # pipeline runs, not to an explicit operator click). Offline and
        # deterministic — no OMLX needed.
        r = run_compound_join(settings, force=True)
        return {
            "status": r.status,
            "n_risks": r.n_risks,
            "n_pairs": r.n_pairs,
            "n_written": r.n_written,
            "note": r.note,
            "artifact": r.artifact,
            "errors": r.errors,
        }

    if phase == "review_analyze":
        from contingency_atlas.review_assist import run_review_analysis

        # Advisory pre-analysis of the pending review queue. Writes only
        # to the review_suggestions cache — never to review state.
        target_kind = str(params.get("kind") or "") or None
        target_id = str(params.get("record_id") or "") or None
        if target_kind is not None and target_kind not in ("event", "risk", "channel"):
            raise ValueError("kind must be 'event', 'risk' or 'channel'")
        client = llm_client()
        try:
            return run_review_analysis(
                settings,
                client,
                kind=target_kind,
                record_id=target_id,
                force=bool(params.get("force")),
            )
        finally:
            client.close()

    if phase == "review_research":
        from contingency_atlas.research import run_deep_research

        # Bounded web research for ONE pending record. Proposal-only:
        # writes a memo plus pending evidence candidates — never approvals.
        target_kind = str(params.get("kind") or "")
        target_id = str(params.get("record_id") or "")
        if target_kind not in ("event", "risk", "channel"):
            raise ValueError("kind must be 'event', 'risk' or 'channel'")
        if not target_id:
            raise ValueError("record_id is required")
        raw_domains = str(params.get("seed_domains") or "")
        seed_domains = [
            d.strip() for d in re.split(r"[\s,]+", raw_domains) if d.strip()
        ][:6]
        client = llm_client()
        try:
            return run_deep_research(
                settings,
                client,
                kind=target_kind,
                record_id=target_id,
                seed_domains=seed_domains,
            )
        finally:
            client.close()

    if phase == "review_research_all":
        from contingency_atlas.research import run_deep_research_all

        # Batch twin of review_research: bounded web research for EVERY
        # pending record with a fresh pre-analysis (the analyze →
        # research sequence, batch mode). Proposal-only, capped per run.
        target_kind = str(params.get("kind") or "") or None
        if target_kind is not None and target_kind not in ("event", "risk", "channel"):
            raise ValueError("kind must be 'event', 'risk' or 'channel'")
        client = llm_client()
        try:
            return run_deep_research_all(
                settings,
                client,
                kind=target_kind,
                force=bool(params.get("force")),
            )
        finally:
            client.close()

    if phase == "compound_graduate":
        from contingency_atlas.compound_graduate import run_compound_graduation

        client = llm_client()
        try:
            r = run_compound_graduation(settings, client=client)
            result: dict[str, Any] = {
                "status": r.status,
                "n_eligible": r.n_eligible,
                "n_graduated": r.n_graduated,
                "n_judge_rejected": r.n_judge_rejected,
                "graduated": r.graduated,
                "note": r.note,
                "errors": r.errors,
            }
            # P6 red-team memos: opt-in only (off by default) — display
            # artifacts, never a gate. Same client, same eligibility set.
            if params.get("with_refutations"):
                from contingency_atlas.refutations import run_refutation_pass

                memo_result = run_refutation_pass(settings, client)
                result["refutation_memos"] = memo_result["n_stored"]
                if memo_result["errors"]:
                    result["errors"] = r.errors + memo_result["errors"]
            return result
        finally:
            client.close()

    # ── hybrid-scoring program triggers ──
    # The four eval phases are deterministic (no OMLX); the two elicitation
    # phases are OMLX-gated and write ONLY pending proposals — decisions
    # stay in the Review queue (ADR-014).

    if phase == "eval_r0_suite":
        # Crash Test: the full go/no-go gauntlet on the synthetic world —
        # R0 generation + Task B/C + B-cal + Task L, eval_report written
        # next to the dataset (same layout as `atlas-eval demo`).
        from contingency_atlas.scoring.evaluate import run_evaluation, write_eval_outputs
        from contingency_atlas.scoring.scorer import ScorerConfig
        from contingency_atlas.scoring.synthetic import R0Config, generate_r0

        seed = 20260801
        mc_draws = _int(params, "mc_draws", 20_000, 1_000, 200_000)
        out = Path("generated/redesign/phase1/r0")
        ds = generate_r0(R0Config(seed=seed))
        ds.write(out)
        result = run_evaluation(
            ds.atoms,
            ds.events,
            truth=ds.truth,
            holdout_quarters=8,
            config=ScorerConfig(mc_draws=mc_draws, seed=seed),
        )
        write_eval_outputs(result, out)
        report = result.report
        task_b = report["task_b"]
        lift_ci = (task_b.get("bootstrap") or {}).get("ap_lift_b3_vs_b1_relative")
        bcal_modes = (report.get("task_bcal") or {}).get("modes") or {}
        bcal_row = bcal_modes.get(ScorerConfig().dependence_mode) or {}
        loglik = (report.get("task_loss") or {}).get("holdout_loglik") or {}
        return {
            "recommendation": report["recommendation"],
            "ap_lift": task_b["ap_lift_b3_vs_b1_relative"],
            "ap_lift_ci95": f"[{lift_ci[0]}, {lift_ci[1]}]" if lift_ci else None,
            "task_b_pass": task_b["pass"],
            "task_c_pass": (report.get("task_c") or {}).get("pass"),
            "bcal_coverage": bcal_row.get("coverage_90"),
            "bcal_pass": bcal_row.get("pass"),
            "task_l_fitted_minus_pooled": loglik.get("fitted_minus_pooled"),
            "artifact": str(out / "eval_report.json"),
        }

    if phase == "eval_r0_multiseed":
        # Parallel Worlds: the go/no-go metrics as a distribution over
        # generative seeds (pre-registered sequence base+i).
        from contingency_atlas.scoring.evaluate import run_r0_multiseed
        from contingency_atlas.scoring.scorer import ScorerConfig

        seed = 20260801
        n_seeds = _int(params, "seeds", 10, 2, 25)
        mc_draws = _int(params, "mc_draws", 20_000, 1_000, 200_000)
        report = run_r0_multiseed(
            n_seeds=n_seeds,
            base_seed=seed,
            holdout_quarters=8,
            config=ScorerConfig(mc_draws=mc_draws, seed=seed),
        )
        out = Path("generated/redesign/phase1/multiseed")
        out.mkdir(parents=True, exist_ok=True)
        report_path = out / "multiseed_report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
        summary = report["summary"]
        return {
            "seeds": summary["n_seeds"],
            "ap_lift_min": summary["ap_lift"]["min"],
            "ap_lift_median": summary["ap_lift"]["median"],
            "ap_lift_max": summary["ap_lift"]["max"],
            "task_b_pass_fraction": summary["task_b_pass_fraction"],
            "go_fraction": summary["go_fraction"],
            "report": str(report_path),
        }

    if phase == "eval_ablations":
        # What Breaks First?: pre-registered ablations + the kappa /
        # pi0 x c sensitivity grid + challenger-vs-primary B-cal.
        from contingency_atlas.scoring.ablations import run_ablations, write_ablation_outputs
        from contingency_atlas.scoring.scorer import ScorerConfig
        from contingency_atlas.scoring.synthetic import R0Config, generate_r0

        seed = 20260801
        mc_draws = _int(params, "mc_draws", 20_000, 1_000, 200_000)
        ds = generate_r0(R0Config(seed=seed))
        result = run_ablations(
            ds.atoms,
            ds.events,
            truth=ds.truth,
            holdout_quarters=8,
            scorer_config=ScorerConfig(mc_draws=mc_draws, seed=seed),
        )
        out = Path("generated/redesign/phase1/ablations")
        write_ablation_outputs(result, out)
        summary = result.report["summary"]
        challenger = (result.report.get("sensitivity") or {}).get("challenger_vs_primary") or {}
        one_liner = " · ".join(
            f"{mode}: coverage {m['coverage_90']} · supported-rare "
            f"{m['strata']['supported_rare']['coverage_90']}"
            for mode, m in challenger.items()
        ) or None
        return {
            "n_consistent": summary["n_consistent"],
            "n_runnable": summary["n_runnable"],
            "challenger_vs_primary": one_liner,
            "artifact": str(out / "ablations.json"),
        }

    if phase == "judgment_ledger":
        # Reality Check: record approved analogue claims with their frozen
        # forecasts, resolve against the R1 series, Brier over resolved
        # claims (an honest n=0 is a fine result). Same record -> resolve
        # -> report -> verify sequence as `atlas-eval ledger`.
        from contingency_atlas.scoring.judgment_ledger import (
            DEFAULT_LEDGER_PATH,
            calibration_report,
            record_analogue_claims,
            render_report_md,
            resolve_claims,
            verify_chain,
        )
        from contingency_atlas.scoring.r1 import build_signal_series

        ledger = DEFAULT_LEDGER_PATH
        n_recorded = record_analogue_claims(settings.scenario_db_path, ledger)
        events = []
        if settings.emerging_risks_path.exists():
            _atoms, events, _diag = build_signal_series(
                settings.emerging_risks_path, settings.evidence_db_path
            )
        if events:
            # same train/holdout split as compound_join (HOLDOUT_QUARTERS=2)
            from contingency_atlas.compound_join import HOLDOUT_QUARTERS

            n_periods = max(e.period for e in events) + 1
            resolve_claims(
                ledger,
                events,
                current_period=n_periods - 1,
                train_end_period=n_periods - HOLDOUT_QUARTERS - 1,
            )
        report = calibration_report(ledger)
        md = render_report_md(report)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        # tmp + rename: same atomic-write discipline as the join artifact
        for name, payload in (
            ("judgment_report.json", json.dumps(report, indent=2, sort_keys=True)),
            ("judgment_report.md", md),
        ):
            out_path = ledger.parent / name
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            tmp.write_text(payload)
            tmp.replace(out_path)
        ok, detail = verify_chain(ledger)
        return {
            "n_recorded": n_recorded,
            "n_resolved": report["n_resolved"],
            "n_open": report["n_open"],
            "brier": report["brier"] if report["brier"] is not None else "insufficient (n=0)",
            "chain_ok": ok,
            "chain": detail,
            "report": str(ledger.parent / "judgment_report.json"),
        }

    if phase == "channel_audit":
        # Hidden Connections: LLM audit of the governed register's channel
        # tags. Proposals land PENDING in the Review queue (auto-rejected
        # citations are recorded but never arrive).
        from contingency_atlas import proposals
        from contingency_atlas.provenance import is_approved_record
        from contingency_atlas.scoring.elicitation import channel_audit as audit_channels
        from contingency_atlas.scoring.inventory import load_risk_atoms

        if not omlx_reachable(settings):
            return {
                "status": "omlx_offline",
                "n_proposals_created": 0,
                "n_auto_rejected": 0,
                "note": f"LLM backend ({settings.llm_provider}) is offline — Hidden Connections is unavailable",
            }
        raw_risks = []
        if settings.emerging_risks_path.exists():
            try:
                data = json.loads(settings.emerging_risks_path.read_text())
                if isinstance(data, list):
                    raw_risks = [r for r in data if isinstance(r, dict)]
            except (json.JSONDecodeError, OSError):
                raw_risks = []
        # audit the atoms the join actually scores: approved records only
        approved_titles = {
            str(r.get("issue") or r.get("title") or "")
            for r in raw_risks
            if is_approved_record(r)
        }
        atoms = [
            a for a in load_risk_atoms(settings.emerging_risks_path)
            if a.title in approved_titles
        ] if raw_risks else []
        if not atoms:
            return {
                "status": "no_approved_risks",
                "n_proposals_created": 0,
                "n_auto_rejected": 0,
                "note": "no approved risks in the register — approve some first",
            }
        client = llm_client()
        try:
            rows = audit_channels(atoms, client)
        finally:
            client.close()
        errors = [r for r in rows if r.get("error")]
        candidates = [r for r in rows if not r.get("error")]
        stored = proposals.create_channel_proposals(
            settings.scenario_db_path, candidates
        )
        n_auto = sum(1 for r in candidates if r.get("status") == "auto_rejected")
        return {
            "status": "ok",
            "n_atoms_audited": len(atoms),
            "n_proposals_created": stored["created"] - n_auto,
            "n_auto_rejected": n_auto,
            "n_skipped_existing": stored["skipped_existing_pending"],
            "n_errors": len(errors),
            "note": "pending proposals are waiting in the Review queue — you dispose",
        }

    if phase == "analogue_elicitation":
        # Precedent: K cited analogue dossiers per top compound into
        # the Review queue; the optional canary runs the same elicitor on
        # fictional R0 atoms and reports the confabulation rate.
        from contingency_atlas import proposals
        from contingency_atlas.scoring.elicitation import (
            contamination_canary,
            elicit_analogues,
        )
        from contingency_atlas.scoring.inventory import load_risk_atoms
        from contingency_atlas.scoring.synthetic import R0Config, generate_r0

        if not omlx_reachable(settings):
            return {
                "status": "omlx_offline",
                "n_created": 0,
                "n_dropped": 0,
                "note": f"LLM backend ({settings.llm_provider}) is offline — Precedent is unavailable",
            }
        k = _int(params, "k", 3, 1, 10)
        top_n = _int(params, "top_n", 5, 1, 25)
        compound_rows: list[dict[str, Any]] = []
        try:
            conn = sqlite3.connect(str(settings.scenario_db_path))
            conn.row_factory = sqlite3.Row
            try:
                compound_rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT pair_id, risk_ids, state_hash, p_hat, p_lo, p_hi,"
                        " uncertainty_class, dependence_support, evidence_support"
                        " FROM compound_scenarios ORDER BY p_hat DESC LIMIT ?",
                        (top_n,),
                    )
                ]
            finally:
                conn.close()
        except sqlite3.Error:  # no compound store yet — honest note below
            compound_rows = []
        if not compound_rows:
            return {
                "status": "no_compounds",
                "n_created": 0,
                "n_dropped": 0,
                "note": "no compound scenarios yet — run Phase 7 (Compound Join) first",
            }
        titles = {}
        atom_planes = {}
        if settings.emerging_risks_path.exists():
            risk_atoms = load_risk_atoms(settings.emerging_risks_path)
            titles = {
                a.risk_id: a.title
                for a in risk_atoms
            }
            atom_planes = {a.risk_id: a.plane for a in risk_atoms}
        for row in compound_rows:
            row["atom_titles"] = titles
            row["atom_planes"] = atom_planes
        client = llm_client()
        canary: dict[str, Any] | None = None
        try:
            dicts = elicit_analogues(compound_rows, client, k=k)
            if params.get("canary"):
                # small fictional world keeps the canary to 15 elicitor calls
                canary = contamination_canary(
                    generate_r0(R0Config(n_atoms=6)).atoms, client
                )
        finally:
            client.close()
        candidates = [d for d in dicts if not d.get("error")]
        stored = proposals.create_analogues(settings.scenario_db_path, candidates)
        result: dict[str, Any] = {
            "status": "ok",
            "n_compounds": len(compound_rows),
            "n_created": stored["created"],
            "n_dropped": len(dicts) - stored["created"],
            "note": "pending dossiers are waiting in the Review queue — reviewed one by one",
        }
        if canary is not None:
            result["canary_rate"] = canary["canary_rate"]
            result["canary_confident"] = f"{canary['n_confident']}/{canary['n_elicited']}"
        return result

    if phase == "compound_join_llm":
        # Dark Matter Join: the LLM proposes couplings the channel graph
        # cannot see (pairs sharing NO channel tag); the production-fitted
        # deterministic scorer scores them. Proposals land PENDING in the
        # Review queue; every scored join is pre-registered in the judgment
        # ledger with its falsifiable signature. Display/shadow lane only —
        # production scores and graduation are untouched
        # (docs/research/llm-compound-join.md).
        from contingency_atlas.compound_join_llm import run_llm_compound_join

        if not omlx_reachable(settings):
            return {
                "status": "omlx_offline",
                "n_proposed": 0,
                "note": f"LLM backend ({settings.llm_provider}) is offline — Dark Matter Join is unavailable",
            }
        max_joins = _int(params, "max_joins", 12, 1, 25)
        client = llm_client()
        try:
            r = run_llm_compound_join(
                settings,
                client,
                max_joins=max_joins,
                canary=bool(params.get("canary")),
                steer=str(params.get("steer") or ""),
            )
        finally:
            client.close()
        result = {
            "status": r.status,
            "n_atoms": r.n_atoms,
            "n_visible_pairs": r.n_visible_pairs,
            "n_proposed": r.n_proposed,
            "n_dropped": r.n_dropped,
            "n_scored": r.n_scored,
            "n_proposals_created": r.n_proposals_created,
            "n_claims_frozen": r.n_claims_frozen,
            # Reported back so the job console shows a directed run as
            # directed — a steered search that looks undirected in the log is
            # the whole failure this feature has to avoid.
            "elicitation": r.elicitation,
            "steer": r.steer,
            "note": r.note,
            "artifact": r.artifact,
            "errors": r.errors,
        }
        if r.canary is not None:
            result["canary_joins"] = r.canary["n_joins"]
        return result

    # full pipeline — LLM phases run only when OMLX is reachable
    from contingency_atlas.pipeline import run_pipeline

    reachable = omlx_reachable(settings)
    result = run_pipeline(
        settings,
        evidence_texts=_texts(params),
        source_urls=_urls(params, "source_urls"),
        source_text=_text(params, "source_text"),
        risk_source_urls=_urls(params, "risk_source_urls"),
        num_scenarios=_num_scenarios(params),
        skip_llm=not reachable,
    )
    summary = result.summary()
    summary["skip_llm"] = not reachable
    return summary
