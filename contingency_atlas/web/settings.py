"""Operator Settings surface for the local Command Deck.

This module is display-and-control for already-local operator actions. It
never downloads framework data, never talks to MITRE, and never live-mutates
the running Settings object. Model writes go to the YAML file and take effect
on the next dashboard start.
"""

from __future__ import annotations

import base64
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from contingency_atlas.config import (
    Settings,
    load_yaml_settings,
    normalize_llm_provider,
    project_root,
    yaml_config_path,
)
from contingency_atlas.frameworks import (
    DEFAULT_MAX_BYTES,
    FrameworkImportError,
    ImportSizeError,
    ReleaseCollisionError,
    activate_release,
    framework_status,
    import_mitre_atlas,
)
from contingency_atlas.web.display import safe_endpoint_message, safe_endpoint_url

# Operator-facing pin for the official release documented with this overlay.
# The application records these fields; it never fetches download_url.
PINNED_ATLAS_RELEASE: dict[str, str] = {
    "content_version": "2026.07",
    "format_version": "6.0.0",
    "source_url": "https://github.com/mitre-atlas/atlas-data/releases/tag/v2026.07",
    "download_url": (
        "https://raw.githubusercontent.com/mitre-atlas/atlas-data/"
        "v2026.07/dist/v6/ATLAS-2026.07.yaml"
    ),
    "expected_sha256": (
        "0e07bb07fc6423d72cdf24ddc2038a6905bcbc00ba571064153119ee1a5888d4"
    ),
}

PLANNED_FRAMEWORKS: tuple[dict[str, str], ...] = (
    {
        "id": "mitre-attack",
        "name": "MITRE ATT&CK",
        "status": "planned",
        "note": (
            "Broader framework crosswalks stay planned until the MITRE ATLAS "
            "overlay is in actual use and remain maintainable."
        ),
    },
    {
        "id": "nist-ai-rmf",
        "name": "NIST AI RMF",
        "status": "planned",
        "note": (
            "A NIST AI RMF crosswalk is the same gated workstream as ATT&CK. "
            "It is not an ingest target yet."
        ),
    },
)

PLANNED_FEEDS: tuple[dict[str, str], ...] = (
    {
        "id": "automatic-refresh",
        "name": "Automatic source and framework refresh",
        "status": "planned",
        "note": (
            "Scheduled fetch needs a threat model, pin policy, rollback, and "
            "an explicit operator approval step before it can be built."
        ),
    },
)

_MODEL_FIELDS = ("model", "model_judge", "model_final_judge")
_KEY_LINE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>provider|model|model_judge|model_final_judge|"
    r"use_responses_api)"
    r"(?P<sep>\s*:\s*)(?P<rest>.*)$"
)
_OMLX_ENV = {
    "model": ("OMLX_MODEL",),
    "model_judge": ("OMLX_MODEL_JUDGE",),
    "model_final_judge": ("OMLX_MODEL_FINAL_JUDGE",),
    "use_responses_api": ("OMLX_USE_RESPONSES_API",),
}
_DGX_ENV = {
    "model": ("DGX_MODEL", "VLLM_MODEL"),
    "model_judge": ("DGX_MODEL_JUDGE",),
    "model_final_judge": ("DGX_MODEL_FINAL_JUDGE",),
}


class SettingsActionError(ValueError):
    """Operator-facing failure from a Settings mutation."""


def _iso_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).replace(microsecond=0).isoformat()
    except OSError:
        return None


def _file_info(path: Path) -> dict[str, Any]:
    exists = path.is_file()
    size = path.stat().st_size if exists else None
    return {
        "path": str(path),
        "exists": exists,
        "size_bytes": size,
        "modified_at": _iso_mtime(path) if exists else None,
    }


def env_model_overrides(provider: str | None = None) -> dict[str, str]:
    """Return {field: winning env var} for model fields the env currently sets."""
    provider = provider or "omlx"
    mapping = _DGX_ENV if provider == "dgx" else _OMLX_ENV
    found: dict[str, str] = {}
    if os.environ.get("ATLAS_LLM_PROVIDER") or os.environ.get("LLM_PROVIDER"):
        found["provider"] = (
            "ATLAS_LLM_PROVIDER"
            if os.environ.get("ATLAS_LLM_PROVIDER")
            else "LLM_PROVIDER"
        )
    for field, names in mapping.items():
        for name in names:
            if os.environ.get(name):
                found[field] = name
                break
    return found


def _served_models(
    settings: Settings,
) -> tuple[bool, list[str], str | None, bool, str]:
    """Probe the configured endpoint. Offline is a normal, non-fatal result."""
    from contingency_atlas.llm import LLMError, OMLXClient
    from contingency_atlas.web import runner

    reachable = runner.omlx_reachable(settings)
    try:
        client = OMLXClient(settings)
    except LLMError as exc:
        return False, [], str(exc), False, str(exc)
    try:
        client._timeout = 2.0
        probe_error: str | None = None
        served: list[str] = []
        try:
            served = list(client.list_models())
        except Exception as exc:
            probe_error = str(exc)
        supported, detail = client.responses_supported()
        return reachable or bool(served), served, probe_error, supported, detail
    finally:
        client.close()


def _scout_registry_payload() -> dict[str, Any]:
    from contingency_atlas.scouting import load_registry, registry_summary

    try:
        registry = load_registry()
    except Exception as exc:
        return {"available": False, "error": str(exc), "sources": []}
    summary = registry_summary(registry)
    return {
        "available": True,
        "schema_version": summary["schema_version"],
        "network_collection_default": summary["network_collection_default"],
        "last_reviewed_on": summary["last_reviewed_on"],
        "source_count": summary["source_count"],
        "enabled_source_count": summary["enabled_source_count"],
        "sources": [
            {
                "id": source.source_id,
                "name": source.name,
                "category": source.category,
                "enabled": source.enabled,
                "jurisdiction": source.jurisdiction,
                "homepage_url": source.homepage_url,
                "notes": source.notes,
            }
            for source in registry.sources
        ],
    }


def _framework_payload(settings: Settings) -> dict[str, Any]:
    try:
        atlas = framework_status(settings.scenario_db_path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        atlas = {
            "framework": "mitre-atlas",
            "installed": False,
            "available": False,
            "active_release": None,
            "active": None,
            "releases": [],
            "counts": {
                "tactics": 0,
                "techniques": 0,
                "mitigations": 0,
                "case_studies": 0,
                "relationships": 0,
            },
            "error": str(exc),
        }
    return {
        "mitre_atlas": atlas,
        "planned": [dict(item) for item in PLANNED_FRAMEWORKS],
        "import": {
            "max_bytes": DEFAULT_MAX_BYTES,
            "accepts": [".yaml", ".yml"],
            "recommended": dict(PINNED_ATLAS_RELEASE),
            "downloads": False,
        },
    }


def build_settings_payload(settings: Settings) -> dict[str, Any]:
    """Aggregate the Settings view. Catalog absence is not an application error."""
    reachable, served, probe_error, responses_supported, responses_detail = (
        _served_models(settings)
    )
    yaml_path = yaml_config_path()
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "frameworks": _framework_payload(settings),
        "feeds": {
            "scout_registry": _scout_registry_payload(),
            "planned": [dict(item) for item in PLANNED_FEEDS],
        },
        "inference": {
            "provider": settings.llm_provider,
            "base_url": safe_endpoint_url(settings.llm_base_url),
            "reachable": reachable,
            "configured": {
                "model": settings.llm_model,
                "model_judge": settings.llm_model_judge,
                "model_final_judge": settings.llm_model_final_judge,
                "use_responses_api": settings.llm_use_responses_api,
            },
            "served": served,
            "probe_error": safe_endpoint_message(probe_error),
            "responses_api": {
                "supported": responses_supported,
                "detail": safe_endpoint_message(responses_detail),
                "enabled": settings.llm_use_responses_api,
                "omlx_only": True,
            },
            "yaml_path": str(yaml_path),
            "yaml_exists": yaml_path.is_file(),
            "env_overrides": env_model_overrides(settings.llm_provider),
            "restart_required_for_changes": True,
        },
        "stores": {
            "engine": "sqlite+json",
            "note": (
                "Contingency Atlas does not run Neo4j. The local stores are "
                "SQLite and JSON. TwinOps is a separate stack."
            ),
            "scenario_db": _file_info(Path(settings.scenario_db_path)),
            "evidence_db": _file_info(Path(settings.evidence_db_path)),
            "emerging_risks": _file_info(Path(settings.emerging_risks_path)),
            "ontology_snapshot": _file_info(
                project_root() / "ontology" / "existing.ttl"
            ),
        },
    }


def _yaml_scalar(value: str | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    dumped = yaml.safe_dump(value, default_flow_style=True).strip()
    if dumped.endswith("\n..."):
        dumped = dumped[: -len("\n...")].strip()
    elif dumped == "...":
        dumped = '""'
    return dumped or '""'


def _split_comment(rest: str) -> tuple[str, str]:
    in_single = False
    in_double = False
    for index, char in enumerate(rest):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            cut = index
            while cut > 0 and rest[cut - 1] in " \t":
                cut -= 1
            return rest[:cut], rest[cut:]
    return rest, ""


def _top_level_key(line: str) -> str | None:
    if not line or line[0] in " \t#":
        return None
    match = re.match(r"^([A-Za-z_][\w-]*)\s*:", line)
    return match.group(1) if match else None


def update_yaml_model_settings(
    path: Path,
    *,
    provider: str | None = None,
    model: str | None = None,
    model_judge: str | None = None,
    model_final_judge: str | None = None,
    use_responses_api: bool | None = None,
) -> dict[str, Any]:
    """Rewrite only model-related YAML keys and keep comments intact."""
    if not path.is_file():
        raise SettingsActionError(f"No config file at {path}")
    original = path.read_text(encoding="utf-8")
    current = load_yaml_settings(path)
    target_provider = normalize_llm_provider(
        provider or current.get("llm_provider") or "omlx"
    )
    updates: dict[str, str | bool] = {}
    if provider is not None:
        updates["provider"] = target_provider
    if model is not None:
        updates["model"] = model
    if model_judge is not None:
        updates["model_judge"] = model_judge
    if model_final_judge is not None:
        updates["model_final_judge"] = model_final_judge
    if use_responses_api is not None:
        updates["use_responses_api"] = bool(use_responses_api)
    if not updates:
        raise SettingsActionError("No model fields were supplied")

    lines = original.splitlines(keepends=True)
    seen: set[str] = set()
    section: str | None = None
    rewritten: list[str] = []
    for line in lines:
        newline = ""
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith("\n"):
            body, newline = line[:-1], "\n"
        else:
            body, newline = line, ""
        top = _top_level_key(body)
        if top is not None:
            section = top
        match = _KEY_LINE.match(body)
        if match:
            key = match.group("key")
            indent = match.group("indent")
            in_backend = section == target_provider and not indent.strip()
            is_provider = key == "provider" and indent == ""
            if (is_provider and "provider" in updates) or (
                in_backend and key in updates and key != "provider"
            ):
                _, comment = _split_comment(match.group("rest"))
                body = (
                    f"{indent}{key}{match.group('sep')}"
                    f"{_yaml_scalar(updates[key])}{comment}"
                )
                seen.add(key)
        rewritten.append(body + newline)

    missing = [key for key in updates if key not in seen]
    if missing:
        rewritten = _insert_missing_keys(
            rewritten, target_provider, {key: updates[key] for key in missing}
        )

    text = "".join(rewritten)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    try:
        parsed = load_yaml_settings(path)
    except ValueError as exc:
        path.write_text(original, encoding="utf-8")
        raise SettingsActionError(f"YAML rewrite failed validation: {exc}") from exc
    return {
        "path": str(path),
        "provider": parsed.get("llm_provider", target_provider),
        "applied": {
            key: parsed.get(
                f"{target_provider}_{key}" if key != "provider" else "llm_provider"
            )
            for key in updates
        },
    }


def _insert_missing_keys(
    lines: list[str],
    section: str,
    missing: dict[str, str | bool],
) -> list[str]:
    """Append any keys the file did not already carry, under the right section."""
    if "provider" in missing:
        insert_at = 0
        for index, line in enumerate(lines):
            if line.strip() and not line.lstrip().startswith("#"):
                insert_at = index
                break
            insert_at = index + 1
        lines.insert(
            insert_at,
            f"provider: {_yaml_scalar(missing.pop('provider'))}\n",
        )
        if not missing:
            return lines

    section_at = None
    section_end = None
    for index, line in enumerate(lines):
        top = _top_level_key(line.rstrip("\r\n"))
        if top == section:
            section_at = index
            section_end = index + 1
            continue
        if section_at is not None and top is not None:
            break
        if section_at is not None:
            section_end = index + 1
    if section_at is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{section}:\n")
        section_end = len(lines)
    assert section_end is not None
    block = [
        f"  {key}: {_yaml_scalar(value)}\n" for key, value in missing.items()
    ]
    return lines[:section_end] + block + lines[section_end:]


def apply_model_settings(
    settings: Settings,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Write model ids to YAML. The running process keeps its current Settings."""
    provider = body.get("provider")
    if provider is not None:
        provider = normalize_llm_provider(str(provider))
    fields = {
        "model": body.get("model"),
        "model_judge": body.get("model_judge"),
        "model_final_judge": body.get("model_final_judge"),
    }
    supplied = {key: str(value) for key, value in fields.items() if value is not None}
    use_responses_api = body.get("use_responses_api")
    if use_responses_api is not None and not isinstance(use_responses_api, bool):
        raise SettingsActionError("use_responses_api must be a boolean")
    if provider is None and not supplied and use_responses_api is None:
        raise SettingsActionError("No model fields were supplied")
    for key, value in supplied.items():
        if len(value) > 200:
            raise SettingsActionError(f"{key} exceeds 200 characters")
        if any(ord(char) < 32 for char in value):
            raise SettingsActionError(f"{key} contains control characters")
        if key != "model_final_judge" and not value.strip():
            raise SettingsActionError(f"{key} cannot be empty")
    target = provider or settings.llm_provider
    if use_responses_api is True and target != "omlx":
        raise SettingsActionError("Responses API is an OMLX option")
    if target != "omlx":
        use_responses_api = None
    overrides = env_model_overrides(target)
    blocked_keys = list(supplied)
    if provider is not None:
        blocked_keys.append("provider")
    if use_responses_api is not None:
        blocked_keys.append("use_responses_api")
    blocked = [overrides[key] for key in blocked_keys if key in overrides]
    if blocked:
        raise SettingsActionError(
            "Environment variables override YAML for "
            + ", ".join(blocked)
            + "; unset them before changing models from Settings"
        )
    written = update_yaml_model_settings(
        yaml_config_path(),
        provider=provider,
        model=supplied.get("model"),
        model_judge=supplied.get("model_judge"),
        model_final_judge=supplied.get("model_final_judge"),
        use_responses_api=use_responses_api,
    )
    return {
        "ok": True,
        "restart_required": True,
        "path": written["path"],
        "applied": written["applied"],
        "note": "Restart the dashboard for the new models to take effect.",
    }


def _decode_yaml_bytes(filename: str, content_b64: str) -> bytes:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".yaml", ".yml"}:
        raise SettingsActionError("Uploaded catalog must be a .yaml or .yml file")
    try:
        raw = base64.b64decode(content_b64.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise SettingsActionError(f"content_b64 is not valid base64: {exc}") from exc
    if len(raw) > DEFAULT_MAX_BYTES:
        raise SettingsActionError(
            f"MITRE ATLAS input is {len(raw)} bytes; limit is {DEFAULT_MAX_BYTES} bytes"
        )
    return raw


def _resolve_local_catalog(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise SettingsActionError("local_path must be an absolute file path")
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise SettingsActionError(f"Cannot resolve local_path: {exc}") from exc
    if resolved.suffix.lower() not in {".yaml", ".yml"}:
        raise SettingsActionError("local_path must end in .yaml or .yml")
    if not resolved.is_file():
        raise SettingsActionError(f"local_path is not a readable file: {resolved}")
    return resolved


def import_atlas_from_request(
    settings: Settings,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Import a local operator-supplied ATLAS v6 file. No URL is fetched."""
    source_url = str(body.get("source_url") or "").strip()
    if not source_url:
        raise SettingsActionError("source_url is required and is recorded, never fetched")
    expected = str(body.get("expected_sha256") or "").strip() or None
    activate = body.get("activate", True)
    if not isinstance(activate, bool):
        raise SettingsActionError("activate must be a boolean")
    local_path = body.get("local_path")
    content_b64 = body.get("content_b64")
    filename = str(body.get("filename") or "ATLAS.yaml")
    has_path = isinstance(local_path, str) and local_path.strip()
    has_bytes = isinstance(content_b64, str) and content_b64.strip()
    if has_path == has_bytes:
        raise SettingsActionError("Provide exactly one of local_path or content_b64")

    tmp_path: Path | None = None
    try:
        if has_path:
            input_path = _resolve_local_catalog(str(local_path))
        else:
            raw = _decode_yaml_bytes(filename, str(content_b64))
            handle = tempfile.NamedTemporaryFile(
                prefix="atlas-mitre-",
                suffix=".yaml",
                delete=False,
            )
            try:
                handle.write(raw)
            finally:
                handle.close()
            tmp_path = Path(handle.name)
            input_path = tmp_path
        summary = import_mitre_atlas(
            settings.scenario_db_path,
            input_path,
            source_url,
            activate=activate,
            expected_sha256=expected,
        )
    except (FrameworkImportError, ImportSizeError, ReleaseCollisionError) as exc:
        raise SettingsActionError(str(exc)) from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    status = framework_status(settings.scenario_db_path)
    return {"ok": True, "import": summary, "status": status}


def activate_atlas_from_request(
    settings: Settings,
    body: dict[str, Any],
) -> dict[str, Any]:
    version = str(body.get("content_version") or "").strip()
    if not version:
        raise SettingsActionError("content_version is required")
    try:
        activate_release(settings.scenario_db_path, version)
    except KeyError as exc:
        raise SettingsActionError(str(exc)) from exc
    except ValueError as exc:
        raise SettingsActionError(str(exc)) from exc
    return {"ok": True, "status": framework_status(settings.scenario_db_path)}
