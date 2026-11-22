"""Explicit local activation of the separately stored organization workspace."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import yaml

from workbench_atlas.config import yaml_config_path
from workbench_atlas.review import register_lock


def _matches_configuration(text, expected):
    """A text-preserving edit is safe only if its parsed meaning is exact."""
    try:
        return yaml.safe_load(text) == expected
    except yaml.YAMLError:
        return False


def enable_context(settings, *, actor):
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 160:
        raise ValueError("A named operator is required.")
    if os.environ.get("ATLAS_CONTEXT_ENABLED", "").strip().lower() in {"0", "false", "no", "off"}:
        raise ValueError("Organization context is disabled by your deployment settings. Ask the operator to enable it.")
    path = yaml_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with register_lock(path):
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            config = yaml.safe_load(original)
        except yaml.YAMLError as exc:
            raise ValueError("The workspace configuration is not valid YAML. Repair it before activation.") from exc
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("The configuration must be a YAML mapping.")
        section = config.get("organization_context")
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise ValueError("The organization context configuration is invalid.")
        # Copy both mappings so an alias shared with another section is not
        # changed accidentally. Preserve fields understood by future versions.
        intended_section = {**section, "enabled": True,
                            "db_path": str(section.get("db_path") or settings.context_db_path)}
        intended = {**config, "organization_context": intended_section}
        # Preserve existing configuration text, comments and unrelated sections.
        lines = original.splitlines(keepends=True)
        start = next((i for i, line in enumerate(lines) if line.startswith("organization_context:")), None)
        replacement = yaml.safe_dump({"organization_context": intended_section}, sort_keys=False)
        audit = f"# Organization workspace activated {datetime.now(timezone.utc).isoformat()}\n"
        if start is not None:
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].strip() and not lines[i][0].isspace()
                        and not lines[i].startswith("#")), len(lines))
            text = "".join(lines[:start]) + audit + replacement + "".join(lines[end:])
        else:
            text = original.rstrip() + "\n\n" + audit + replacement
        # Flow mappings, quoted keys, indentation and YAML aliases can defeat
        # the line-based edit. Fall back to a complete, equivalent serialization
        # before creating any temporary replacement file.
        if not _matches_configuration(text, intended):
            text = audit + yaml.safe_dump(intended, sort_keys=False, allow_unicode=True)
        if not _matches_configuration(text, intended):
            raise ValueError("The updated configuration could not be verified; the original file was preserved.")
        fd, name = tempfile.mkstemp(prefix=".atlas-config-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(text)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return {"enabled": True, "actor": actor.strip(),
            "message": "Organization workspace enabled. Organization records stay in their separate local store."}
