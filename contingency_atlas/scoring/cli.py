"""CLI entry points for the redesign Phase 1 falsifiable core.

Console scripts (pyproject ``[project.scripts]``):

* ``atlas-score r0-generate`` — write the R0 synthetic dataset.
* ``atlas-score pairs`` — fit the deterministic scorer on the train window
  and score every pair; dump full ScoreRealization components + telemetry.
* ``atlas-eval run`` — Task B (+ Task C with ``--truth``) on holdout data;
  writes eval_report.json / eval_report.md.
* ``atlas-eval demo`` — one-command reproduction: generate R0, score, eval.
* ``atlas-eval synthetic-cell-demo`` — fictional grouped-event scoring demo.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .candidates import all_pairs
from .channels import build_channel_graph
from .evaluate import run_evaluation, write_eval_outputs
from .scorer import DetScorer, ScorerConfig
from .synthetic import R0Config, generate_r0, load_r0_events, load_r0_risks
from .telemetry import TelemetryLogger

DEFAULT_OUT = Path("generated/redesign/phase1")


def _add_common_eval_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--risks", type=Path, help="RiskAtom JSON (e.g. r0_risks.json)")
    parser.add_argument("--events", type=Path, help="incident events JSONL (e.g. r0_events.jsonl)")
    parser.add_argument("--out", type=Path, default=None, help="output directory")
    parser.add_argument("--holdout-quarters", type=int, default=8)
    parser.add_argument("--mc-draws", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20260801)


def _load_inputs(args: argparse.Namespace):
    if not args.risks or not args.events:
        raise SystemExit("--risks and --events are required")
    atoms = load_r0_risks(args.risks)
    events = load_r0_events(args.events)
    return atoms, events


# ---------------------------------------------------------------------------
# atlas-score
# ---------------------------------------------------------------------------


def score_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-score", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("r0-generate", help="generate the R0 synthetic dataset")
    p_gen.add_argument("--out", type=Path, default=DEFAULT_OUT / "r0")
    p_gen.add_argument("--seed", type=int, default=20260801)
    p_gen.add_argument("--quarters", type=int, default=40)
    p_gen.add_argument("--atoms", type=int, default=15)
    p_gen.add_argument("--channels", type=int, default=3)

    p_pairs = sub.add_parser("pairs", help="score all pairs (deterministic batch)")
    _add_common_eval_args(p_pairs)
    p_pairs.add_argument(
        "--train-quarters",
        type=int,
        default=None,
        help="fit on the first N quarters (default: all minus --holdout-quarters)",
    )

    args = parser.parse_args(argv)

    if args.command == "r0-generate":
        ds = generate_r0(
            R0Config(
                n_atoms=args.atoms,
                n_channels=args.channels,
                n_quarters=args.quarters,
                seed=args.seed,
            )
        )
        paths = ds.write(args.out)
        print(f"R0 dataset written to {args.out}")
        for name, path in paths.items():
            print(f"  {name}: {path}")
        return 0

    if args.command == "pairs":
        atoms, events = _load_inputs(args)
        out = args.out or (DEFAULT_OUT / "pairs")
        out.mkdir(parents=True, exist_ok=True)
        n_total = max(e.period for e in events) + 1
        train_quarters = args.train_quarters or (n_total - args.holdout_quarters)
        cfg = ScorerConfig(mc_draws=args.mc_draws, seed=args.seed)
        scorer = DetScorer(cfg).fit(events, atoms, train_quarters)

        logger = TelemetryLogger(out / "telemetry.jsonl", run_id=f"pairs-{cfg.seed}")
        pairs = all_pairs(atoms)
        realizations = scorer.score_pairs(pairs)
        for pair, rlz in zip(pairs, realizations):
            logger.emit(
                node="score_det",
                phase="evaluation",
                provider="none",
                model_id="det-scorer",
                plane="aggregate",
                scenario_id=rlz.scenario_id,
                realization_id=rlz.realization_id,
                config_hash=cfg.config_hash(),
                tags={"pair": list(pair), "mc_draws": cfg.mc_draws},
            )
        rlz_path = out / "score_realizations.jsonl"
        with rlz_path.open("w") as fh:
            for rlz in realizations:
                fh.write(json.dumps(rlz.to_dict(), sort_keys=True) + "\n")
        report_path = logger.write_run_report(out / "run_report.json")
        print(f"scored {len(realizations)} pairs (train quarters: 0..{train_quarters - 1})")
        print(f"  realizations: {rlz_path}")
        print(f"  telemetry:    {logger.path}")
        print(f"  run report:   {report_path}")
        return 0

    return 2


# ---------------------------------------------------------------------------
# atlas-eval
# ---------------------------------------------------------------------------


def eval_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run Task B (+ Task C) on holdout data")
    _add_common_eval_args(p_run)
    p_run.add_argument("--truth", type=Path, default=None, help="R0 truth manifest (enables Task C)")
    p_run.add_argument("--strict", action="store_true", help="exit 1 unless recommendation is GO")

    p_ms = sub.add_parser("multiseed", help="multi-seed R0: Task B/C as a distribution over generative seeds")
    p_ms.add_argument("--out", type=Path, default=DEFAULT_OUT / "multiseed")
    p_ms.add_argument("--seeds", type=int, default=10)
    p_ms.add_argument("--quarters", type=int, default=40)
    p_ms.add_argument("--holdout-quarters", type=int, default=8)
    p_ms.add_argument("--mc-draws", type=int, default=20_000)
    p_ms.add_argument("--seed", type=int, default=20260801)

    p_bcal = sub.add_parser("bcal", help="Task B-cal: compound level calibration vs generative truth")
    p_bcal.add_argument("--risks", type=Path, required=True)
    p_bcal.add_argument("--events", type=Path, required=True)
    p_bcal.add_argument("--truth", type=Path, required=True)
    p_bcal.add_argument("--out", type=Path, default=DEFAULT_OUT / "bcal")
    p_bcal.add_argument("--holdout-quarters", type=int, default=8)
    p_bcal.add_argument("--mc-draws", type=int, default=20_000)
    p_bcal.add_argument("--seed", type=int, default=20260801)
    p_bcal.add_argument(
        "--dependence-modes",
        type=str,
        default="shrunk_empirical",
        help="comma-separated estimators to compare (e.g. shrunk_empirical,mixture)",
    )

    p_loss = sub.add_parser("loss", help="Task L: loss-side evaluation vs generative truth")
    p_loss.add_argument("--risks", type=Path, required=True)
    p_loss.add_argument("--events", type=Path, required=True)
    p_loss.add_argument("--truth", type=Path, required=True)
    p_loss.add_argument("--out", type=Path, default=DEFAULT_OUT / "loss")
    p_loss.add_argument("--holdout-quarters", type=int, default=8)
    p_loss.add_argument("--seed", type=int, default=20260801)

    p_demo = sub.add_parser("demo", help="one-command R0 reproduction: generate + eval")
    p_demo.add_argument("--out", type=Path, default=DEFAULT_OUT / "r0")
    p_demo.add_argument("--seed", type=int, default=20260801)
    p_demo.add_argument("--quarters", type=int, default=40)
    p_demo.add_argument("--holdout-quarters", type=int, default=8)
    p_demo.add_argument("--mc-draws", type=int, default=20_000)

    p_cal = sub.add_parser("calibrate", help="Task A: atom frequency calibration on holdout")
    p_cal.add_argument("--risks", type=Path, required=True)
    p_cal.add_argument("--events", type=Path, required=True)
    p_cal.add_argument("--out", type=Path, default=DEFAULT_OUT / "calibration")
    p_cal.add_argument("--holdout-quarters", type=int, default=8)
    p_cal.add_argument("--seed", type=int, default=20260801)

    p_r1 = sub.add_parser("r1", help="Task B on the R1 public signal series (or documented insufficiency)")
    p_r1.add_argument("--risks", type=Path, default=Path("emerging_risks.json"))
    p_r1.add_argument("--evidence-db", type=Path, default=Path("event_evidence.db"))
    p_r1.add_argument("--out", type=Path, default=DEFAULT_OUT / "r1")
    p_r1.add_argument("--holdout-quarters", type=int, default=2)
    p_r1.add_argument("--mc-draws", type=int, default=20_000)
    p_r1.add_argument("--seed", type=int, default=20260801)

    p_abl = sub.add_parser("ablations", help="pre-registered ablations A1-A6 (evaluation-plan section 5)")
    p_abl.add_argument("--risks", type=Path, required=True)
    p_abl.add_argument("--events", type=Path, required=True)
    p_abl.add_argument("--truth", type=Path, default=None)
    p_abl.add_argument("--out", type=Path, default=DEFAULT_OUT / "ablations")
    p_abl.add_argument("--holdout-quarters", type=int, default=8)
    p_abl.add_argument("--mc-draws", type=int, default=20_000)
    p_abl.add_argument("--seed", type=int, default=20260801)
    p_abl.add_argument("--with-judge", action="store_true", help="run A4 with a stub coherence judge")

    p_cell_demo = sub.add_parser(
        "synthetic-cell-demo",
        help="fictional events -> scoring-cell aggregation -> egress checks -> Task B",
    )
    p_cell_demo.add_argument(
        "--out",
        type=Path,
        default=Path("generated/scoring/synthetic-cell-demo"),
    )
    p_cell_demo.add_argument("--quarters", type=int, default=24)
    p_cell_demo.add_argument("--holdout-quarters", type=int, default=4)
    p_cell_demo.add_argument("--min-group-size", type=int, default=5)
    p_cell_demo.add_argument("--mc-draws", type=int, default=20_000)
    p_cell_demo.add_argument("--seed", type=int, default=20260801)

    p_ext = sub.add_parser("ext-demo", help="external feature screen on synthetic planted series (FDR + nulls)")
    p_ext.add_argument("--risks", type=Path, required=True)
    p_ext.add_argument("--events", type=Path, required=True)
    p_ext.add_argument("--out", type=Path, default=DEFAULT_OUT / "ext-features")
    p_ext.add_argument("--holdout-quarters", type=int, default=8)
    p_ext.add_argument("--min-support", type=int, default=8)
    p_ext.add_argument("--seed", type=int, default=20260801)
    # P6 adversarial null, default-on: the LLM null pass is attempted
    # whenever an OMLX endpoint is configured (offline stays fail-closed
    # and recorded). --with-llm-null is kept as a backwards-compatible
    # no-op; --no-llm-null restores the deterministic-only screen.
    p_ext.add_argument("--with-llm-null", action="store_true",
                       help="(deprecated no-op — the LLM adversarial null pass is default-on)")
    p_ext.add_argument("--no-llm-null", action="store_true",
                       help="disable the LLM adversarial null pass (deterministic nulls only)")

    # P7 judgment ledger (HYBRID_SCORING_PROPOSAL.md; wave 6). No flags =
    # record + resolve + report in that order.
    p_led = sub.add_parser("ledger", help="P7 judgment ledger: record claims, resolve, report")
    p_led.add_argument("--db", type=Path, default=None,
                       help="scenario db (default: settings scenario_db_path)")
    p_led.add_argument("--ledger", type=Path, default=None,
                       help="ledger path (default: generated/judgment/ledger.jsonl)")
    p_led.add_argument("--record", action="store_true",
                       help="append approved-analogue claims with frozen forecasts")
    p_led.add_argument("--resolve", action="store_true",
                       help="run the quarterly resolution job on the R1 series")
    p_led.add_argument("--report", action="store_true",
                       help="print the calibration report + write judgment_report.json/md")
    p_led.add_argument("--risks", type=Path, default=None,
                       help="emerging risks JSON for --resolve (default: settings)")
    p_led.add_argument("--evidence-db", type=Path, default=None,
                       help="evidence db for --resolve (default: settings)")

    args = parser.parse_args(argv)

    if args.command == "demo":
        ds = generate_r0(R0Config(n_quarters=args.quarters, seed=args.seed))
        paths = ds.write(args.out)
        result = run_evaluation(
            ds.atoms,
            ds.events,
            truth=ds.truth,
            holdout_quarters=args.holdout_quarters,
            config=ScorerConfig(mc_draws=args.mc_draws, seed=args.seed),
        )
        outputs = write_eval_outputs(result, args.out)
        _print_eval_summary(result.report, outputs, extra=paths)
        return 0 if result.report["recommendation"] == "go" else 1

    if args.command == "run":
        atoms, events = _load_inputs(args)
        out = args.out or (DEFAULT_OUT / "eval")
        truth = json.loads(args.truth.read_text()) if args.truth else None
        result = run_evaluation(
            atoms,
            events,
            truth=truth,
            holdout_quarters=args.holdout_quarters,
            config=ScorerConfig(mc_draws=args.mc_draws, seed=args.seed),
        )
        outputs = write_eval_outputs(result, out)
        _print_eval_summary(result.report, outputs)
        if args.strict and result.report["recommendation"] != "go":
            return 1
        return 0

    if args.command == "multiseed":
        from .evaluate import run_r0_multiseed

        report = run_r0_multiseed(
            n_seeds=args.seeds,
            base_seed=args.seed,
            holdout_quarters=args.holdout_quarters,
            config=ScorerConfig(mc_draws=args.mc_draws, seed=args.seed),
            n_quarters=args.quarters,
        )
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "multiseed_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True)
        )
        s = report["summary"]
        print(f"multi-seed R0 ({s['n_seeds']} seeds, base {s['base_seed']}):")
        print(f"  AP lift min/q10/median/q90/max: "
              f"{s['ap_lift']['min']} / {s['ap_lift']['q10']} / "
              f"{s['ap_lift']['median']} / {s['ap_lift']['q90']} / {s['ap_lift']['max']}")
        print(f"  Task B pass: {s['task_b_pass_fraction']} · "
              f"Task C pass: {s['task_c_pass_fraction']} · GO: {s['go_fraction']}")
        print(f"  report: {args.out / 'multiseed_report.json'}")
        return 0

    if args.command == "bcal":
        from .task_bcal import run_bcal, write_bcal_outputs

        atoms, events = _load_inputs(args)
        truth = json.loads(args.truth.read_text())
        modes = tuple(m.strip() for m in args.dependence_modes.split(",") if m.strip())
        result = run_bcal(
            atoms,
            events,
            truth,
            holdout_quarters=args.holdout_quarters,
            config=ScorerConfig(mc_draws=args.mc_draws, seed=args.seed),
            dependence_modes=modes,
        )
        outputs = write_bcal_outputs(result, args.out)
        print(result.report_md)
        for name, path in outputs.items():
            print(f"  bcal_report.{name}: {path}")
        return 0

    if args.command == "loss":
        from .task_loss import run_task_loss, write_loss_outputs

        atoms, events = _load_inputs(args)
        truth = json.loads(args.truth.read_text())
        result = run_task_loss(
            atoms,
            events,
            truth,
            holdout_quarters=args.holdout_quarters,
            config=ScorerConfig(seed=args.seed),
        )
        outputs = write_loss_outputs(result, args.out)
        print(result.report_md)
        for name, path in outputs.items():
            print(f"  task_loss.{name}: {path}")
        return 0

    if args.command == "calibrate":
        from .calibration import calibrate_atoms

        atoms, events = _load_inputs(args)
        n_total = max(e.period for e in events) + 1
        result = calibrate_atoms(
            events,
            [a.risk_id for a in atoms],
            train_quarters=n_total - args.holdout_quarters,
            n_periods=n_total,
            seed=args.seed,
        )
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "calibration_report.json").write_text(
            json.dumps(result.report, indent=2, sort_keys=True)
        )
        (args.out / "calibration_report.md").write_text(result.report_md)
        print(result.report_md)
        print(f"  reports: {args.out}")
        return 0 if result.report["pass"] else 1

    if args.command == "r1":
        from .r1 import run_r1_eval

        result = run_r1_eval(
            args.risks,
            args.evidence_db,
            holdout_quarters=args.holdout_quarters,
            config=ScorerConfig(mc_draws=args.mc_draws, seed=args.seed),
        )
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "r1_eval.json").write_text(
            json.dumps(result.report, indent=2, sort_keys=True)
        )
        (args.out / "r1_eval.md").write_text(result.report_md)
        if result.report.get("status") == "insufficient_data":
            print(f"R1: insufficient data (documented) -> {args.out}")
            for reason in result.report["reasons"]:
                print(f"  - {reason}")
            return 0  # documented data limitation is not a CLI failure
        _print_eval_summary(result.report, {"json": args.out / "r1_eval.json", "md": args.out / "r1_eval.md"})
        return 0

    if args.command == "ablations":
        from .ablations import run_ablations, write_ablation_outputs

        atoms, events = _load_inputs(args)
        truth = json.loads(args.truth.read_text()) if args.truth else None
        result = run_ablations(
            atoms, events,
            truth=truth,
            holdout_quarters=args.holdout_quarters,
            scorer_config=ScorerConfig(mc_draws=args.mc_draws, seed=args.seed),
        )
        outputs = write_ablation_outputs(result, args.out)
        print(result.report_md)
        for name, path in outputs.items():
            print(f"  ablations.{name}: {path}")
        return 0

    if args.command == "synthetic-cell-demo":
        from .egress import EgressGateway
        from .models import IncidentEvent, RiskAtom
        from .synthetic_aggregation import write_synthetic_aggregation
        from .synthetic_events import (
            SyntheticEventConfig,
            generate_synthetic_events,
            write_synthetic_events_csv,
        )

        out = args.out
        out.mkdir(parents=True, exist_ok=True)
        records, truth = generate_synthetic_events(
            SyntheticEventConfig(n_quarters=args.quarters, seed=args.seed)
        )
        write_synthetic_events_csv(records, out / "synthetic_events.csv")
        aggregation = write_synthetic_aggregation(
            records, out, min_group_size=args.min_group_size
        )

        # Structural aggregate demo features pass; event rows have no egress schema.
        gateway = EgressGateway(out / "egress_audit.jsonl")
        n_allowed = n_denied = 0
        for line in aggregation["features"].read_text().splitlines():
            if gateway.validate(
                json.loads(line), "scoring_features", caller="synthetic-cell-demo"
            ):
                n_allowed += 1
            else:
                n_denied += 1
        local_denied = 0
        for line in aggregation["local_events"].read_text().splitlines()[:20]:
            if not gateway.validate(
                json.loads(line),
                "synthetic_scoring_events",
                caller="synthetic-cell-demo",
            ):
                local_denied += 1

        atoms = [
            RiskAtom(
                risk_id=a["risk_id"],
                title=a["risk_id"],
                channels=[a["channel"]],
                scoring_cell_code=a["scoring_cell_code"],
                source_type="synthetic",
                plane="local_detail",
            )
            for a in truth["atoms"]
        ]
        events = [
            IncidentEvent(
                event_id=row["event_id"],
                risk_id=row["risk_id"],
                period=int(row["period"]),
                loss_amount=float(row["impact_amount"]),
            )
            for row in (
                json.loads(line)
                for line in aggregation["local_events"].read_text().splitlines()
            )
        ]
        result = run_evaluation(
            atoms, events, truth=truth,
            holdout_quarters=args.holdout_quarters,
            config=ScorerConfig(mc_draws=args.mc_draws, seed=args.seed),
            regime="synthetic-cell-demo",
        )
        result.report["synthetic_aggregation"] = aggregation["aggregation_report"]
        result.report["egress_check"] = {
            "features_allowed": n_allowed,
            "features_denied": n_denied,
            "local_events_denied_sample": local_denied,
        }
        outputs = write_eval_outputs(result, out)
        report = aggregation["aggregation_report"]
        print(
            f"synthetic events: {len(records)} generated -> {report['n_accepted']} "
            f"accepted (rejected {report['n_rejected']}, suppressed groups "
            f"{report['n_suppressed_groups']})"
        )
        print(f"egress: features allowed {n_allowed} / denied {n_denied}; "
              f"local events denied {local_denied}/20 sampled (no schema)")
        _print_eval_summary(result.report, outputs)
        return 0

    if args.command == "ext-demo":
        from .ext_features import FeatureEntry, FeatureRegistry, render_screen_md, run_screen
        from .synth_ext import generate_ext_series
        from .telemetry import TelemetryLogger

        atoms, events = _load_inputs(args)
        n_periods = max(e.period for e in events) + 1
        train_quarters = n_periods - args.holdout_quarters
        out = args.out
        out.mkdir(parents=True, exist_ok=True)

        registry = FeatureRegistry(out / "ext_registry.yaml")
        for fid, spec in generate_ext_series(events, atoms, n_periods, seed=args.seed).items():
            registry.register(FeatureEntry(
                feature_id=fid, name=spec["name"], family=spec["family"],
                registered_at=spec["registered_at"], series=spec["series"],
            ))
        llm_null = None
        # P6 (HYBRID_SCORING_PROPOSAL.md): the LLM adversarial null pass is
        # attempted by default whenever an OMLX endpoint is configured.
        # Offline is fail-closed and recorded — run_screen converts client
        # errors into recorded rejections, so an unreachable endpoint means
        # nothing is accepted through this screen, never a crash and never
        # a silent skip. --no-llm-null restores the deterministic-only
        # screen (the pre-flip default).
        if not args.no_llm_null:
            from ..config import get_settings
            from ..llm import OMLXClient

            settings = get_settings()
            client = OMLXClient(settings)
            llm_null = {
                "explain_client": client, "vote_client": client,
                "explain_model": settings.llm_model,
                "vote_model": settings.llm_model_judge,
                "telemetry": TelemetryLogger(out / "telemetry.jsonl", run_id="ext-demo"),
            }
        try:
            report = run_screen(
                registry, events, [a.risk_id for a in atoms],
                train_quarters=train_quarters, n_periods=n_periods,
                min_support=args.min_support, seed=args.seed,
                llm_null=llm_null,
            )
        finally:
            if llm_null is not None:
                llm_null["explain_client"].close()
        registry.save()
        (out / "ext_report.json").write_text(json.dumps(report, indent=2, sort_keys=True))
        (out / "ext_report.md").write_text(render_screen_md(report))
        print(render_screen_md(report))
        print(f"  registry: {out / 'ext_registry.yaml'}")
        print(f"  reports:  {out / 'ext_report.json'}")
        return 0

    if args.command == "ledger":
        # P7 (HYBRID_SCORING_PROPOSAL.md): record -> resolve -> report.
        # Display-only throughout — the ledger never feeds a score.
        from ..config import get_settings
        from .judgment_ledger import (
            DEFAULT_LEDGER_PATH,
            calibration_report,
            record_analogue_claims,
            render_report_md,
            resolve_claims,
            verify_chain,
        )
        from .r1 import build_signal_series

        settings = get_settings()
        db_path = args.db or settings.scenario_db_path
        ledger = args.ledger or DEFAULT_LEDGER_PATH
        run_all = not (args.record or args.resolve or args.report)

        if args.record or run_all:
            n = record_analogue_claims(db_path, ledger)
            print(f"ledger record: {n} new claim row(s) appended -> {ledger} "
                  "(skips — already ledgered or no deterministic realization — "
                  "are logged)")

        if args.resolve or run_all:
            risks = args.risks or settings.emerging_risks_path
            evidence_db = args.evidence_db or settings.evidence_db_path
            _atoms, events, _diag = build_signal_series(risks, evidence_db)
            if not events:
                print("ledger resolve: no signal events in the R1 series — "
                      "nothing to resolve against")
            else:
                # same train/holdout split as compound_join
                # (HOLDOUT_QUARTERS=2): train end is the last train index.
                from ..compound_join import HOLDOUT_QUARTERS

                n_periods = max(e.period for e in events) + 1
                train_end = n_periods - HOLDOUT_QUARTERS - 1
                summary = resolve_claims(
                    ledger,
                    events,
                    current_period=n_periods - 1,
                    train_end_period=train_end,
                )
                print(f"ledger resolve: {summary['resolved']} resolved, "
                      f"{summary['open']} open (window not closed), "
                      f"{summary['already_resolved']} already resolved")
                for skip in summary["skipped"]:
                    print(f"  skipped {skip['entry_id']}: {skip['reason']}")

        if args.report or run_all:
            report = calibration_report(ledger)
            md = render_report_md(report)
            print(md)
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
                print(f"  {name}: {out_path}")

        ok, detail = verify_chain(ledger)
        print(f"ledger chain: {detail}")
        return 0 if ok else 1

    return 2


def _print_eval_summary(report: dict, outputs: dict, extra: dict | None = None) -> None:
    tb = report["task_b"]
    print(f"Task B: AP lift B3 vs B1 = {tb['ap_lift_b3_vs_b1_relative']} "
          f"(threshold {tb['pass_threshold']}) -> {'PASS' if tb['pass'] else 'FAIL'}")
    if report.get("task_c"):
        tc = report["task_c"]
        print(f"Task C: recovery precision {tc['precision_at_planted_count']} "
              f"(threshold {tc['precision_threshold']}) -> {'PASS' if tc['pass'] else 'FAIL'}")
    print(f"recommendation: {report['recommendation'].upper()}")
    for name, path in (extra or {}).items():
        print(f"  {name}: {path}")
    for name, path in outputs.items():
        print(f"  eval_report.{name}: {path}")


if __name__ == "__main__":
    sys.exit(score_main() if "score" in sys.argv[0] else eval_main())
