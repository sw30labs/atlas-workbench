"""I strictly import the offline MITRE ATLAS v6 YAML distribution."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from .store import (
    MITRE_ATLAS,
    TECHNIQUE_MATURITIES,
    TECHNIQUE_PLATFORMS,
    _activate_in_connection,
    _connect,
    migrate_frameworks,
)

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
MAX_YAML_DEPTH = 64
MAX_YAML_NODES = 100_000
MAX_YAML_ALIASES = 256
SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")

TOP_LEVEL_KEYS = frozenset(
    {
        "format-version",
        "collection",
        "matrix",
        "tactics",
        "techniques",
        "mitigations",
        "case-studies",
        "relationships",
    }
)
OBJECT_SECTIONS = {
    "tactics": ("tactic", re.compile(r"^AML\.TA[0-9]{4}$")),
    "techniques": (
        "technique",
        re.compile(r"^AML\.T[0-9]{4}(?:\.[0-9]{3})?$"),
    ),
    "mitigations": ("mitigation", re.compile(r"^AML\.M[0-9]{4}$")),
    "case-studies": ("case-study", re.compile(r"^AML\.CS[0-9]{4}$")),
}
FORMAT_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
STEP_ID = re.compile(r"^S[0-9]{2}$")
RELATIONSHIP_TYPES = frozenset(
    {"sequences", "achieves", "specializes", "mitigates", "employs"}
)
RELATIONSHIP_ALLOWED_FIELDS = {
    "sequences": frozenset(
        {"source", "target", "relationship-type", "description", "position"}
    ),
    "achieves": frozenset(
        {"source", "target", "relationship-type", "description"}
    ),
    "specializes": frozenset(
        {"source", "target", "relationship-type", "description"}
    ),
    "mitigates": frozenset(
        {"source", "target", "relationship-type", "description"}
    ),
    "employs": frozenset(
        {
            "source",
            "target",
            "relationship-type",
            "description",
            "tactic",
            "step-id",
            "leads-to",
        }
    ),
}
MITIGATION_CATEGORIES = frozenset(
    {"Policy", "Technical - AI", "Technical - Cyber"}
)
MITIGATION_LIFECYCLE_PHASES = frozenset(
    {
        "Business and Data Understanding",
        "Data Preparation",
        "AI Model Engineering",
        "AI Model Evaluation",
        "Deployment",
        "Monitoring and Maintenance",
    }
)
CASE_STUDY_TYPES = frozenset({"Incident", "Exercise"})
DATE_GRANULARITIES = frozenset({"Year", "Month", "Day"})


class FrameworkImportError(ValueError):
    """I use this error when a local framework file fails validation or import."""


class ImportSizeError(FrameworkImportError):
    """I use this error when input exceeds my pre-parse size boundary."""


class ReleaseCollisionError(FrameworkImportError):
    """I reject an existing content version presented with different bytes."""


class _BoundedSafeLoader(yaml.SafeLoader):
    """I bound YAML graph construction and reject ambiguous mapping keys."""

    def __init__(self, stream: Any) -> None:
        self._atlas_depth = 0
        self._atlas_nodes = 0
        self._atlas_aliases = 0
        super().__init__(stream)

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        event = self.peek_event()
        if isinstance(event, yaml.events.AliasEvent):
            self._atlas_aliases += 1
            if self._atlas_aliases > MAX_YAML_ALIASES:
                raise yaml.composer.ComposerError(
                    None,
                    None,
                    f"YAML aliases exceed my limit of {MAX_YAML_ALIASES}",
                    event.start_mark,
                )
        self._atlas_depth += 1
        self._atlas_nodes += 1
        try:
            if self._atlas_depth > MAX_YAML_DEPTH:
                raise yaml.composer.ComposerError(
                    None,
                    None,
                    f"YAML nesting exceeds my limit of {MAX_YAML_DEPTH}",
                    event.start_mark,
                )
            if self._atlas_nodes > MAX_YAML_NODES:
                raise yaml.composer.ComposerError(
                    None,
                    None,
                    f"YAML nodes exceed my limit of {MAX_YAML_NODES}",
                    event.start_mark,
                )
            return super().compose_node(parent, index)
        finally:
            self._atlas_depth -= 1

    def construct_mapping(
        self,
        node: yaml.Node,
        deep: bool = False,
    ) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "I expected a mapping node",
                node.start_mark,
            )
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "I require hashable mapping keys",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"I reject duplicate YAML key {key!r}",
                    key_node.start_mark,
                )
            if key == "<<":
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "I reject YAML merge keys",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


@dataclass(frozen=True)
class ParsedAtlas:
    content_version: str
    format_version: str
    release_date: str | None
    objects: tuple[dict[str, Any], ...]
    relationships: tuple[dict[str, Any], ...]
    counts: dict[str, int]


def canonical_json(value: Any) -> str:
    """I serialize normalized YAML values without platform-dependent variation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_yaml(
    value: Any,
    path: str = "document",
    *,
    _active: set[int] | None = None,
    _depth: int = 0,
    _budget: list[int] | None = None,
) -> Any:
    if _active is None:
        _active = set()
    if _budget is None:
        _budget = [MAX_YAML_NODES]
    _budget[0] -= 1
    if _budget[0] < 0:
        raise FrameworkImportError(
            f"{path} exceeds my normalized node limit of {MAX_YAML_NODES}"
        )
    if _depth > MAX_YAML_DEPTH:
        raise FrameworkImportError(
            f"{path} exceeds my nesting limit of {MAX_YAML_DEPTH}"
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FrameworkImportError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, Mapping)):
        identity = id(value)
        if identity in _active:
            raise FrameworkImportError(
                f"{path} contains a recursive YAML alias"
            )
        _active.add(identity)
        try:
            if isinstance(value, list):
                return [
                    _normalize_yaml(
                        item,
                        f"{path}[{index}]",
                        _active=_active,
                        _depth=_depth + 1,
                        _budget=_budget,
                    )
                    for index, item in enumerate(value)
                ]
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise FrameworkImportError(f"{path} contains a non-string key")
                normalized[key] = _normalize_yaml(
                    item,
                    f"{path}.{key}",
                    _active=_active,
                    _depth=_depth + 1,
                    _budget=_budget,
                )
            return normalized
        finally:
            _active.remove(identity)
    raise FrameworkImportError(
        f"{path} contains unsupported YAML value {type(value).__name__}"
    )


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FrameworkImportError(f"{path} must be a mapping")
    return value


def _string(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    *,
    nonempty: bool = True,
) -> str:
    if key not in mapping or not isinstance(mapping[key], str):
        raise FrameworkImportError(f"{path}.{key} must be a string")
    value = mapping[key]
    if nonempty and not value.strip():
        raise FrameworkImportError(f"{path}.{key} must be non-empty")
    return value


def _string_list(
    value: Any,
    path: str,
    *,
    allowed: frozenset[str] | None = None,
    nonempty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise FrameworkImportError(f"{path} must be a list of strings")
    if nonempty and not value:
        raise FrameworkImportError(f"{path} must not be empty")
    if len(value) != len(set(value)):
        raise FrameworkImportError(f"{path} must not contain duplicates")
    if allowed is not None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise FrameworkImportError(f"{path} contains unknown values: {unknown}")
    return value


def _validate_common_object(
    payload: dict[str, Any],
    *,
    key: str,
    path: str,
    expected_type: str,
    id_pattern: re.Pattern[str],
) -> None:
    object_id = _string(payload, "id", path)
    if object_id != key:
        raise FrameworkImportError(
            f"{path}.id must match its map key {key!r}"
        )
    if not id_pattern.fullmatch(object_id):
        raise FrameworkImportError(f"{path}.id is not a valid {expected_type} ID")
    if _string(payload, "object-type", path) != expected_type:
        raise FrameworkImportError(
            f"{path}.object-type must be {expected_type!r}"
        )
    _string(payload, "name", path)
    _string(payload, "description", path, nonempty=False)
    references = payload.get("references")
    if not isinstance(references, list):
        raise FrameworkImportError(f"{path}.references must be a list")


def _validate_type_specific_object(
    payload: dict[str, Any], object_type: str, path: str
) -> tuple[str | None, list[str]]:
    maturity: str | None = None
    platforms: list[str] = []
    if object_type == "technique":
        maturity = _string(payload, "maturity", path)
        if maturity not in TECHNIQUE_MATURITIES:
            raise FrameworkImportError(
                f"{path}.maturity is not a recognized MITRE ATLAS maturity"
            )
        platforms = _string_list(
            payload.get("platforms"),
            f"{path}.platforms",
            allowed=TECHNIQUE_PLATFORMS,
            nonempty=True,
        )
    elif "maturity" in payload or "platforms" in payload:
        raise FrameworkImportError(
            f"{path} may not define technique-only maturity or platforms"
        )

    if object_type == "mitigation":
        _string_list(
            payload.get("lifecycle-phases"),
            f"{path}.lifecycle-phases",
            allowed=MITIGATION_LIFECYCLE_PHASES,
            nonempty=True,
        )
        _string_list(
            payload.get("categories"),
            f"{path}.categories",
            allowed=MITIGATION_CATEGORIES,
            nonempty=True,
        )
    if object_type == "case-study":
        if _string(payload, "type", path) not in CASE_STUDY_TYPES:
            raise FrameworkImportError(f"{path}.type is not recognized")
        if _string(payload, "date-granularity", path) not in DATE_GRANULARITIES:
            raise FrameworkImportError(
                f"{path}.date-granularity is not recognized"
            )
        _string(payload, "date", path)
        _string(payload, "actor", path, nonempty=False)
        _string(payload, "target", path, nonempty=False)
    return maturity, platforms


def _validate_container_objects(
    raw: dict[str, Any]
) -> tuple[str, str, str | None]:
    format_version = raw["format-version"]
    if not isinstance(format_version, str) or not FORMAT_VERSION.fullmatch(
        format_version
    ):
        raise FrameworkImportError("format-version must be a semantic version")
    if int(format_version.split(".", 1)[0]) != 6:
        raise FrameworkImportError(
            f"Only MITRE ATLAS v6 is supported, got {format_version!r}"
        )

    collection = _mapping(raw["collection"], "collection")
    if _string(collection, "id", "collection") != "ATLAS-collection":
        raise FrameworkImportError("collection.id must be 'ATLAS-collection'")
    if _string(collection, "object-type", "collection") != "collection":
        raise FrameworkImportError("collection.object-type must be 'collection'")
    _string(collection, "name", "collection")
    _string(collection, "description", "collection", nonempty=False)
    content_version = _string(collection, "version", "collection")
    if len(content_version) > 128 or any(ord(c) < 32 or ord(c) > 126 for c in content_version):
        raise FrameworkImportError("collection.version must be printable ASCII")

    matrix = _mapping(raw["matrix"], "matrix")
    if _string(matrix, "id", "matrix") != "ATLAS-matrix":
        raise FrameworkImportError("matrix.id must be 'ATLAS-matrix'")
    if _string(matrix, "object-type", "matrix") != "matrix":
        raise FrameworkImportError("matrix.object-type must be 'matrix'")
    _string(matrix, "name", "matrix")
    _string(matrix, "description", "matrix", nonempty=False)

    release_date = collection.get("release-date")
    if release_date is not None and not isinstance(release_date, str):
        raise FrameworkImportError("collection.release-date must be a string")
    return content_version, format_version, release_date


def _parse_objects(
    raw: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, int]]:
    objects: list[dict[str, Any]] = []
    object_types: dict[str, str] = {
        "ATLAS-collection": "collection",
        "ATLAS-matrix": "matrix",
    }
    counts = {
        "tactics": 0,
        "techniques": 0,
        "mitigations": 0,
        "case_studies": 0,
    }
    count_keys = {
        "tactics": "tactics",
        "techniques": "techniques",
        "mitigations": "mitigations",
        "case-studies": "case_studies",
    }
    for section, (object_type, id_pattern) in OBJECT_SECTIONS.items():
        entries = _mapping(raw[section], section)
        for key in sorted(entries):
            if not isinstance(key, str):
                raise FrameworkImportError(f"{section} contains a non-string ID")
            path = f"{section}.{key}"
            payload = _mapping(entries[key], path)
            _validate_common_object(
                payload,
                key=key,
                path=path,
                expected_type=object_type,
                id_pattern=id_pattern,
            )
            maturity, platforms = _validate_type_specific_object(
                payload, object_type, path
            )
            if key in object_types:
                raise FrameworkImportError(f"Duplicate ATLAS object ID: {key}")
            object_types[key] = object_type
            objects.append(
                {
                    "object_id": key,
                    "object_type": object_type,
                    "name": payload["name"],
                    "description": payload["description"],
                    "maturity": maturity,
                    "platforms_json": canonical_json(sorted(platforms)),
                    "object_hash": _hash_json(payload),
                    "payload_json": canonical_json(payload),
                }
            )
        counts[count_keys[section]] = len(entries)
    return objects, object_types, counts


def _expected_relationship_types(
    relationship_type: str,
) -> tuple[str, str]:
    return {
        "sequences": ("matrix", "tactic"),
        "achieves": ("technique", "tactic"),
        "specializes": ("technique", "technique"),
        "mitigates": ("mitigation", "technique"),
        "employs": ("case-study", "technique"),
    }[relationship_type]


def _validate_relationship_metadata(
    relationship: dict[str, Any], relationship_type: str, path: str
) -> None:
    unknown = set(relationship) - RELATIONSHIP_ALLOWED_FIELDS[relationship_type]
    if unknown:
        raise FrameworkImportError(
            f"{path} contains unsupported fields: {sorted(unknown)}"
        )
    description = relationship.get("description")
    if description is not None and not isinstance(description, str):
        raise FrameworkImportError(f"{path}.description must be a string or null")
    if relationship_type == "sequences":
        position = relationship.get("position")
        if isinstance(position, bool) or not isinstance(position, int) or position < 1:
            raise FrameworkImportError(f"{path}.position must be a positive integer")
    if relationship_type == "employs":
        tactic = relationship.get("tactic")
        if not isinstance(tactic, str) or not OBJECT_SECTIONS["tactics"][1].fullmatch(tactic):
            raise FrameworkImportError(f"{path}.tactic must be an ATLAS tactic ID")
        step_id = relationship.get("step-id")
        if step_id is not None and (
            not isinstance(step_id, str) or not STEP_ID.fullmatch(step_id)
        ):
            raise FrameworkImportError(f"{path}.step-id must match S##")
        if "leads-to" in relationship:
            leads_to = _string_list(relationship["leads-to"], f"{path}.leads-to")
            if any(not STEP_ID.fullmatch(item) for item in leads_to):
                raise FrameworkImportError(f"{path}.leads-to entries must match S##")


def _parse_relationships(
    raw: dict[str, Any], object_types: dict[str, str]
) -> list[dict[str, Any]]:
    relationship_groups = _mapping(raw["relationships"], "relationships")
    relationships: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    employs_steps: dict[str, set[str]] = {}
    employs_leads: list[tuple[str, str, list[str]]] = []
    sequence_positions: set[int] = set()

    for owner in sorted(relationship_groups):
        if not isinstance(owner, str):
            raise FrameworkImportError("relationships contains a non-string owner")
        if owner not in object_types:
            raise FrameworkImportError(
                f"relationships.{owner} references an unknown owner"
            )
        groups = _mapping(relationship_groups[owner], f"relationships.{owner}")
        unknown_groups = set(groups) - RELATIONSHIP_TYPES
        if unknown_groups:
            raise FrameworkImportError(
                f"relationships.{owner} contains unknown relationship types: "
                f"{sorted(unknown_groups)}"
            )
        for relationship_type in sorted(groups):
            entries = groups[relationship_type]
            if not isinstance(entries, list):
                raise FrameworkImportError(
                    f"relationships.{owner}.{relationship_type} must be a list"
                )
            for index, raw_relationship in enumerate(entries):
                path = f"relationships.{owner}.{relationship_type}[{index}]"
                relationship = _mapping(raw_relationship, path)
                source = _string(relationship, "source", path)
                target = _string(relationship, "target", path)
                actual_type = _string(relationship, "relationship-type", path)
                if source != owner:
                    raise FrameworkImportError(
                        f"{path}.source must match relationship owner {owner!r}"
                    )
                if actual_type != relationship_type:
                    raise FrameworkImportError(
                        f"{path}.relationship-type must match {relationship_type!r}"
                    )
                if source not in object_types or target not in object_types:
                    raise FrameworkImportError(
                        f"{path} contains a dangling source or target reference"
                    )
                expected_source, expected_target = _expected_relationship_types(
                    relationship_type
                )
                if object_types[source] != expected_source or object_types[target] != expected_target:
                    raise FrameworkImportError(
                        f"{path} must connect {expected_source} to {expected_target}"
                    )
                if relationship_type == "specializes":
                    if "." not in source.removeprefix("AML.T") or "." in target.removeprefix("AML.T"):
                        raise FrameworkImportError(
                            f"{path} must connect a sub-technique to a parent technique"
                        )
                    if source.rsplit(".", 1)[0] != target:
                        raise FrameworkImportError(
                            f"{path} specializes the wrong parent technique"
                        )
                _validate_relationship_metadata(relationship, relationship_type, path)
                if relationship_type == "employs":
                    tactic = str(relationship["tactic"])
                    if object_types.get(tactic) != "tactic":
                        raise FrameworkImportError(f"{path}.tactic is dangling")
                    step_id = relationship.get("step-id")
                    if step_id is not None:
                        steps = employs_steps.setdefault(owner, set())
                        if step_id in steps:
                            raise FrameworkImportError(
                                f"{path}.step-id is duplicated within {owner}"
                            )
                        steps.add(step_id)
                    employs_leads.append(
                        (owner, path, list(relationship.get("leads-to", [])))
                    )
                if relationship_type == "sequences":
                    position = int(relationship["position"])
                    if position in sequence_positions:
                        raise FrameworkImportError(
                            f"{path}.position duplicates matrix position {position}"
                        )
                    sequence_positions.add(position)

                key = _hash_json(relationship)
                if key in seen_keys:
                    raise FrameworkImportError(f"{path} duplicates a relationship")
                seen_keys.add(key)
                metadata = {
                    item_key: item_value
                    for item_key, item_value in relationship.items()
                    if item_key not in {"source", "target", "relationship-type"}
                }
                relationships.append(
                    {
                        "relationship_key": key,
                        "source_id": source,
                        "relationship_type": relationship_type,
                        "target_id": target,
                        "metadata_json": canonical_json(metadata),
                    }
                )

    for owner, path, leads_to in employs_leads:
        known_steps = employs_steps.get(owner, set())
        dangling = sorted(set(leads_to) - known_steps)
        if dangling:
            raise FrameworkImportError(
                f"{path}.leads-to contains unknown case-study steps: {dangling}"
            )
    return sorted(relationships, key=lambda item: item["relationship_key"])


def parse_mitre_atlas(raw: Any) -> ParsedAtlas:
    """I validate and normalize an already-loaded MITRE ATLAS v6 document."""

    normalized = _normalize_yaml(raw)
    document = _mapping(normalized, "document")
    missing = TOP_LEVEL_KEYS - set(document)
    unknown = set(document) - TOP_LEVEL_KEYS
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if unknown:
            details.append(f"unknown {sorted(unknown)}")
        raise FrameworkImportError(
            "MITRE ATLAS v6 top-level keys are invalid: " + "; ".join(details)
        )
    content_version, format_version, release_date = _validate_container_objects(
        document
    )
    objects, object_types, counts = _parse_objects(document)
    relationships = _parse_relationships(document, object_types)
    return ParsedAtlas(
        content_version=content_version,
        format_version=format_version,
        release_date=release_date,
        objects=tuple(objects),
        relationships=tuple(relationships),
        counts=counts,
    )


def _validate_expected_sha256(expected_sha256: str | None) -> str | None:
    if expected_sha256 is None:
        return None
    expected = str(expected_sha256).strip()
    if not SHA256_HEX.fullmatch(expected):
        raise FrameworkImportError(
            "expected_sha256 must contain exactly 64 hexadecimal characters"
        )
    return expected.lower()


def _read_local_bytes(path: Path, max_bytes: int) -> bytes:
    if isinstance(max_bytes, bool) or int(max_bytes) < 1:
        raise ValueError("max_bytes must be a positive integer")
    max_bytes = int(max_bytes)
    try:
        # I inspect and read one already-open descriptor, so a path swap cannot
        # make my size decision apply to different bytes than I later parse.
        with path.open("rb") as handle:
            descriptor = os.fstat(handle.fileno())
            if not stat.S_ISREG(descriptor.st_mode):
                raise FrameworkImportError(
                    f"Input path is not a regular file: {path}"
                )
            if descriptor.st_size > max_bytes:
                raise ImportSizeError(
                    "MITRE ATLAS input is "
                    f"{descriptor.st_size} bytes; limit is {max_bytes} bytes"
                )
            # I cap the only input allocation at one byte beyond the boundary;
            # that sentinel detects growth without ever reading an unbounded file.
            raw_bytes = handle.read(max_bytes + 1)
    except OSError as exc:
        raise FrameworkImportError(f"Cannot read input file {path}: {exc}") from exc
    if len(raw_bytes) > max_bytes:
        raise ImportSizeError(
            f"MITRE ATLAS input grew beyond the {max_bytes}-byte limit"
        )
    return raw_bytes


def _load_local_yaml(raw_bytes: bytes) -> Any:
    try:
        return yaml.load(raw_bytes, Loader=_BoundedSafeLoader)
    except yaml.YAMLError as exc:
        raise FrameworkImportError(f"Invalid MITRE ATLAS YAML: {exc}") from exc


def _existing_summary(
    conn: sqlite3.Connection,
    parsed: ParsedAtlas,
    source_sha256: str,
    activate: bool,
) -> dict[str, Any]:
    if activate:
        _activate_in_connection(conn, parsed.content_version, MITRE_ATLAS)
    active_row = conn.execute(
        """
        SELECT is_active FROM framework_releases
        WHERE framework = ? AND content_version = ?
        """,
        (MITRE_ATLAS, parsed.content_version),
    ).fetchone()
    relation_count = conn.execute(
        """
        SELECT COUNT(*) FROM framework_relationships
        WHERE framework = ? AND content_version = ?
        """,
        (MITRE_ATLAS, parsed.content_version),
    ).fetchone()[0]
    return {
        "framework": MITRE_ATLAS,
        "content_version": parsed.content_version,
        "format_version": parsed.format_version,
        "objects": dict(parsed.counts),
        "relationships": int(relation_count),
        "source_sha256": source_sha256,
        "activated": bool(active_row["is_active"]),
        "already_imported": True,
    }


def import_mitre_atlas(
    db_path: str | Path,
    input_path: str | Path,
    source_url: str,
    activate: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """I import one pinned local v6 release in a catalog transaction.

    I record ``source_url`` as operator-asserted provenance; I never retrieve
    it.  When supplied, I verify ``expected_sha256`` against the exact bytes I
    read before I parse YAML or create the database.
    """

    source_url = str(source_url).strip()
    if not source_url or len(source_url) > 2048 or any(ord(c) < 32 for c in source_url):
        raise FrameworkImportError("source_url must be a non-empty, bounded string")
    expected_digest = _validate_expected_sha256(expected_sha256)
    raw_bytes = _read_local_bytes(Path(input_path), max_bytes)
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if expected_digest is not None and source_sha256 != expected_digest:
        raise FrameworkImportError(
            "MITRE ATLAS input SHA-256 does not match expected_sha256: "
            f"expected {expected_digest}, got {source_sha256}"
        )
    document = _load_local_yaml(raw_bytes)
    parsed = parse_mitre_atlas(document)

    migrate_frameworks(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT source_sha256 FROM framework_releases
            WHERE framework = ? AND content_version = ?
            """,
            (MITRE_ATLAS, parsed.content_version),
        ).fetchone()
        if existing is not None:
            if str(existing["source_sha256"]) != source_sha256:
                raise ReleaseCollisionError(
                    "MITRE ATLAS content version "
                    f"{parsed.content_version!r} is already pinned to different bytes"
                )
            summary = _existing_summary(
                conn, parsed, source_sha256, bool(activate)
            )
            conn.commit()
            return summary

        imported_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        conn.execute(
            """
            INSERT INTO framework_releases (
                framework, content_version, format_version, release_date,
                source_url, source_sha256, imported_at, is_active, is_sealed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                MITRE_ATLAS,
                parsed.content_version,
                parsed.format_version,
                parsed.release_date,
                source_url,
                source_sha256,
                imported_at,
            ),
        )
        conn.executemany(
            """
            INSERT INTO framework_objects (
                framework, content_version, object_id, object_type, name,
                description, maturity, platforms_json, object_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    MITRE_ATLAS,
                    parsed.content_version,
                    item["object_id"],
                    item["object_type"],
                    item["name"],
                    item["description"],
                    item["maturity"],
                    item["platforms_json"],
                    item["object_hash"],
                    item["payload_json"],
                )
                for item in parsed.objects
            ],
        )
        conn.executemany(
            """
            INSERT INTO framework_relationships (
                framework, content_version, relationship_key, source_id,
                relationship_type, target_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    MITRE_ATLAS,
                    parsed.content_version,
                    item["relationship_key"],
                    item["source_id"],
                    item["relationship_type"],
                    item["target_id"],
                    item["metadata_json"],
                )
                for item in parsed.relationships
            ],
        )
        conn.execute(
            """
            UPDATE framework_releases
            SET is_sealed = 1
            WHERE framework = ? AND content_version = ? AND is_sealed = 0
            """,
            (MITRE_ATLAS, parsed.content_version),
        )
        if activate:
            _activate_in_connection(conn, parsed.content_version, MITRE_ATLAS)
        conn.commit()
        return {
            "framework": MITRE_ATLAS,
            "content_version": parsed.content_version,
            "format_version": parsed.format_version,
            "objects": dict(parsed.counts),
            "relationships": len(parsed.relationships),
            "source_sha256": source_sha256,
            "activated": bool(activate),
            "already_imported": False,
        }
    except (FrameworkImportError, sqlite3.Error):
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
