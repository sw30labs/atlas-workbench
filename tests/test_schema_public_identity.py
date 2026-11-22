"""Public-schema identity tests for the release lexicon.

Planted forbidden strings fail, live shipped keys stay disjoint from the
forbidden set, and excluded hosts are rejected by network admission.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from workbench_atlas.independence_lexicon import (
    RESEARCH_HOST_DENY,
    SCHEMA_FORBIDDEN_IDENTIFIERS,
    SCHEMA_FORBIDDEN_PREFIXES,
    forbidden_schema_names,
    host_is_denied,
)
from workbench_atlas.schema import EVIDENCE_EVENT_COLUMNS, SCENARIO_COLUMNS
from workbench_atlas.scoring.models import RiskAtom

ROOT = Path(__file__).resolve().parents[1]


def _joined(*parts: str) -> str:
    """Build audit terms so this file does not trip the gate it supports."""
    return "".join(parts)


def _json_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(str(key))
            keys.update(_json_keys(nested))
    elif isinstance(value, list):
        for item in value:
            keys.update(_json_keys(item))
    return keys


def test_planted_schema_identifier_is_forbidden():
    planted = _joined("Event Type Level 1 ", "Name")
    assert forbidden_schema_names([planted, "event_driver_class"]) == (planted,)


def test_planted_alias_prefix_is_forbidden():
    planted = _joined("cm", "db", "_info_", "AAName")
    assert forbidden_schema_names([planted, "event_id"]) == (planted,)


def test_reminted_evidence_and_scoring_identifiers_stay_retired():
    retired = [
        _joined("news", "_event"),
        _joined("News", "Event"),
        _joined("news", "_body"),
        _joined("event_", "type"),
        _joined("root_", "cause"),
        _joined("taxonomy_", "event_type"),
        _joined("bu_", "code"),
    ]
    assert forbidden_schema_names(retired) == tuple(retired)


def test_shipped_schema_columns_are_canonical():
    names = {name for name, _ in EVIDENCE_EVENT_COLUMNS} | {
        name for name, _ in SCENARIO_COLUMNS
    }
    names.update(RiskAtom.__dataclass_fields__)
    assert not forbidden_schema_names(names)


def test_demo_fixture_keys_are_canonical():
    keys: set[str] = set()
    for relative in (
        "examples/demo_evidence.json",
        "examples/demo_emerging_risks.json",
        "examples/demo_scenarios.json",
        "examples/fixture_manifest.json",
    ):
        payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        keys.update(_json_keys(payload))
    assert not forbidden_schema_names(keys)


def test_lexicon_source_keeps_proper_nouns_fragmented():
    """PAUSE: completed proper nouns and leftover alias keys stay fragmented."""
    source = (
        ROOT / "workbench_atlas" / "independence_lexicon.py"
    ).read_text(encoding="utf-8")
    assert _joined("cm", "db", "_info_") not in source
    employer = _joined("U", "BS")
    industry = _joined("O", "RX")
    assert not re.search(r"(?i)\b" + re.escape(employer) + r"\b", source)
    assert not re.search(r"(?i)\b" + re.escape(industry) + r"\b", source)


def test_host_denylist_is_defined():
    assert RESEARCH_HOST_DENY
    assert SCHEMA_FORBIDDEN_IDENTIFIERS
    assert SCHEMA_FORBIDDEN_PREFIXES


def test_excluded_provider_hosts_are_denied():
    industry = _joined("o", "rx", ".org")
    employer_com = _joined("u", "bs", ".com")
    employer_ch = _joined("u", "bs", ".ch")
    assert host_is_denied(industry)
    assert host_is_denied("www." + industry)
    assert host_is_denied("https://blog." + industry + "/resource")
    assert host_is_denied(employer_com)
    assert host_is_denied("https://www." + employer_ch + "/")


def test_synthetic_scoring_codes_are_project_native():
    from workbench_atlas.scoring.synthetic_events import (
        SCORING_CELL_CODES,
        SEGMENT_CODES,
    )

    assert SCORING_CELL_CODES
    assert SEGMENT_CODES
    assert all(code.startswith("DEMO-CELL-") for code in SCORING_CELL_CODES)
    assert all(code.startswith("DEMO-SEGMENT-") for code in SEGMENT_CODES)


def test_unrelated_hosts_are_not_denied():
    industry = _joined("o", "rx", ".org")
    employer_com = _joined("u", "bs", ".com")
    assert not host_is_denied("example.org")
    assert not host_is_denied("https://www.sec.gov/news")
    assert not host_is_denied("not" + industry)
    assert not host_is_denied("not" + employer_com)
    assert not host_is_denied("")
    assert not host_is_denied(None)
