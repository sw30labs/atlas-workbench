"""Discover and inspect shipped prompt constants without a closed inventory."""

from __future__ import annotations

import ast
import importlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from contingency_atlas.independence_lexicon import (
    DISCLOSURE_ONLY,
    STRING_FORBIDDEN,
    scan_prompt_residue,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]*-v[1-9][0-9]*$")

# Protected modules can declare release-audit versions here until their next
# domain-specific prompt revision. Discovery remains dynamic; this is only a
# version-resolution exception list, not a prompt inventory.
PROMPT_VERSION_OVERRIDES: dict[tuple[str, str], str] = {
    (
        "contingency_atlas.research",
        "RESEARCH_SYSTEM_PROMPT",
    ): "research-planner-v1",
    (
        "contingency_atlas.scoring.ext_features",
        "_LLM_NULL_PROMPT",
    ): "external-feature-null-v1",
    (
        "contingency_atlas.scoring.ext_features",
        "_LLM_VOTE_PROMPT",
    ): "external-feature-vote-v1",
}


@dataclass(frozen=True)
class PromptRecord:
    """One dynamically discovered prompt constant and its audit version."""

    module: str
    name: str
    texts: tuple[str, ...]
    version: str | None

    @property
    def qualified_name(self) -> str:
        return f"{self.module}.{self.name}"


def _assignment_names(node: ast.stmt) -> tuple[str, ...]:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets.extend(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets.append(node.target)
    return tuple(target.id for target in targets if isinstance(target, ast.Name))


def _is_prompt_constant(name: str) -> bool:
    upper = name.upper()
    if "VERSION" in upper or upper.endswith(("_CHARS", "_BYTES")):
        return False
    return (
        "PROMPT" in upper
        or upper.endswith("_SYSTEM")
        or upper.endswith("_SYSTEMS")
        or upper.endswith("_TEMPLATE")
        or upper.endswith("_TEMPLATES")
    )


def _module_name(path: Path, package_root: Path) -> str:
    relative = path.relative_to(package_root.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def discover_prompt_names(
    package_root: Path = PACKAGE_ROOT,
) -> tuple[tuple[str, str], ...]:
    """Statically discover module-level prompt-like constant names."""
    discovered: set[tuple[str, str]] = set()
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_name = _module_name(path, package_root)
        for node in tree.body:
            for name in _assignment_names(node):
                if _is_prompt_constant(name):
                    discovered.add((module_name, name))
    return tuple(sorted(discovered))


def _prompt_texts(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Mapping):
        values: Iterable[object] = value.values()
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        return ()
    texts: list[str] = []
    for nested in values:
        texts.extend(_prompt_texts(nested))
    return tuple(texts)


def _version_candidates(name: str) -> tuple[str, ...]:
    stripped = name.strip("_").upper()
    stems = [stripped]
    for suffix in (
        "_SYSTEM_PROMPT",
        "_PROMPT_TEMPLATE",
        "_SYSTEMS",
        "_SYSTEM",
        "_TEMPLATES",
        "_TEMPLATE",
        "_PROMPT",
    ):
        if stripped.endswith(suffix):
            stems.append(stripped[: -len(suffix)])
    candidates = [f"{stripped}_VERSION"]
    for stem in stems:
        candidates.extend((f"{stem}_PROMPT_VERSION", f"{stem}_VERSION"))
    candidates.append("PROMPT_VERSION")
    return tuple(dict.fromkeys(candidates))


def _resolve_version(module: object, module_name: str, name: str) -> str | None:
    override = PROMPT_VERSION_OVERRIDES.get((module_name, name))
    if override is not None:
        return override
    for candidate in _version_candidates(name):
        value = getattr(module, candidate, None)
        if isinstance(value, str) and value:
            return value
    module_versions = {
        value
        for attr, value in vars(module).items()
        if attr.endswith("PROMPT_VERSION") and isinstance(value, str) and value
    }
    if len(module_versions) == 1:
        return next(iter(module_versions))
    return None


def discover_prompts(
    package_root: Path = PACKAGE_ROOT,
) -> tuple[PromptRecord, ...]:
    """Import discovered modules and return all prompt-valued constants."""
    records: list[PromptRecord] = []
    for module_name, name in discover_prompt_names(package_root):
        module = importlib.import_module(module_name)
        if not hasattr(module, name):
            continue
        texts = _prompt_texts(getattr(module, name))
        if not texts:
            continue
        records.append(
            PromptRecord(
                module=module_name,
                name=name,
                texts=texts,
                version=_resolve_version(module, module_name, name),
            )
        )
    return tuple(records)


def inspect_prompt_registry(
    records: Iterable[PromptRecord] | None = None,
) -> list[str]:
    """Return versioning or identity-residue failures for discovered prompts."""
    failures: list[str] = []
    active_records = tuple(records) if records is not None else discover_prompts()
    if not active_records:
        return ["no prompt constants were discovered"]
    for record in active_records:
        if record.version is None:
            failures.append(f"{record.qualified_name}: prompt version is missing")
        elif not _VERSION.fullmatch(record.version):
            failures.append(f"{record.qualified_name}: prompt version is invalid")
        for text in record.texts:
            for label in scan_prompt_residue(text):
                failures.append(f"{record.qualified_name}: {label}")
            for label, pattern in (*STRING_FORBIDDEN.items(), *DISCLOSURE_ONLY.items()):
                if pattern.search(text):
                    failures.append(f"{record.qualified_name}: {label}")
    return failures
