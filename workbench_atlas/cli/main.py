"""Argument parsing and command dispatch for the ``atlas`` CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from workbench_atlas import __version__
from workbench_atlas.cli import home as home_mod
from workbench_atlas.cli import ui

CONFIG_TEMPLATE_PATH = Path(__file__).with_name("config-template.yaml")


def _config_template() -> str:
    """The commented starter config written by `atlas init` / `atlas up`."""
    return CONFIG_TEMPLATE_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _settings():
    """Construct Settings *after* the home directory is activated."""
    from workbench_atlas.config import get_settings

    return get_settings()


def _read_only(db: Path) -> sqlite3.Connection | None:
    if not db.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _scalar(conn: sqlite3.Connection | None, sql: str, params: tuple = ()) -> Any:
    if conn is None:
        return None
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _rows(conn: sqlite3.Connection | None, sql: str, params: tuple = ()) -> list[tuple]:
    if conn is None:
        return []
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _store_report(home: Path) -> dict[str, Any]:
    """Counts for `atlas status`, read directly from the stores."""
    from workbench_atlas.phases.phase2_risks import load_emerging_risks

    paths = home_mod.store_paths(home)
    evidence_db = paths["evidence_db_path"]
    scenario_db = paths["scenario_db_path"]

    evidence_conn = _read_only(evidence_db)
    scenario_conn = _read_only(scenario_db)
    try:
        evidence_by_status = {
            str(status): int(count)
            for status, count in _rows(
                evidence_conn,
                "SELECT COALESCE(review_status, 'unknown'), COUNT(*) "
                "FROM evidence_events GROUP BY 1 ORDER BY 1",
            )
        }
        scenario_by_status = {
            str(status): int(count)
            for status, count in _rows(
                scenario_conn,
                "SELECT COALESCE(status, 'active'), COUNT(*) "
                "FROM scenarios GROUP BY 1 ORDER BY 1",
            )
        }
        compounds = _scalar(scenario_conn, "SELECT COUNT(*) FROM compound_scenarios")
        last_run = _rows(
            scenario_conn,
            "SELECT run_id, phase, status, completed_at FROM pipeline_runs "
            "ORDER BY id DESC LIMIT 1",
        )
        pending_signposts = _scalar(
            scenario_conn,
            "SELECT COUNT(*) FROM signpost_assessments WHERE review_status = 'pending'",
        )
        pending_merges = _scalar(
            scenario_conn,
            "SELECT COUNT(*) FROM merge_proposals WHERE status = 'pending'",
        )
    finally:
        if evidence_conn:
            evidence_conn.close()
        if scenario_conn:
            scenario_conn.close()

    risks = load_emerging_risks(paths["emerging_risks_path"]) if paths[
        "emerging_risks_path"
    ].is_file() else []
    risk_by_status: dict[str, int] = {}
    for risk in risks:
        status = str(risk.get("review_status") or "unknown")
        risk_by_status[status] = risk_by_status.get(status, 0) + 1

    return {
        "home": str(home),
        "stores": {
            "evidence_db": evidence_db,
            "scenario_db": scenario_db,
            "risks_file": paths["emerging_risks_path"],
            "scenario_export": paths["scenario_export_path"],
        },
        "evidence": {
            "total": sum(evidence_by_status.values()),
            "by_status": evidence_by_status,
        },
        "risks": {"total": len(risks), "by_status": risk_by_status},
        "scenarios": {
            "total": sum(scenario_by_status.values()),
            "by_status": scenario_by_status,
        },
        "compounds": int(compounds or 0),
        "pending_review": {
            "evidence": int(evidence_by_status.get("pending", 0)),
            "risks": int(risk_by_status.get("pending", 0)),
            "signposts": int(pending_signposts or 0),
            "merges": int(pending_merges or 0),
        },
        "last_run": (
            {
                "run_id": last_run[0][0],
                "phase": last_run[0][1],
                "status": last_run[0][2],
                "completed_at": last_run[0][3],
            }
            if last_run
            else None
        ),
        "initialized": home_mod.is_initialized(home),
    }


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace, home: Path) -> int:
    paths = home_mod.ensure_layout(home)
    ui.header("Setting up Atlas Workbench")
    ui.ok(f"data directory: {home}")

    config_path = home / "config.yaml"
    if config_path.exists() and not args.force:
        ui.info(f"keeping existing {config_path}")
    else:
        config_path.write_text(_config_template(), encoding="utf-8")
        ui.ok(f"wrote {config_path}")

    for label, path in paths.items():
        ui.info(f"{label.replace('_path', ''):<24} {path}")

    print("\nNext:")
    ui.info("atlas demo     load the fictional demo corpus")
    ui.info("atlas serve    open the dashboard")
    ui.info("atlas run      analyse the approved corpus (offline, no model needed)")
    ui.info("atlas doctor   check the installation")
    return 0


def _seed(home: Path, *, reset: bool, force: bool) -> dict[str, Any]:
    from workbench_atlas.demo import seed_into

    home_mod.ensure_layout(home)
    return seed_into(home, reset=reset, force=force)


def _print_seed(result: dict[str, Any]) -> None:
    ui.header("Demo corpus loaded")
    ui.ok(f"{result['evidence_count']} evidence records, "
          f"{result['scenario_count']} scenarios (all conspicuously fictional)")
    ui.kv(
        [
            ("data directory", result["output_dir"]),
            ("scenario store", result["scenario_database"]),
            ("evidence store", result["evidence_database"]),
            ("risk register", result["emerging_risks"]),
        ]
    )


def cmd_demo(args: argparse.Namespace, home: Path) -> int:
    result = _seed(home, reset=args.reset, force=args.force)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    _print_seed(result)
    if args.serve:
        return _serve(args.port, args.host, not args.no_browser, home)
    print("\nNext:")
    ui.info("atlas serve    open the dashboard")
    ui.info("atlas run      run the deterministic analysis")
    return 0


def _serve(port: int, host: str, open_browser: bool, home: Path) -> int:
    from workbench_atlas.web.server import main as server_main

    url = f"http://{host}:{port}"
    if not _port_free(host, port):
        ui.fail(
            f"port {port} is already in use. A dashboard may already be "
            f"running at {url} — open it, or pass --port {port + 1}."
        )
        return 1
    if open_browser:
        threading.Timer(1.2, webbrowser.open, args=[url]).start()
    ui.header("Atlas Workbench dashboard")
    ui.kv([("url", url), ("data", str(home)), ("stop", "Ctrl+C")])
    print()
    try:
        server_main(["--host", host, "--port", str(port)])
    except SystemExit as exc:  # the server rejects a bad bind or access config
        return int(exc.code or 0)
    return 0


def cmd_up(args: argparse.Namespace, home: Path) -> int:
    """First-run experience: create, seed, serve, open."""
    home_mod.ensure_layout(home)
    config_path = home / "config.yaml"
    if not config_path.exists():
        config_path.write_text(_config_template(), encoding="utf-8")

    if home_mod.is_initialized(home):
        ui.info(f"using existing data in {home}")
    else:
        ui.info("no data yet — loading the fictional demo corpus")
        result = _seed(home, reset=False, force=False)
        _print_seed(result)

    return _serve(args.port, args.host, not args.no_browser, home)


def cmd_serve(args: argparse.Namespace, home: Path) -> int:
    if not home_mod.is_initialized(home):
        ui.info("no data yet — loading the fictional demo corpus first")
        _print_seed(_seed(home, reset=False, force=False))
    return _serve(args.port, args.host, not args.no_browser, home)


def cmd_run(args: argparse.Namespace, home: Path) -> int:
    from workbench_atlas.pipeline import run_pipeline

    if not home_mod.is_initialized(home):
        ui.fail("no data yet. Run `atlas demo` first (or `atlas init` and add research).")
        return 1

    source_text = ""
    if args.file:
        source_text = Path(args.file).expanduser().read_text(encoding="utf-8")
    if args.text:
        source_text = args.text

    ui.header("Running the analysis")
    ui.kv(
        [
            ("mode", "LLM-backed" if args.with_llm else "deterministic (offline)"),
            ("scenarios", args.scenarios),
            ("data", str(home)),
        ]
    )
    result = run_pipeline(
        _settings(),
        source_text=source_text,
        risk_source_urls=args.url or None,
        num_scenarios=args.scenarios,
        skip_llm=not args.with_llm,
    )

    summary = result.summary()
    ui.header("Run complete")
    rows = []
    for phase, payload in summary.get("phases", {}).items():
        if not isinstance(payload, dict):
            rows.append([phase, "—", "—"])
            continue
        status = str(payload.get("status") or payload.get("error") or "ok")
        detail = next(
            (
                f"{key}={value}"
                for key, value in payload.items()
                if key in {"scenarios_generated", "risks_found", "events_processed",
                           "scenarios_scored", "findings", "candidates", "graduated"}
            ),
            "",
        )
        rows.append([phase, status, detail])
    if rows:
        ui.table(["phase", "status", "result"], rows)
    else:
        print(json.dumps(summary, indent=2, default=str))

    if result.errors:
        ui.header("Errors")
        for error in result.errors:
            ui.fail(error)
    if not args.with_llm:
        ui.info("offline run: no model calls, no cost. Use --with-llm to generate scenarios.")
    return 0 if not result.errors else 1


def cmd_status(args: argparse.Namespace, home: Path) -> int:
    report = _store_report(home)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    ui.header("Atlas Workbench status")
    ui.kv(
        [
            ("data directory", report["home"]),
            ("initialized", "yes" if report["initialized"] else "no — run `atlas demo`"),
            ("size on disk", ui.human_bytes(_directory_size(home)) if home.exists() else "0 B"),
        ]
    )

    ui.header("Library")
    ui.table(
        ["collection", "total", "by status"],
        [
            ["evidence", report["evidence"]["total"], _fmt(report["evidence"]["by_status"])],
            ["risks", report["risks"]["total"], _fmt(report["risks"]["by_status"])],
            ["scenarios", report["scenarios"]["total"], _fmt(report["scenarios"]["by_status"])],
            ["compounds", report["compounds"], ""],
        ],
    )

    pending = report["pending_review"]
    total_pending = sum(pending.values())
    ui.header("Review queue")
    if total_pending:
        ui.kv([(kind, count) for kind, count in pending.items() if count])
        ui.info("review in the dashboard, or: atlas review list")
    else:
        ui.ok("nothing waiting for review")

    last = report["last_run"]
    ui.header("Last run")
    if last:
        ui.kv(
            [
                ("run", last["run_id"]),
                ("phase", last["phase"]),
                ("status", last["status"]),
                ("finished", last["completed_at"] or "—"),
            ]
        )
    else:
        ui.info("no pipeline run recorded yet")
    return 0


def _fmt(counts: dict[str, int]) -> str:
    if not counts:
        return "—"
    return ", ".join(f"{key} {value}" for key, value in sorted(counts.items()))


def cmd_doctor(args: argparse.Namespace, home: Path) -> int:
    """Diagnose the installation. Exit 1 only on FAIL, never on WARN."""
    checks: list[dict[str, str]] = []

    def record(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    version = sys.version_info
    if version >= (3, 12):
        record("python", "OK", f"{version.major}.{version.minor}.{version.micro}")
    else:
        record("python", "FAIL", f"{version.major}.{version.minor} — requires 3.12+")

    try:
        home_mod.ensure_layout(home)
        probe = home / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("data directory", "OK", f"{home} (writable)")
    except OSError as exc:
        record("data directory", "FAIL", f"{home}: {exc}")

    try:
        from workbench_atlas.guard_hook import ONTOLOGY_DIR

        missing = [
            name
            for name in ("guard.py", "domain.ttl", "shapes.ttl", "category-map.json")
            if not (ONTOLOGY_DIR / name).is_file()
        ]
        if missing:
            record("ontology guard", "FAIL", f"missing {', '.join(missing)} in {ONTOLOGY_DIR}")
        else:
            record("ontology guard", "OK", str(ONTOLOGY_DIR))
    except Exception as exc:  # pragma: no cover - defensive
        record("ontology guard", "FAIL", str(exc))

    try:
        import rdflib  # noqa: F401
        import pyshacl  # noqa: F401

        record("guard dependencies", "OK", "rdflib + pyshacl importable")
    except ImportError as exc:
        record(
            "guard dependencies",
            "WARN",
            f"{exc.name} missing — `pip install -e '.[ontology]'` "
            "or set ATLAS_GUARD=off for a demo-only session",
        )

    paths = home_mod.store_paths(home)
    for label, key in (("scenario store", "scenario_db_path"),
                       ("evidence store", "evidence_db_path")):
        path = paths[key]
        if not path.is_file():
            record(label, "WARN", f"{path} not created yet (run `atlas demo`)")
            continue
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                result = conn.execute("PRAGMA integrity_check").fetchone()
            if result and result[0] == "ok":
                record(label, "OK", f"{path} (integrity ok)")
            else:
                record(label, "FAIL", f"{path}: integrity check {result}")
        except sqlite3.Error as exc:
            record(label, "FAIL", f"{path}: {exc}")

    try:
        from workbench_atlas.demo import fixtures_dir

        record("demo fixtures", "OK", str(fixtures_dir()))
    except Exception as exc:
        record("demo fixtures", "FAIL", str(exc))

    if args.no_network:
        record("model endpoint", "SKIP", "not probed (--no-network)")
    else:
        settings = _settings()
        url = settings.llm_base_url.rstrip("/") + "/models"
        try:
            import httpx

            with httpx.Client(timeout=2.0) as client:
                response = client.get(
                    url, headers={"Authorization": f"Bearer {settings.llm_api_key}"}
                )
            if response.status_code < 400:
                record("model endpoint", "OK", f"{settings.llm_base_url} reachable")
            else:
                record(
                    "model endpoint",
                    "WARN",
                    f"{url} returned HTTP {response.status_code} — "
                    "offline analysis still works",
                )
        except Exception:
            record(
                "model endpoint",
                "WARN",
                f"{settings.llm_base_url} not reachable — offline analysis still works",
            )

    if _port_free("127.0.0.1", args.port):
        record("dashboard port", "OK", f"{args.port} free")
    else:
        record("dashboard port", "WARN", f"{args.port} in use (dashboard may be running)")

    if args.json:
        print(json.dumps(checks, indent=2))
    else:
        ui.header("Installation check")
        for check in checks:
            line = f"{check['check']}: {check['detail']}"
            {"OK": ui.ok, "WARN": ui.warn, "FAIL": ui.fail}.get(
                check["status"], ui.info
            )(line)
        failures = [c for c in checks if c["status"] == "FAIL"]
        print()
        if failures:
            ui.fail(f"{len(failures)} problem(s) need attention")
        else:
            ui.ok("ready — run `atlas demo` to load the demo corpus")

    return 1 if any(c["status"] == "FAIL" for c in checks) else 0


def cmd_config(args: argparse.Namespace, home: Path) -> int:
    settings = _settings()
    from workbench_atlas.config import yaml_config_path

    payload = settings.to_safe_dict()
    payload["config_file"] = str(yaml_config_path())
    payload["home"] = str(home)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0
    ui.header("Effective configuration")
    ui.kv(
        [
            ("config file", payload["config_file"]),
            ("data directory", payload["home"]),
            ("provider", payload["llm_provider"]),
            ("endpoint", settings.llm_base_url),
            ("model", settings.llm_model),
            ("judge model", settings.llm_model_judge),
            ("scenario store", payload["scenario_db_path"]),
            ("evidence store", payload["evidence_db_path"]),
        ]
    )
    ui.info("API keys are read from OMLX_API_KEY / DGX_API_KEY and never printed")
    return 0


def cmd_clean(args: argparse.Namespace, home: Path) -> int:
    if home == Path("/") or home == Path.home():
        ui.fail(f"refusing to clean {home}")
        return 1
    targets = [
        path
        for path in home_mod.store_paths(home).values()
        if path.is_file()
    ] + [home / name for name in ("exports", "logs", "telemetry")]
    if not targets:
        ui.info(f"nothing to remove in {home}")
        return 0
    if not args.yes:
        ui.warn("this deletes the local stores (research data is not recoverable):")
        for path in targets:
            ui.info(str(path))
        ui.info("re-run with --yes to confirm")
        return 1
    for path in targets:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
            path.with_name(path.name + "-wal").unlink(missing_ok=True)
            path.with_name(path.name + "-shm").unlink(missing_ok=True)
    ui.ok(f"removed local data from {home}")
    return 0


def cmd_open(args: argparse.Namespace, home: Path) -> int:
    url = f"http://{args.host}:{args.port}"
    ui.info(f"opening {url}")
    webbrowser.open(url)
    return 0


def cmd_review(args: argparse.Namespace, home: Path) -> int:
    from workbench_atlas.review_cli import main as review_main

    if args.review_help or not args.review_args:
        return review_main(["--help"])
    return review_main(args.review_args)


def cmd_export(args: argparse.Namespace, home: Path) -> int:
    from workbench_atlas.exports import main as export_main

    if args.export_help or not args.export_args:
        return export_main(["--help"])
    return export_main(args.export_args)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--home",
        metavar="DIR",
        help="data directory (default: $ATLAS_HOME or ~/.atlas)",
    )
    common.add_argument("--json", action="store_true", help="machine-readable output")

    parser = argparse.ArgumentParser(
        prog="atlas",
        description="Operational-risk scenario workbench — one command to a working dashboard.",
        epilog=(
            "Start here:  atlas up        (create, load demo, open dashboard)\n"
            "Diagnose:    atlas doctor\n"
            "Own data:    atlas init && atlas run && atlas serve\n"
            "Docs:        README.md and docs/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"atlas {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    init = commands.add_parser("init", parents=[common], help="create the data directory and config")
    init.add_argument("--force", action="store_true", help="overwrite an existing config.yaml")
    init.set_defaults(func=cmd_init)

    demo = commands.add_parser(
        "demo", parents=[common],
        help="load the fictional demo corpus (never real data)",
    )
    demo.add_argument("--reset", action="store_true", help="replace existing demo databases")
    demo.add_argument("--force", action="store_true", help="allow seeding into the checkout root")
    demo.add_argument("--serve", action="store_true", help="start the dashboard afterwards")
    demo.add_argument("--port", type=int, default=8080)
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--no-browser", action="store_true", help="do not open a browser")
    demo.set_defaults(func=cmd_demo)

    up = commands.add_parser(
        "up", parents=[common],
        help="first run: create, seed the demo, and open the dashboard",
    )
    up.add_argument("--port", type=int, default=8080)
    up.add_argument("--host", default="127.0.0.1")
    up.add_argument("--no-browser", action="store_true")
    up.set_defaults(func=cmd_up)

    serve = commands.add_parser("serve", parents=[common], help="start the dashboard")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(func=cmd_serve)

    run = commands.add_parser("run", parents=[common], help="run the analysis pipeline")
    run.add_argument("--with-llm", action="store_true",
                     help="enable model-backed generation (default: deterministic offline)")
    run.add_argument("--scenarios", type=int, default=3, help="scenarios to generate (default 3)")
    run.add_argument("--text", help="research text for this run")
    run.add_argument("--file", help="read research text from a file")
    run.add_argument("--url", action="append", help="public source URL (repeatable)")
    run.set_defaults(func=cmd_run)

    status = commands.add_parser("status", parents=[common], help="show library and review status")
    status.set_defaults(func=cmd_status)

    doctor = commands.add_parser("doctor", parents=[common], help="check the installation")
    doctor.add_argument("--port", type=int, default=8080)
    doctor.add_argument("--no-network", action="store_true", help="skip the model endpoint probe")
    doctor.set_defaults(func=cmd_doctor)

    config = commands.add_parser("config", parents=[common], help="show effective configuration")
    config.set_defaults(func=cmd_config)

    clean = commands.add_parser("clean", parents=[common], help="delete local stores in the data directory")
    clean.add_argument("--yes", action="store_true", help="confirm deletion")
    clean.set_defaults(func=cmd_clean)

    open_cmd = commands.add_parser("open", parents=[common], help="open the dashboard in a browser")
    open_cmd.add_argument("--port", type=int, default=8080)
    open_cmd.add_argument("--host", default="127.0.0.1")
    open_cmd.set_defaults(func=cmd_open)

    review = commands.add_parser(
        "review", parents=[common], add_help=False,
        help="list and decide review items (same audited path as the dashboard)",
    )
    review.add_argument("-h", "--help", action="store_true", dest="review_help",
                        help="show the review commands")
    review.add_argument("review_args", nargs=argparse.REMAINDER,
                        help="see `atlas review --help`")
    review.set_defaults(func=cmd_review)

    export = commands.add_parser(
        "export", parents=[common], add_help=False,
        help="export a governed snapshot",
    )
    export.add_argument("-h", "--help", action="store_true", dest="export_help",
                        help="show the export options")
    export.add_argument("export_args", nargs=argparse.REMAINDER,
                        help="see `atlas export --help`")
    export.set_defaults(func=cmd_export)

    return parser


def _delegation(argv: list[str]) -> tuple[str, list[str], str | None] | None:
    """Split `atlas [globals] review|export <args>` for pass-through commands.

    Those two commands have their own mature parsers.  Routing them through
    the top-level argparse would either duplicate every flag or reject the
    flags it does not know, so they are handed the remainder verbatim.
    """
    home: str | None = None
    index = 0
    command: str | None = None
    while index < len(argv):
        token = argv[index]
        if token in ("-h", "--help", "--version"):
            return None
        if token == "--home" and index + 1 < len(argv):
            home = argv[index + 1]
            index += 2
            continue
        if token.startswith("--home="):
            home = token.split("=", 1)[1]
            index += 1
            continue
        if token == "--json":
            index += 1
            continue
        command = token
        index += 1
        break
    if command not in {"review", "export"}:
        return None

    rest: list[str] = []
    while index < len(argv):
        token = argv[index]
        if token == "--home" and index + 1 < len(argv):
            home = argv[index + 1]
            index += 2
            continue
        if token.startswith("--home="):
            home = token.split("=", 1)[1]
            index += 1
            continue
        if token == "--json":
            index += 1
            continue
        rest.append(token)
        index += 1
    return command, rest, home


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    delegated = _delegation(raw)
    if delegated:
        command, rest, delegated_home = delegated
        home_mod.activate(delegated_home)
        if command == "review":
            from workbench_atlas.review_cli import main as delegate
        else:
            from workbench_atlas.exports import main as delegate
        return int(delegate(rest or ["--help"]) or 0)

    parser = build_parser()
    args = parser.parse_args(raw)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    home = home_mod.activate(getattr(args, "home", None))

    try:
        return int(args.func(args, home) or 0)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        ui.fail(str(exc))
        if os.environ.get("ATLAS_DEBUG"):
            raise
        ui.info("run `atlas doctor` for diagnostics, or set ATLAS_DEBUG=1 for a traceback")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
