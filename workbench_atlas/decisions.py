"""Revision-aware decision records shared by local review interfaces.

SQLite decisions are written in the same transaction as their object. JSON
register decisions travel inside the atomically replaced record so neither a
crash nor a failed sidecar write can separate the decision from its history.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


def record_revision(record: dict[str, Any]) -> str:
    """Fingerprint the exact inspectable record, excluding display/history fields."""
    value = {key: value for key, value in record.items()
             if key not in {"review_history", "review_revision"}}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def ensure_decision_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS review_decisions (
        decision_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        record_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        before_revision TEXT NOT NULL,
        after_revision TEXT NOT NULL,
        attestations_json TEXT NOT NULL,
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_record "
                 "ON review_decisions(kind, record_id, decided_at)")
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS
            review_decisions_no_{operation.lower()} BEFORE {operation}
            ON review_decisions BEGIN
            SELECT RAISE(ABORT, 'review decisions are immutable'); END""")


def decision_event(*, kind: str, record_id: str, actor: str, action: str,
                   reason: str, before: dict, after: dict,
                   attestations: dict | None = None) -> dict[str, Any]:
    actor = str(actor).strip()
    if not actor or len(actor) > 160:
        raise ValueError("A reviewer name of 1–160 characters is required")
    return {
        "decision_id": uuid.uuid4().hex,
        "kind": kind, "record_id": record_id, "actor": actor,
        "action": action, "reason": str(reason).strip(),
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "before_revision": record_revision(before),
        "after_revision": record_revision(after),
        "attestations": dict(attestations or {}),
    }


def append_decision(conn: sqlite3.Connection, event: dict, before: dict,
                    after: dict) -> None:
    ensure_decision_schema(conn)
    encode = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False)
    conn.execute("INSERT INTO review_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
        event["decision_id"], event["kind"], event["record_id"], event["actor"],
        event["action"], event["reason"], event["decided_at"],
        event["before_revision"], event["after_revision"],
        encode(event["attestations"]), encode(before), encode(after),
    ))


def list_decisions(db_path: str, *, kind: str | None = None,
                   record_id: str | None = None) -> list[dict]:
    from pathlib import Path
    if not Path(db_path).is_file():
        return []
    with sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='review_decisions'").fetchone():
            return []
        clauses, params = [], []
        for key, value in (("kind", kind), ("record_id", record_id)):
            if value is not None:
                clauses.append(f"{key} = ?")
                params.append(value)
        sql = "SELECT * FROM review_decisions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = conn.execute(sql + " ORDER BY decided_at, decision_id", params)
        result = []
        for row in rows:
            item = dict(row)
            item["attestations"] = json.loads(item.pop("attestations_json"))
            item.pop("before_json")
            item.pop("after_json")
            result.append(item)
        return result
