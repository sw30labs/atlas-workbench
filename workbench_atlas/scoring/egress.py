"""Egress gateway enforcement point (docs/research/redesignv1/architecture.md section 5.2).

The only path from local processing to external or frontier compute.
The policy is a deterministic validator with a tamper-evident audit log:

1. Outbound payloads must match an allow-list schema (scoring codes,
   numeric features, structural IDs, public URLs).
2. Deny on free-text fields, local-detail narrative keys, email-like tokens,
   account-number-like digit runs, or over-long prose strings.
3. Regex redaction scan on any residual text value.
4. Hash-chained audit log of every request (body hash, decision, caller).

The red-team unit tests throw synthetic local-detail strings at the gateway
(evaluation-plan.md section 7.4).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from .models import CANONICAL_SCORING_PLANES, canonical_json

try:
    import fcntl
except ImportError:  # pragma: no cover - platform without safe file locks
    fcntl = None

# ---------------------------------------------------------------------------
# Allow-list schemas
# ---------------------------------------------------------------------------

#: schema name -> allowed top-level keys with a coarse type check.
AllowedType = type | tuple[type, ...]


ALLOWLIST_SCHEMAS: dict[str, dict[str, AllowedType]] = {
    "scenario_structure": {
        "scenario_id": str,
        "risk_ids": list,
        "scoring_cell_codes": list,
        "channels": list,
        "horizon": str,
        "p_hat": float,
        "p_interval": list,
        "loss_quantiles": dict,
        "uncertainty_class": str,
        "state_hash": str,
    },
    "scoring_codes": {
        "risk_id": str,
        "scoring_cell_code": str,
        "channels": list,
    },
    "public_refs": {
        "urls": list,
        "source_publisher": str,
    },
    "judge_prompt": {
        "risk_titles": list,
        "channels": list,
        "horizon": str,
    },
    # The public demo admits only structural synthetic scoring features.
    # Event rows and narrative have no allow-list schema.
    "scoring_features": {
        "cell_id": str,
        "scoring_cell_code": str,
        "segment_code": str,
        "period": int,
        "count": int,
        "severity_quantiles": dict,
        "n_severity": int,
        "plane": str,
        "source_system": str,
    },
    "telemetry_rollup": {
        "run_id": str,
        "spend_usd": float,
        "tokens": dict,
        "n_promoted": int,
    },
    # Wave 5 (P1/P2/P6 LLM-facing payloads, HYBRID_SCORING_PROPOSAL.md):
    # structural elicitation I/O — ids, titles, taxonomy channels, public
    # citation URLs and short claim lists. No narrative keys, no scores
    # produced or adjusted by the model (ADR-001/014).
    "channel_audit": {
        "risk_id": str,
        "title": str,
        "proposed_channels": list,
        "proposed_edges": list,
        "mechanism": str,
        "citation_url": str,
    },
    "analogue_elicitation": {
        "pair_id": str,
        "risk_ids": list,
        "horizon": str,
        "event": str,
        "period": str,
        "mechanism_map": dict,
        "co_fired": bool,
        "outcome_class": str,
        "citations": list,
        "disanalogies": list,
    },
    "refutation_memo": {
        "compound_id": str,
        "risk_ids": list,
        "claims": list,
        "citations": list,
    },
    # Request bodies actually interpolated into the three scoring-lane LLM
    # prompts.  These are intentionally separate from the response schemas
    # above: validating a hoped-for response shape does not constrain what
    # leaves the host.
    "channel_audit_request": {
        "plane": str,
        "risk_id": str,
        "title": str,
        "current_channels": list,
        "taxonomy": list,
        "other_atoms": list,
    },
    "analogue_elicitation_request": {
        "plane": str,
        "pair_id": str,
        "risk_ids": list,
        "horizon": str,
        "compound_def": str,
        "atoms": list,
        "stored_score": dict,
        "k": int,
    },
    "refutation_memo_request": {
        "plane": str,
        "compound_id": str,
        "compound_definition": str,
        "atoms": list,
        "stored_score": dict,
        "eligibility_evidence": (dict, type(None)),
    },
    "compound_join_request": {
        "plane": str,
        "k": int,
        "inventory": list,
        "visible_rule": str,
        "human_steer": str,
    },
}

#: keys that indicate free text or local-detail content, matched case-insensitively
#: on any nested key.
FORBIDDEN_KEY_PARTS = (
    "narrative",
    "description",
    "incident",
    "free_text",
    "legal",
    "person",
    "employee",
    "customer_name",
    "case_file",
    "writeup",
    "write_up",
    "_".join(("root", "cause", "story")),
    "notes",
    "comment",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ACCOUNT_RE = re.compile(r"\b\d{8,17}\b")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

#: strings longer than this many characters are treated as prose and denied
#: (structural fields — ids, codes, hashes — are all far shorter).
MAX_STRING_LEN = 240
EGRESS_ALLOWED_PLANES = CANONICAL_SCORING_PLANES - {"local_detail"}
DEFAULT_EGRESS_AUDIT_PATH = Path("generated/egress_audit.jsonl")


class EgressDeniedError(RuntimeError):
    """A remote LLM request was stopped before reaching its transport."""


@dataclass
class EgressDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class EgressGateway:
    """Allow-list + deny-scan gateway with a hash-chained audit log."""

    audit_path: Path | str | None = None

    def __post_init__(self) -> None:
        if self.audit_path is not None:
            self.audit_path = Path(self.audit_path)
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    # -- validation ---------------------------------------------------------

    def validate(self, payload: dict[str, Any], schema: str, *, caller: str = "") -> EgressDecision:
        reasons: list[str] = []
        if schema not in ALLOWLIST_SCHEMAS:
            reasons.append(f"unknown_schema:{schema}")
        else:
            allowed = ALLOWLIST_SCHEMAS[schema]
            for key in allowed.keys() - payload.keys():
                reasons.append(f"missing_required_key:{key}")
            for key, value in payload.items():
                if key not in allowed:
                    reasons.append(f"key_not_in_allowlist:{key}")
                elif not isinstance(value, allowed[key]) or (
                    allowed[key] is int and isinstance(value, bool)
                ):
                    reasons.append(f"type_mismatch:{key}")
                elif key == "plane" and value not in EGRESS_ALLOWED_PLANES:
                    reasons.append(f"plane_not_egressible:{value}")
        reasons.extend(self._scan(payload))
        decision = EgressDecision(allowed=not reasons, reasons=reasons)
        self._audit(payload, schema, decision, caller=caller)
        return decision

    def _scan(self, node: Any, path: str = "") -> list[str]:
        reasons: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                if (
                    path
                    and lowered == "plane"
                    and value not in EGRESS_ALLOWED_PLANES
                ):
                    reasons.append(f"plane_not_egressible:{value}")
                for part in FORBIDDEN_KEY_PARTS:
                    if part in lowered:
                        reasons.append(f"forbidden_key:{path}{key}")
                reasons.extend(self._scan(value, path=f"{path}{key}."))
        elif isinstance(node, (list, tuple)):
            for i, value in enumerate(node):
                reasons.extend(self._scan(value, path=f"{path}{i}."))
        elif isinstance(node, str):
            if len(node) > MAX_STRING_LEN and not _URL_RE.match(node):
                reasons.append(f"overlong_string:{path.rstrip('.')}")
            if _EMAIL_RE.search(node):
                reasons.append(f"email_like:{path.rstrip('.')}")
            if _ACCOUNT_RE.search(node):
                reasons.append(f"account_like:{path.rstrip('.')}")
        return reasons

    # -- audit ---------------------------------------------------------------

    def _audit(
        self, payload: dict[str, Any], schema: str, decision: EgressDecision, *, caller: str
    ) -> None:
        if self.audit_path is None:
            return
        if fcntl is None:
            raise RuntimeError("egress auditing requires process-safe file locking")
        body_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        # Each caller opens its own descriptor. flock then serializes both
        # threads and separate dashboard/CLI processes. Never cache the tail:
        # another gateway may have appended since this instance was created.
        with self.audit_path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                previous_hash = _verified_tail(fh)
                entry = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "caller": caller,
                    "schema": schema,
                    "body_sha256": body_hash,
                    "allowed": decision.allowed,
                    "reasons": decision.reasons,
                    "prev_hash": previous_hash,
                }
                entry["entry_hash"] = hashlib.sha256(
                    canonical_json(entry).encode("utf-8")
                ).hexdigest()
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


_PRODUCTION_GATEWAY: EgressGateway | None = None


def production_egress_gateway() -> EgressGateway:
    """Shared gateway for remote LLM preflights in the running process."""
    global _PRODUCTION_GATEWAY
    if _PRODUCTION_GATEWAY is None:
        _PRODUCTION_GATEWAY = EgressGateway(DEFAULT_EGRESS_AUDIT_PATH)
    return _PRODUCTION_GATEWAY


def payload_plane(*values: object) -> str:
    """Return the most restrictive supplied plane, preserving bad labels.

    An unknown label is returned unchanged so the gateway rejects it instead
    of quietly treating it as public.  Empty inputs are public by default.
    """
    planes = [str(value or "public") for value in values]
    for plane in planes:
        if plane not in CANONICAL_SCORING_PLANES:
            return plane
    if "local_detail" in planes:
        return "local_detail"
    if "aggregate" in planes:
        return "aggregate"
    return "public"


def _configured_endpoint(client: Any) -> str:
    endpoint = getattr(client, "_base_url", "")
    if endpoint:
        return str(endpoint)
    settings = getattr(client, "settings", None)
    try:
        return str(getattr(settings, "llm_base_url", "") or "")
    except Exception:
        # A malformed settings facade must not be mistaken for a configured
        # remote endpoint. Real OMLXClient instances always expose _base_url.
        return ""


def _is_remote_client(client: Any) -> bool:
    """Whether the client's configured transport endpoint is non-loopback."""
    endpoint = _configured_endpoint(client)
    if not endpoint:
        return False
    try:
        host = urlsplit(endpoint).hostname
    except ValueError:
        return True
    return host not in {"127.0.0.1", "localhost", "::1"}


def _emit_denial(client: Any, *, caller: str, plane: str) -> None:
    """Record a denied attempt in the client's normal telemetry sink."""
    telemetry = getattr(client, "_telemetry", None)
    emit = getattr(telemetry, "emit", None)
    if not callable(emit):
        return
    settings = getattr(client, "settings", None)
    model_id = (
        getattr(settings, "llm_model_judge", None)
        or getattr(settings, "llm_model", None)
        or "unknown"
    )
    try:
        emit(
            node=caller,
            phase="control",
            provider=(
                "dgx"
                if getattr(settings, "llm_provider", "omlx") == "dgx"
                else "mlx"
            ),
            model_id=model_id,
            plane=plane,
            status="denied_egress",
            error_class="EgressDeniedError",
        )
    except Exception:
        # Telemetry is secondary to the deny. Never route around a failed
        # telemetry sink, and never let it turn a deny into a transport call.
        return


def preflight_remote_llm(
    client: Any,
    payload: dict[str, Any],
    schema: str,
    *,
    caller: str,
    gateway: EgressGateway | None = None,
) -> EgressDecision | None:
    """Validate a prompt payload iff its configured endpoint is remote.

    ``None`` means the endpoint is loopback (or an injected test double has
    no endpoint).  A denied or broken preflight raises before ``chat_json``
    can touch the transport.
    """
    if not _is_remote_client(client):
        return None
    active_gateway = gateway or production_egress_gateway()
    try:
        decision = active_gateway.validate(payload, schema, caller=caller)
    except Exception as exc:
        _emit_denial(
            client,
            caller=caller,
            plane=str(payload.get("plane") or "public"),
        )
        raise EgressDeniedError(
            f"egress preflight failed closed for {caller}: {type(exc).__name__}"
        ) from exc
    if decision.allowed:
        return decision
    _emit_denial(
        client,
        caller=caller,
        plane=str(payload.get("plane") or "public"),
    )
    reasons = ", ".join(decision.reasons) or "policy_denied"
    raise EgressDeniedError(f"egress denied for {caller}: {reasons}")


def _verified_tail(handle: TextIO) -> str:
    """Read a complete chain while its file lock is held; reject damage."""
    prev = ""
    for line in handle:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("egress audit contains an incomplete or invalid entry") from exc
        if not isinstance(entry, dict) or not line.endswith("\n"):
            raise ValueError("egress audit contains an incomplete or invalid entry")
        if entry.get("prev_hash") != prev:
            raise ValueError("egress audit chain is broken")
        claimed = entry.pop("entry_hash", None)
        recomputed = hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()
        if recomputed != claimed:
            raise ValueError("egress audit entry hash is invalid")
        prev = claimed
    return prev


def verify_audit_chain(audit_path: str | Path) -> bool:
    """Verify a stable log view; malformed/truncated entries return False."""
    path = Path(audit_path)
    if not path.exists():
        return True
    if fcntl is None:
        return False
    try:
        with path.open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                _verified_tail(handle)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError, TypeError, UnicodeError):
        return False
    return True
