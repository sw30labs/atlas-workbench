"""Local operational journal, verified backups and non-destructive recovery.

Run ``python -m contingency_atlas.operations --help`` for the operator CLI.
Backups contain governed local data and belong in operator-controlled storage,
never in the public code repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from .config import Settings, get_settings

try:
    import fcntl
except ImportError:  # pragma: no cover - fail closed on unsupported platforms
    fcntl = None

MAX_QUEUED_JOBS = 20
MANIFEST_VERSION = 1
BACKUP_FILES = {
    "atlas.db": "sqlite",
    "event_evidence.db": "sqlite",
    "organization-context.db": "sqlite",
    "integration-outbox.db": "sqlite",
    "emerging_risks.json": "json",
    "configuration.json": "json",
    "generated/operations/jobs.sqlite3": "sqlite",
    "generated/judgment/ledger.jsonl": "jsonl",
    "generated/egress_audit.jsonl": "jsonl",
}
REQUIRED_BACKUP_FILES = {
    "atlas.db", "event_evidence.db", "emerging_risks.json", "configuration.json",
}


class OperationsError(ValueError):
    """A safe-to-report operational refusal or integrity failure."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor(actor: str) -> str:
    if not isinstance(actor, str):
        raise OperationsError("An operator name of 1–160 characters is required")
    actor = actor.strip()
    if not actor or len(actor) > 160 or any(ord(c) < 32 for c in actor):
        raise OperationsError("An operator name of 1–160 characters is required")
    return actor


def operations_directory(settings: Settings) -> Path:
    return Path(settings.scenario_db_path).parent / "generated" / "operations"


def job_journal_path(settings: Settings) -> Path:
    return operations_directory(settings) / "jobs.sqlite3"


@contextmanager
def exclusive_file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Lock one stable sidecar across threads and processes; never degrade."""
    if fcntl is None:
        raise OperationsError("Process-safe file locking is unavailable")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise OperationsError("An operation is already running against these stores") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def worker_lock_path(settings: Settings) -> Path:
    return operations_directory(settings) / "worker.lock"


def safe_configuration(settings: Settings) -> dict[str, Any]:
    """Snapshot named settings without copying an environment or raw YAML."""
    result = dict(settings.to_safe_dict())
    result.update(context_enabled=settings.context_enabled, context_db_path=str(settings.context_db_path))
    for key in list(result):
        if any(word in key.lower() for word in ("api_key", "password", "secret", "credential", "token")):
            # Numeric token budgets are configuration, never credentials.
            if not key.endswith("max_tokens"):
                result.pop(key)
                continue
        value = result[key]
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            hostname = parsed.hostname or ""
            if ":" in hostname:
                hostname = f"[{hostname}]"
            if parsed.port is not None:
                hostname += f":{parsed.port}"
            result[key] = urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
    return result


def settings_fingerprint(settings: Settings) -> str:
    return hashlib.sha256(json.dumps(safe_configuration(settings), sort_keys=True).encode()).hexdigest()


class JobJournal:
    """Durable queue state; job parameters are private and omitted from reads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS operational_jobs (
                    id TEXT PRIMARY KEY, phase TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'queued','running','completed','failed','interrupted','cancelled')),
                    actor TEXT NOT NULL, params_json TEXT NOT NULL,
                    settings_hash TEXT NOT NULL, retry_of TEXT,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    result_json TEXT, error TEXT, owner_pid INTEGER
                );
                CREATE INDEX IF NOT EXISTS operational_jobs_queue
                    ON operational_jobs(state, created_at);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _public(row, *, include_params: bool = False) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        params = item.pop("params_json")
        item["result"] = json.loads(item.pop("result_json") or "null")
        item["status"] = (
            "running" if item["state"] in {"queued", "running"}
            else "done" if item["state"] == "completed" else "error"
        )
        item.pop("settings_hash", None)
        item.pop("owner_pid", None)
        if include_params:
            item["params"] = json.loads(params)
        else:
            item["parameter_names"] = sorted(json.loads(params))
        return item

    def enqueue(self, phase: str, params: dict, *, actor: str, settings_hash: str,
                retry_of: str | None = None, max_queue: int = MAX_QUEUED_JOBS) -> str:
        actor = _actor(actor)
        encoded = json.dumps(params, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > 600_000:
            raise OperationsError("Job parameters exceed the local journal size limit")
        job_id = f"web-{uuid.uuid4().hex}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) FROM operational_jobs WHERE state IN ('queued','running')"
            ).fetchone()[0]
            if count >= max_queue:
                raise OperationsError(f"Job queue is full ({max_queue} outstanding jobs)")
            connection.execute(
                "INSERT INTO operational_jobs (id,phase,state,actor,params_json,"
                "settings_hash,retry_of,created_at) VALUES (?,?,'queued',?,?,?,?,?)",
                (job_id, phase, actor, encoded, settings_hash, retry_of, now_iso()),
            )
            connection.commit()
        return job_id

    def get(self, job_id: str, *, include_params: bool = False) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM operational_jobs WHERE id=?", (job_id,)).fetchone()
        return self._public(row, include_params=include_params)

    def list(self, limit: int = 100) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operational_jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [self._public(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            return dict(connection.execute("SELECT state,COUNT(*) FROM operational_jobs GROUP BY state"))

    def interrupt_running(self) -> int:
        """Caller must hold the worker lock, proving no worker is still active."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE operational_jobs SET state='interrupted',finished_at=?,error=? "
                "WHERE state='running'",
                (now_iso(), "Previous worker stopped before reporting completion. "
                 "Some changes may already be saved; inspect before explicitly retrying."),
            )
            connection.commit()
            return cursor.rowcount

    def claim(self) -> dict | None:
        """Caller holds the worker lock for the complete phase execution."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operational_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE operational_jobs SET state='running',started_at=?,owner_pid=? WHERE id=?",
                (now_iso(), os.getpid(), row["id"]),
            )
            connection.commit()
        item = dict(row)
        item["params"] = json.loads(item.pop("params_json"))
        return item

    def finish(self, job_id: str, state: str, *, result: dict | None = None,
               error: str | None = None) -> None:
        if state not in {"completed", "failed", "interrupted"}:
            raise OperationsError("Invalid terminal job state")
        encoded = json.dumps(result, default=str, allow_nan=False)
        with self.connect() as connection:
            connection.execute(
                "UPDATE operational_jobs SET state=?,finished_at=?,result_json=?,error=? "
                "WHERE id=? AND state='running'",
                (state, now_iso(), encoded, error, job_id),
            )
            connection.commit()

    def note_queued(self, job_id: str, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE operational_jobs SET error=? WHERE id=? AND state='queued'", (message, job_id),
            )
            connection.commit()

    def cancel(self, job_id: str, *, actor: str) -> dict:
        actor = _actor(actor)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM operational_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise OperationsError("Job was not found")
            if row["state"] != "queued":
                raise OperationsError("Only queued jobs can be cancelled. Running phases cannot "
                                      "be safely preempted or rolled back.")
            connection.execute(
                "UPDATE operational_jobs SET state='cancelled',finished_at=?,error=? WHERE id=?",
                (now_iso(), f"Cancelled before execution by {actor}", job_id),
            )
            connection.commit()
        return self.get(job_id)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _publish_bundle(staging: Path, destination: Path) -> None:
    """Reserve a new destination and publish its manifest only after its data."""
    destination.mkdir(mode=0o700)
    identity = destination.stat().st_ino
    try:
        for source in sorted(staging.iterdir(), key=lambda path: path.name == "manifest.json"):
            shutil.move(str(source), str(destination / source.name))
        staging.rmdir()
        for directory in (destination, destination.parent):
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException:
        # Remove only the newly reserved directory, never an operator's
        # pre-existing destination or a replacement installed by another actor.
        if destination.is_dir() and not destination.is_symlink() and destination.stat().st_ino == identity:
            shutil.rmtree(destination)
        raise


def _sqlite_snapshot(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    deadline = time.monotonic() + 30
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise OperationsError("SQLite snapshot exceeded its deadline; retry while idle")
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(target)) as writer:
            reader.backup(writer, pages=128, progress=progress, sleep=0.05)
            if writer.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise OperationsError(f"SQLite integrity check failed for {source.name}")


def create_backup(destination: str | Path, *, actor: str, settings: Settings | None = None,
                  artifacts_root: str | Path | None = None) -> dict:
    """Create a verified bundle, refusing an existing destination directory."""
    settings = settings or get_settings()
    actor = _actor(actor)
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise OperationsError("Backup destination must be a new directory")
    required = {
        "atlas.db": Path(settings.scenario_db_path),
        "event_evidence.db": Path(settings.evidence_db_path),
        "emerging_risks.json": Path(settings.emerging_risks_path),
    }
    # Preserve an existing private workspace even if its feature is now off.
    # Merely taking a backup must not activate that workspace.
    context_path = Path(settings.context_db_path)
    if settings.context_enabled or context_path.exists():
        required["organization-context.db"] = context_path
    outbox_path = context_path.with_name("integration-outbox.db")
    if outbox_path.exists():
        required["integration-outbox.db"] = outbox_path
    for name, path in required.items():
        if not path.is_file() or path.is_symlink():
            raise OperationsError(f"Required configured store is missing or a symlink: {name}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".atlas-backup-", dir=destination.parent))
    sources = dict(required)
    if job_journal_path(settings).is_file():
        sources["generated/operations/jobs.sqlite3"] = job_journal_path(settings)
    artifact_dir = Path(artifacts_root) if artifacts_root else Path(settings.scenario_db_path).parent / "generated"
    for relative in ("judgment/ledger.jsonl", "egress_audit.jsonl"):
        if (artifact_dir / relative).is_file():
            sources[f"generated/{relative}"] = artifact_dir / relative
    try:
        with ExitStack() as stack:
            stack.enter_context(exclusive_file_lock(worker_lock_path(settings), blocking=False))
            stack.enter_context(exclusive_file_lock(Path(str(settings.emerging_risks_path) + ".lock")))
            # Hold write reservations simultaneously, then use separate read
            # connections for SQLite's online backup (including committed WAL).
            databases = sorted({path.resolve() for name, path in sources.items() if BACKUP_FILES[name] == "sqlite"})
            for source in databases:
                connection = sqlite3.connect(f"{source.as_uri()}?mode=rw", uri=True, timeout=5)
                stack.callback(connection.close)
                connection.execute("BEGIN IMMEDIATE")
            for name, source in sources.items():
                if source.is_symlink():
                    raise OperationsError(f"Backup source must not be a symlink: {name}")
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if BACKUP_FILES[name] == "sqlite":
                    _sqlite_snapshot(source, target)
                else:
                    before = source.stat()
                    shutil.copyfile(source, target)
                    target.chmod(0o600)
                    with target.open("rb") as handle:
                        os.fsync(handle.fileno())
                    after = source.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise OperationsError(f"Source changed during backup: {name}; retry while idle")
            _write_json(staging / "configuration.json", safe_configuration(settings))
        files = [{"path": name, "kind": BACKUP_FILES[name], "size": (staging / name).stat().st_size,
                  "sha256": _sha256(staging / name)} for name in sorted([*sources, "configuration.json"])]
        manifest = {"schema_version": MANIFEST_VERSION, "created_at": now_iso(), "actor": actor,
                    "files": files, "credential_configuration_included": False,
                    "consistency": "Primary stores reserved against writes; SQLite online snapshots include committed WAL"}
        _write_json(staging / "manifest.json", manifest)
        verify_backup(staging)
        _publish_bundle(staging, destination)
        return {"path": str(destination), **manifest}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_backup(bundle: str | Path) -> dict:
    """Verify a bounded manifest and every copied byte without executing data."""
    bundle = Path(bundle).expanduser().resolve()
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink() or manifest_path.stat().st_size > 1_000_000:
        raise OperationsError("Backup manifest is missing, oversized or a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperationsError("Backup manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_VERSION:
        raise OperationsError("Unsupported backup manifest version")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not 4 <= len(entries) <= len(BACKUP_FILES):
        raise OperationsError("Backup manifest has an invalid file list")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise OperationsError("Invalid backup file entry")
        name = entry.get("path")
        if not isinstance(name, str) or name not in BACKUP_FILES or name in seen or entry.get("kind") != BACKUP_FILES[name]:
            raise OperationsError("Backup manifest contains an unexpected or duplicate path")
        seen.add(name)
        path = bundle / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(bundle):
            raise OperationsError(f"Backup file is missing or unsafe: {name}")
        if path.stat().st_size != entry.get("size") or _sha256(path) != entry.get("sha256"):
            raise OperationsError(f"Backup checksum mismatch: {name}")
        if BACKUP_FILES[name] == "sqlite":
            try:
                with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
                    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise OperationsError(f"SQLite integrity check failed: {name}")
            except sqlite3.Error as exc:
                raise OperationsError(f"SQLite backup is unreadable: {name}") from exc
        elif BACKUP_FILES[name] == "json":
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise OperationsError(f"JSON backup is unreadable: {name}") from exc
            if name == "emerging_risks.json" and (
                not isinstance(value, list) or not all(isinstance(record, dict) for record in value)
            ):
                raise OperationsError("Risk backup must contain a list of records")
            if name == "configuration.json" and not isinstance(value, dict):
                raise OperationsError("Configuration backup must contain an object")
    if not REQUIRED_BACKUP_FILES <= seen:
        raise OperationsError("Backup omits a required store")
    return {"verified": True, "path": str(bundle), "manifest": manifest, "files": len(entries)}


def restore_backup(bundle: str | Path, destination: str | Path, *, actor: str,
                   confirmed: bool = False) -> dict:
    """Restore only into a new directory; never replace live stores."""
    actor = _actor(actor)
    if confirmed is not True:
        raise OperationsError("Restore requires explicit operator confirmation")
    verified = verify_backup(bundle)
    bundle = Path(verified["path"])
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise OperationsError("Restore destination must be a new directory; live stores cannot be overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".atlas-restore-", dir=destination.parent))
    try:
        for entry in verified["manifest"]["files"]:
            source, target = bundle / entry["path"], staging / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            target.chmod(0o600)
            if _sha256(target) != entry["sha256"]:
                raise OperationsError("Backup changed while restoring; restored data was discarded")
        _write_json(staging / "manifest.json", verified["manifest"])
        verify_backup(staging)
        receipt = {"restored_at": now_iso(), "actor": actor, "source": str(bundle),
                   "destination": str(destination), "live_stores_modified": False}
        _write_json(staging / "restore-receipt.json", receipt)
        _publish_bundle(staging, destination)
        environment = {
            "ATLAS_DB": str(destination / "atlas.db"),
            "ATLAS_EVIDENCE_DB": str(destination / "event_evidence.db"),
            "ATLAS_EMERGING_RISKS": str(destination / "emerging_risks.json"),
        }
        if any((destination / name).is_file() for name in (
            "organization-context.db", "integration-outbox.db",
        )):
            environment["ATLAS_CONTEXT_DB"] = str(destination / "organization-context.db")
        return {**receipt, "environment": environment}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def readiness(settings: Settings | None = None) -> dict:
    """Read-only local deployment checks; no network probing or SSO claim."""
    settings = settings or get_settings()
    from .web.health import data_health

    health = data_health(settings)
    journal = job_journal_path(settings)
    checks = [{"name": "data_stores", "status": health["status"], "detail": health["stores"]},
              {"name": "process_locks", "status": "ok" if fcntl is not None else "unavailable"}]
    from .web.access import AccessError, load_access_config
    try:
        access = load_access_config()
        checks.append({"name": "deployment_boundary", "status": access.mode,
                       "detail": "Signed single-tenant identity proxy configured; external login must be validated by the operator"
                       if access.mode == "proxy" else "Loopback local operator mode"})
    except AccessError as exc:
        checks.append({"name": "deployment_boundary", "status": "unavailable", "detail": str(exc)})
    for name, path, required in (
        ("organization_context", Path(settings.context_db_path), settings.context_enabled),
        ("integration_outbox", Path(settings.context_db_path).with_name("integration-outbox.db"), False),
    ):
        status = "uninitialized"
        if path.exists():
            try:
                with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
                    status = "ok" if connection.execute("PRAGMA quick_check").fetchone()[0] == "ok" else "unavailable"
            except sqlite3.Error:
                status = "unavailable"
        elif required:
            status = "unavailable"
        checks.append({"name": name, "status": status})
    counts = {}
    if journal.exists():
        try:
            with closing(sqlite3.connect(f"{journal.resolve().as_uri()}?mode=ro", uri=True)) as connection:
                counts = dict(connection.execute("SELECT state,COUNT(*) FROM operational_jobs GROUP BY state"))
            checks.append({"name": "job_journal", "status": "attention" if counts.get("interrupted") else "ok"})
        except sqlite3.Error:
            checks.append({"name": "job_journal", "status": "unavailable"})
    else:
        checks.append({"name": "job_journal", "status": "uninitialized"})
    checks.append({"name": "backup_recovery", "status": "operator_managed",
                   "detail": "Use backup, verify and restore commands; retain bundles on independent private storage"})
    return {"ready_for_local_use": all(store["status"] == "ok" for store in health["stores"])
            and fcntl is not None and not any(check["status"] == "unavailable" for check in checks),
            "checks": checks, "job_counts": counts, "journal_path": str(journal)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contingency Atlas local operations")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Create a verified private backup in a new directory")
    backup.add_argument("destination")
    backup.add_argument("--actor", required=True)
    backup.add_argument("--artifacts-root")
    verify = commands.add_parser("verify", help="Verify every file in a backup")
    verify.add_argument("bundle")
    restore = commands.add_parser("restore", help="Restore into a new directory without touching live stores")
    restore.add_argument("bundle")
    restore.add_argument("destination")
    restore.add_argument("--actor", required=True)
    restore.add_argument("--confirm-restore", action="store_true")
    commands.add_parser("readiness", help="Inspect local deployment readiness without network calls")
    jobs = commands.add_parser("jobs", help="List durable jobs without exposing their input text")
    jobs.add_argument("--limit", type=int, default=100)
    for command in ("cancel-job", "retry-job"):
        job = commands.add_parser(command)
        job.add_argument("job_id")
        job.add_argument("--actor", required=True)
    recover = commands.add_parser("recover-jobs", help="Reconcile abandoned runs and resume queued work")
    recover.add_argument("--no-resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(args.destination, actor=args.actor, artifacts_root=args.artifacts_root)
        elif args.command == "verify":
            result = verify_backup(args.bundle)
        elif args.command == "restore":
            result = restore_backup(args.bundle, args.destination, actor=args.actor, confirmed=args.confirm_restore)
        elif args.command == "readiness":
            result = readiness()
        else:
            from .web import runner
            if args.command == "jobs":
                result = runner.list_jobs(args.limit)
            elif args.command == "cancel-job":
                result = runner.cancel_job(args.job_id, actor=args.actor)
            elif args.command == "retry-job":
                job_id, error = runner.retry_job(args.job_id, actor=args.actor)
                if error:
                    raise OperationsError(error)
                result = {"job_id": job_id}
            else:
                result = runner.recover_jobs(resume_queued=not args.no_resume)
            # CLI workers must finish before this short-lived process exits;
            # the dashboard's daemon dispatcher instead follows server life.
            if args.command in {"retry-job", "recover-jobs"}:
                runner.wait_for_jobs()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Operation refused: {exc}\n")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
