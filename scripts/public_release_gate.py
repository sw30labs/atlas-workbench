#!/usr/bin/env python3
"""Fail closed when a release candidate violates public-repository boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Residue terms live in the independence lexicon so planted-identity tests
# and this scanner cannot drift. Path allowlists stay here.
from contingency_atlas.independence_lexicon import (
    DISCLOSURE_ONLY,
    scan_identifier_residue,
)
from contingency_atlas.independence_lexicon import (
    STRING_FORBIDDEN as GLOBAL_FORBIDDEN,
)

MAX_RELEASE_FILE_BYTES = 2_000_000
MAX_RELEASE_ARTIFACT_BYTES = 100_000_000
MAX_RELEASE_ARTIFACT_MEMBERS = 20_000

REQUIRED_FILES = {
    "ACKNOWLEDGMENTS.md",
    "CITATION.cff",
    "CONTRIBUTING.md",
    "DATA_LICENSE.md",
    "DATA_PROVENANCE.md",
    "DISCLAIMER.md",
    "LICENSE",
    "LICENSES/CC0-1.0.txt",
    "docs/release/PUBLIC_RELEASE_CHECKLIST.md",
    "README.md",
    "SECURITY.md",
    "docs/reference/TAXONOMY.md",
    "THIRD_PARTY_NOTICES.md",
    "contingency_atlas/source_registry.yaml",
    "docs/reference/RADAR_AND_BRIEFS.md",
    "examples/demo_emerging_risks.json",
    "examples/demo_evidence.json",
    "examples/demo_scenarios.json",
    "examples/fixture_manifest.json",
    "examples/README.md",
}

# Disclosure permissions are per rule and per path. In particular, no file is
# permitted to name the excluded employer merely because it cites an unrelated
# industry resource or acknowledgment.
DISCLOSURE_EXEMPTIONS: dict[str, frozenset[str]] = {
    "external industry resource": frozenset(
        {
            "ACKNOWLEDGMENTS.md",
            "CONTRIBUTING.md",
            "DATA_LICENSE.md",
            "DATA_PROVENANCE.md",
            "DISCLAIMER.md",
            "docs/release/PUBLIC_RELEASE_CHECKLIST.md",
            "README.md",
            "THIRD_PARTY_NOTICES.md",
            "contingency_atlas/web/about.yaml",
            "docs/research/essays/checklist-empty-cells/article.md",
        }
    ),
    "author acknowledgment": frozenset(
        {
            "ACKNOWLEDGMENTS.md",
            "CONTRIBUTING.md",
            "DATA_PROVENANCE.md",
            "DISCLAIMER.md",
            "docs/release/PUBLIC_RELEASE_CHECKLIST.md",
            "README.md",
            "contingency_atlas/web/about.yaml",
        }
    ),
}

RUNTIME_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-shm",
    ".db-wal",
    ".db-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite-journal",
}

# Binary artifacts individually reviewed and approved for public release.
# Review is bound to exact bytes: replacing a file at an approved path requires
# a new review and digest. Every entry must stay under MAX_RELEASE_FILE_BYTES;
# anything not listed fails closed.
REVIEWED_BINARY_SHA256 = {
    # Whitepaper figures reviewed 2026-08-05: each is a matplotlib chart of the
    # preregistered synthetic R0 dataset (baselines, PIT calibration, rare-pair
    # markdown, interval violations). Axis labels, titles and the one annotated
    # id (scn-04bc49946200) are synthetic; no institution, person or path
    # appears. Regenerate with docs/research/whitepaper/scripts/generate_figures.py.
    "docs/research/whitepaper/figures/figure1_taskb_baselines.png": "0806a7bdede596966c03608ca35cdf2cb92a6848b2ed3646b76b2069f10cf3c7",
    "docs/research/whitepaper/figures/figure2_pit_calibration.png": "05efa4e487324a570e0784a64b145b9d1062f71a33dc9af889a5e62af7cdf4dc",
    "docs/research/whitepaper/figures/figure3_rare_pair_markdown.png": "fcfd684ac4b772781daf12231cae5b6a37adb50767fb767f8b804a9d78b88610",
    "docs/research/whitepaper/figures/figure4_interval_violations.png": "6a247f265a3f69de987d3781dc823c81a446c8825a6343a8f2e6e006b42765f3",
    # Built manuscript reviewed 2026-08-29: no embedded local path or username,
    # document metadata empty (/Author, /Title, /Subject, /Keywords), producer
    # pdfTeX. The manuscript contains attribution but no personal contact data.
    "docs/research/whitepaper/manuscript/contingency_atlas_whitepaper.pdf": "dd4f314682630d07cbea8fa2472e0bdb8fc4db8deac668c27ec9fb47e93384f3",
    # Essay hero figure reviewed 2026-08-09: a hand-authored conceptual diagram
    # (checklist rows vs an axis matrix) built from the generic taxonomy labels
    # already in the essay. No institution, person, path or dataset appears;
    # the vector source sits beside it as checklist_vs_search.svg.
    "docs/research/essays/checklist-empty-cells/figures/checklist_vs_search.png": "0e164c5415c7162d4ab2653ebf6432249783171b977601711d3c4929351da0a2",
}
REVIEWED_BINARY_PATHS = frozenset(REVIEWED_BINARY_SHA256)
# Older commits store these same bytes at docs/whitepaper/ and docs/essays/.
# A historical blob is reviewed when its digest matches, even after the move.
REVIEWED_BINARY_DIGESTS = frozenset(REVIEWED_BINARY_SHA256.values())


def historical_binary_failure(path_label: str, data: bytes) -> str | None:
    """Return a finding for an unreviewed binary, or None when the bytes were reviewed."""
    digest = hashlib.sha256(data).hexdigest()
    if path_label in REVIEWED_BINARY_PATHS:
        if digest != REVIEWED_BINARY_SHA256[path_label]:
            return f"{path_label}: reviewed binary digest mismatch"
        return None
    if digest in REVIEWED_BINARY_DIGESTS:
        return None
    return f"{path_label}: binary historical blob"

# A cited URL is not a filesystem path. Only the two home-directory rules are
# exempted from URL text: they match bare substrings, so any link whose path
# happens to contain either platform's home prefix would otherwise be reported
# as a leaked local path (a businesswire.com news URL did exactly that). The
# credential-in-URL and email rules deliberately keep reading URLs.
_URL_EXEMPT_RULES = frozenset({
    "private macOS home path",
    "private Linux home path",
})
_URL_IN_TEXT = re.compile(r"https?://\S+")

# Per-rule, per-path exemptions. This remains explicit even when empty so any
# future exception requires a reviewable code change.
RULE_EXEMPTIONS: dict[str, set[str]] = {}

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_IDENTIFIER_SEPARATOR = re.compile(r"[_./\\:-]+")
_METADATA_RULE_EXCLUSIONS = frozenset({"email address"})
_GENERATED_PACKAGE_PATH = re.compile(
    r"(?:"
    r"[^/]+\.dist-info/(?:METADATA|WHEEL|RECORD|entry_points\.txt|"
    r"top_level\.txt|licenses/.+)"
    r"|[^/]+\.egg-info/(?:PKG-INFO|SOURCES\.txt|dependency_links\.txt|"
    r"entry_points\.txt|requires\.txt|top_level\.txt)"
    r"|PKG-INFO|setup\.cfg"
    r")\Z"
)
_DESCRIPTION_METADATA_PATH = re.compile(
    r"(?:[^/]+\.dist-info/METADATA|[^/]+\.egg-info/PKG-INFO|PKG-INFO)\Z"
)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def release_files() -> list[Path]:
    """Return present tracked and untracked-but-not-ignored candidates."""
    result = _git(
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    paths = [
        ROOT / item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    ]
    return [path for path in paths if path.exists() or path.is_symlink()]


def tracked_file_labels() -> frozenset[str]:
    """Return present Git-tracked paths, excluding deleted working-tree files."""
    result = _git("ls-files", "--cached", "-z")
    deleted = _git("ls-files", "--deleted", "-z")
    labels = {
        item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    }
    labels.difference_update(
        item.decode("utf-8")
        for item in deleted.stdout.split(b"\0")
        if item
    )
    return frozenset(labels)


def _decode_text(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _normalized_identifier_text(text: str) -> str:
    """Expose snake/path/camel boundaries to the phrase-oriented rules."""
    boundaries = _ACRONYM_BOUNDARY.sub(" ", text)
    boundaries = _CAMEL_BOUNDARY.sub(" ", boundaries)
    return _IDENTIFIER_SEPARATOR.sub(" ", boundaries)


def _first_match(
    pattern: re.Pattern[str],
    *candidates: str,
) -> tuple[str, re.Match[str]] | None:
    for candidate in candidates:
        match = pattern.search(candidate)
        if match:
            return candidate, match
    return None


def _scan_text(
    path_label: str,
    text: str,
    *,
    excluded_rules: frozenset[str] = frozenset(),
    exemption_path_label: str | None = None,
) -> list[str]:
    failures: list[str] = []
    exemption_path = exemption_path_label or path_label
    # Blank out http(s) URLs for the home-directory checks only, preserving
    # offsets so reported line numbers stay correct. Every other rule still
    # sees URLs, which is what catches a credential embedded in one.
    without_urls = _URL_IN_TEXT.sub(lambda m: " " * len(m.group(0)), text)
    normalized = _normalized_identifier_text(text)
    for label, pattern in GLOBAL_FORBIDDEN.items():
        if label in excluded_rules or exemption_path in RULE_EXEMPTIONS.get(label, ()):
            continue
        raw_haystack = without_urls if label in _URL_EXEMPT_RULES else text
        found = _first_match(pattern, raw_haystack, normalized)
        if found:
            haystack, match = found
            line = haystack.count("\n", 0, match.start()) + 1
            failures.append(f"{path_label}:{line}: {label}")
    for label, pattern in DISCLOSURE_ONLY.items():
        if label in excluded_rules or exemption_path in DISCLOSURE_EXEMPTIONS.get(
            label, ()
        ):
            continue
        found = _first_match(pattern, text, normalized)
        if found:
            haystack, match = found
            line = haystack.count("\n", 0, match.start()) + 1
            failures.append(
                f"{path_label}:{line}: {label} outside approved disclosure files"
            )
    for line_number, line_text in enumerate(text.splitlines(), start=1):
        if scan_identifier_residue(line_text):
            failures.append(
                f"{path_label}:{line_number}: legacy schema identifier"
            )
            break
    return failures


def _scan_path_name(path_label: str) -> list[str]:
    """Scan a normalized release path without inheriting content exemptions."""
    failures: list[str] = []
    for finding in _scan_text("<release-path>", path_label):
        reason = finding.split(": ", 1)[-1]
        failures.append(f"{path_label}: path name contains {reason}")
    return failures


def _scan_entry(
    path_label: str,
    data: bytes,
    *,
    binary_allowlist: frozenset[str] | set[str] = REVIEWED_BINARY_PATHS,
    exemption_path_label: str | None = None,
) -> list[str]:
    failures = _scan_path_name(path_label)
    lower_name = path_label.lower()
    if any(lower_name.endswith(suffix) for suffix in RUNTIME_SUFFIXES):
        failures.append(
            f"{path_label}: runtime database or sidecar must not be released"
        )
    if len(data) > MAX_RELEASE_FILE_BYTES:
        failures.append(
            f"{path_label}: exceeds {MAX_RELEASE_FILE_BYTES:,}-byte release limit"
        )
    text = _decode_text(data)
    if text is None:
        if path_label not in binary_allowlist:
            failures.append(
                f"{path_label}: binary release files require explicit review"
            )
        else:
            expected = REVIEWED_BINARY_SHA256.get(path_label)
            actual = hashlib.sha256(data).hexdigest()
            if expected is None or actual != expected:
                failures.append(
                    f"{path_label}: reviewed binary digest mismatch"
                )
        return failures
    failures.extend(
        _scan_text(
            path_label,
            text,
            exemption_path_label=exemption_path_label,
        )
    )
    return failures


def scan_worktree(files: Iterable[Path]) -> list[str]:
    failures: list[str] = []
    seen: set[str] = set()
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        seen.add(relative)
        if path.is_symlink():
            failures.append(f"{relative}: symbolic links are not permitted")
            continue
        if not path.is_file():
            failures.append(f"{relative}: release candidate is not a regular file")
            continue
        failures.extend(_scan_entry(relative, path.read_bytes()))

    for required in sorted(REQUIRED_FILES - seen):
        failures.append(f"{required}: required public-release file is missing")
    return failures


def scan_staging(staging_root: Path) -> list[str]:
    """Inspect a release tree and reject every non-tracked input file."""
    root = staging_root.resolve()
    if not root.is_dir():
        return [f"{staging_root}: staging path is not a directory"]
    tracked = tracked_file_labels()
    failures: list[str] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        seen.add(relative)
        if relative not in tracked:
            failures.append(f"{relative}: staging file is not Git-tracked")
        if path.is_symlink():
            failures.append(f"{relative}: symbolic links are not permitted")
            continue
        if not path.is_file():
            failures.append(f"{relative}: staging entry is not a regular file")
            continue
        failures.extend(_scan_entry(relative, path.read_bytes()))
    for required in sorted(REQUIRED_FILES - seen):
        failures.append(f"{required}: required staged release file is missing")
    return failures


def _safe_archive_name(name: str, prefix: str = "") -> str | None:
    if "\\" in name:
        return None
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    parts = list(pure.parts)
    if prefix and parts and parts[0] == prefix:
        parts.pop(0)
    if not parts:
        return ""
    return PurePosixPath(*parts).as_posix()


def _archive_prefix(names: Iterable[str]) -> str:
    roots = {
        PurePosixPath(name).parts[0]
        for name in names
        if PurePosixPath(name).parts
    }
    return next(iter(roots)) if len(roots) == 1 else ""


def _packaged_ontology_source(path_label: str) -> str | None:
    match = re.fullmatch(
        r"[^/]+\.data/data/share/contingency-atlas/(ontology/.+)",
        path_label,
    )
    return match.group(1) if match else None


def _package_member_is_allowed(path_label: str, tracked: frozenset[str]) -> bool:
    if path_label in tracked or bool(_GENERATED_PACKAGE_PATH.fullmatch(path_label)):
        return True
    source_label = _packaged_ontology_source(path_label)
    return bool(source_label and source_label in tracked)


def scan_release_artifact(artifact: Path) -> list[str]:
    """Inspect a wheel/sdist without extracting it to the working tree."""
    if not artifact.is_file():
        return [f"{artifact}: release artifact is not a regular file"]
    tracked = tracked_file_labels()
    failures: list[str] = []
    entries: list[tuple[str, bytes, bool]] = []
    try:
        if artifact.name.endswith((".whl", ".zip")):
            with zipfile.ZipFile(artifact) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_RELEASE_ARTIFACT_MEMBERS:
                    return [f"{artifact}: release artifact has too many members"]
                if sum(info.file_size for info in infos) > MAX_RELEASE_ARTIFACT_BYTES:
                    return [f"{artifact}: release artifact expands beyond its limit"]
                for info in infos:
                    if info.is_dir():
                        continue
                    if info.file_size > MAX_RELEASE_FILE_BYTES:
                        failures.append(
                            f"{info.filename}: exceeds "
                            f"{MAX_RELEASE_FILE_BYTES:,}-byte release limit"
                        )
                        continue
                    mode = (info.external_attr >> 16) & 0o170000
                    entries.append(
                        (info.filename, archive.read(info), mode == stat.S_IFLNK)
                    )
        elif artifact.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(artifact, mode="r:gz") as archive:
                members = archive.getmembers()
                if len(members) > MAX_RELEASE_ARTIFACT_MEMBERS:
                    return [f"{artifact}: release artifact has too many members"]
                if sum(member.size for member in members) > MAX_RELEASE_ARTIFACT_BYTES:
                    return [f"{artifact}: release artifact expands beyond its limit"]
                for member in members:
                    if member.isdir():
                        continue
                    if not member.isfile():
                        entries.append((member.name, b"", True))
                        continue
                    if member.size > MAX_RELEASE_FILE_BYTES:
                        failures.append(
                            f"{member.name}: exceeds "
                            f"{MAX_RELEASE_FILE_BYTES:,}-byte release limit"
                        )
                        continue
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        failures.append(f"{member.name}: archive member is unreadable")
                        continue
                    entries.append((member.name, extracted.read(), False))
        else:
            return [f"{artifact}: expected a .whl, .zip, .tar.gz, or .tgz file"]
    except (OSError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return [f"{artifact}: invalid release artifact: {exc}"]

    prefix = _archive_prefix(name for name, _, _ in entries)
    seen: set[str] = set()
    for raw_name, data, is_link in entries:
        path_label = _safe_archive_name(raw_name, prefix)
        if path_label is None:
            failures.append(f"{raw_name}: unsafe archive member path")
            continue
        if not path_label:
            continue
        if path_label in seen:
            failures.append(f"{path_label}: duplicate archive member")
            continue
        seen.add(path_label)
        if is_link:
            failures.append(f"{path_label}: archive links are not permitted")
            continue
        if not _package_member_is_allowed(path_label, tracked):
            failures.append(
                f"{path_label}: package member is not tracked or standard metadata"
            )
        ontology_source = _packaged_ontology_source(path_label)
        if ontology_source and ontology_source in tracked:
            source_path = ROOT / ontology_source
            if not source_path.is_file() or source_path.read_bytes() != data:
                failures.append(
                    f"{path_label}: packaged ontology member differs from "
                    f"tracked source {ontology_source}"
                )
        # Core Metadata embeds the README as the package long description.
        # Reuse only README's rule-specific disclosure permissions while
        # retaining the metadata member's own path in findings. No global
        # rule is suppressed, and unrelated generated files inherit nothing.
        exemption_path = (
            "README.md" if _DESCRIPTION_METADATA_PATH.fullmatch(path_label) else None
        )
        failures.extend(
            _scan_entry(
                path_label,
                data,
                exemption_path_label=exemption_path,
            )
        )
    if not seen:
        failures.append(f"{artifact}: release artifact contains no files")
    return failures


def _load_json(relative: str) -> list[dict[str, object]]:
    value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{relative} must contain a JSON array of objects")
    return value


def validate_fixtures() -> list[str]:
    failures: list[str] = []
    try:
        from contingency_atlas.categories import (
            derive_financial_impact_band,
            derive_impact_band,
        )

        evidence = _load_json("examples/demo_evidence.json")
        evidence_ids = {
            str(item.get("event_id")) for item in evidence if item.get("event_id")
        }
        required_evidence = {
            "event_id",
            "headline",
            "organization",
            "source_date",
            "source_url",
            "source_publisher",
            "source_title",
            "retrieved_at",
            "source_type",
            "record_type",
            "provenance_class",
            "source_locator",
            "independently_authored_summary",
            "research_inference",
            "rights_review",
            "content_fingerprint",
            "review_status",
            "reviewed_at",
            "taxonomy_version",
            "provenance_note",
        }
        for index, event in enumerate(evidence):
            missing = required_evidence - set(event)
            if missing:
                failures.append(
                    f"examples/demo_evidence.json[{index}]: missing {sorted(missing)}"
                )
            if event.get("source_type") != "synthetic":
                failures.append(
                    f"examples/demo_evidence.json[{index}]: "
                    "source_type must be synthetic"
                )
            if event.get("provenance_class") != "synthetic":
                failures.append(
                    f"examples/demo_evidence.json[{index}]: "
                    "provenance_class must be synthetic"
                )
            if event.get("review_status") != "approved_synthetic":
                failures.append(
                    f"examples/demo_evidence.json[{index}]: "
                    "review_status must be approved_synthetic"
                )
            if event.get("source_url") is not None:
                failures.append(
                    f"examples/demo_evidence.json[{index}]: "
                    "synthetic source_url must be null"
                )
            if not str(event.get("event_id", "")).startswith("DEMO-EVT-"):
                failures.append(
                    f"examples/demo_evidence.json[{index}]: invalid synthetic event_id"
                )
            note = str(event.get("provenance_note", "")).lower()
            if "fictional" not in note:
                failures.append(
                    f"examples/demo_evidence.json[{index}]: "
                    "provenance must say fictional"
                )

        emerging = _load_json("examples/demo_emerging_risks.json")
        required_radar_risk = {
            "external_drivers",
            "signal_maturity",
            "momentum",
            "evidence_strength",
            "first_observed",
            "last_confirmed",
        }
        for index, risk in enumerate(emerging):
            missing = required_radar_risk - set(risk)
            if missing:
                failures.append(
                    f"examples/demo_emerging_risks.json[{index}]: "
                    f"missing radar fields {sorted(missing)}"
                )
            if risk.get("source_type") != "synthetic":
                failures.append(
                    f"examples/demo_emerging_risks.json[{index}]: "
                    "source_type must be synthetic"
                )
            if risk.get("review_status") != "approved_synthetic":
                failures.append(
                    f"examples/demo_emerging_risks.json[{index}]: "
                    "review_status must be approved_synthetic"
                )
            if "fictional" not in str(risk.get("provenance_note", "")).lower():
                failures.append(
                    f"examples/demo_emerging_risks.json[{index}]: "
                    "provenance must say fictional"
                )
            refs = {str(ref) for ref in (risk.get("evidence_refs") or [])}
            if not refs or not refs.issubset(evidence_ids):
                failures.append(
                    f"examples/demo_emerging_risks.json[{index}]: "
                    "evidence_refs must resolve to demo evidence"
                )

        scenarios = _load_json("examples/demo_scenarios.json")
        required_radar_scenario = {
            "origin",
            "external_drivers",
            "signal_maturity",
            "momentum",
            "evidence_strength",
            "affected_capabilities",
            "transmission_mechanism",
            "second_order_effects",
            "cross_industry_analogues",
            "strengthening_signposts",
            "weakening_signposts",
            "defensive_options",
            "resilience_opportunities",
            "strategic_options",
            "option_expiry_signals",
            "challenged_assumptions",
            "discussion_prompts",
            "signpost_status",
            "signpost_review_status",
            "signpost_assessment",
            "signpost_checked_evidence_refs",
            "signpost_evidence_refs",
            "signposts_last_evaluated",
        }
        for index, scenario in enumerate(scenarios):
            missing = required_radar_scenario - set(scenario)
            if missing:
                failures.append(
                    f"examples/demo_scenarios.json[{index}]: "
                    f"missing radar fields {sorted(missing)}"
                )
            if not str(scenario.get("scenario_id", "")).startswith("ATLAS-DEMO-"):
                failures.append(
                    f"examples/demo_scenarios.json[{index}]: "
                    "invalid synthetic scenario_id"
                )
            if scenario.get("origin") != "synthetic_fixture":
                failures.append(
                    f"examples/demo_scenarios.json[{index}]: "
                    "origin must be synthetic_fixture"
                )
            disclosure = str(scenario.get("additional_information", "")).lower()
            if "fictional" not in disclosure or "not a prediction" not in disclosure:
                failures.append(
                    f"examples/demo_scenarios.json[{index}]: "
                    "must disclose fictional, non-predictive status"
                )
            refs = {str(ref) for ref in (scenario.get("evidence_refs") or [])}
            if not refs or not refs.issubset(evidence_ids):
                failures.append(
                    f"examples/demo_scenarios.json[{index}]: "
                    "evidence_refs must resolve to demo evidence"
                )
            for field_name in ("scenario_event_name", "scenario_horizon"):
                if not scenario.get(field_name):
                    failures.append(
                        f"examples/demo_scenarios.json[{index}]: "
                        f"{field_name} is required"
                    )
            expected_financial = derive_financial_impact_band(
                str(scenario.get("financial_impact_detail") or "")
            )
            if scenario.get("financial_impact_band") != expected_financial:
                failures.append(
                    f"examples/demo_scenarios.json[{index}]: "
                    f"financial_impact_band must be {expected_financial}"
                )
            expected_impact = derive_impact_band(
                str(scenario.get("likelihood") or ""),
                str(scenario.get("financial_impact_detail") or ""),
            )
            if scenario.get("impact_band") != expected_impact:
                failures.append(
                    f"examples/demo_scenarios.json[{index}]: "
                    f"impact_band must be {expected_impact}"
                )

        manifest = json.loads(
            (ROOT / "examples" / "fixture_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(manifest, dict):
            failures.append("examples/fixture_manifest.json: must be an object")
        else:
            if manifest.get("provenance_class") != "synthetic":
                failures.append(
                    "examples/fixture_manifest.json: provenance_class must be synthetic"
                )
            if manifest.get("review_status") != "approved_synthetic":
                failures.append(
                    "examples/fixture_manifest.json: "
                    "review_status must be approved_synthetic"
                )
            if manifest.get("license") != "CC0-1.0":
                failures.append(
                    "examples/fixture_manifest.json: license must be CC0-1.0"
                )
            if manifest.get("license_file") != "LICENSES/CC0-1.0.txt":
                failures.append(
                    "examples/fixture_manifest.json: invalid license_file"
                )
            if "fictional" not in str(manifest.get("synthetic_notice", "")).lower():
                failures.append(
                    "examples/fixture_manifest.json: "
                    "synthetic_notice must say fictional"
                )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"fixture validation failed: {exc}")
    return failures


def validate_prompts() -> list[str]:
    """Dynamically discover prompt constants and enforce residue/version rules."""
    try:
        from contingency_atlas.prompt_registry import inspect_prompt_registry

        return inspect_prompt_registry()
    except (ImportError, OSError, SyntaxError, ValueError) as exc:
        return [f"prompt registry inspection failed: {exc}"]


def scan_history() -> list[str]:
    """Scan reachable paths, blobs, commits, refs, and annotated tag metadata."""
    if _git("rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        return ["Git history scan requested, but the repository has no commit"]

    failures: list[str] = []
    commit_ids = _git("rev-list", "--all").stdout.decode(
        "ascii", errors="replace"
    ).splitlines()
    for object_id in commit_ids:
        data = _git("cat-file", "commit", object_id).stdout
        text = _decode_text(data)
        if text is None:
            failures.append(
                f"history:commit:{object_id[:12]}: unreadable commit metadata"
            )
            continue
        failures.extend(
            f"history:commit:{object_id[:12]}:{item}"
            for item in _scan_text(
                "<commit-metadata>",
                text,
                excluded_rules=_METADATA_RULE_EXCLUSIONS,
            )
        )

    ref_rows = _git(
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(objecttype)",
    ).stdout.decode("utf-8", errors="replace").splitlines()
    scanned_tags: set[str] = set()
    for ref_row in ref_rows:
        ref_name, _, remainder = ref_row.partition("\0")
        object_id, _, object_type = remainder.partition("\0")
        failures.extend(
            f"history:ref:{item}" for item in _scan_path_name(ref_name)
        )
        if object_type != "tag" or not object_id or object_id in scanned_tags:
            continue
        scanned_tags.add(object_id)
        data = _git("cat-file", "tag", object_id).stdout
        text = _decode_text(data)
        if text is None:
            failures.append(
                f"history:tag:{object_id[:12]}: unreadable tag metadata"
            )
            continue
        failures.extend(
            f"history:tag:{object_id[:12]}:{item}"
            for item in _scan_text(
                "<tag-metadata>",
                text,
                excluded_rules=_METADATA_RULE_EXCLUSIONS,
            )
        )

    result = _git("rev-list", "--objects", "--all")
    for raw_line in result.stdout.decode("utf-8", errors="replace").splitlines():
        object_id, _, path_label = raw_line.partition(" ")
        object_type = _git("cat-file", "-t", object_id).stdout.strip()
        if object_type == b"tag":
            if object_id in scanned_tags:
                continue
            data = _git("cat-file", "tag", object_id).stdout
            text = _decode_text(data)
            if text is None:
                failures.append(
                    f"history:tag:{object_id[:12]}: unreadable tag metadata"
                )
                continue
            failures.extend(
                f"history:tag:{object_id[:12]}:{item}"
                for item in _scan_text(
                    "<tag-metadata>",
                    text,
                    excluded_rules=_METADATA_RULE_EXCLUSIONS,
                )
            )
            continue
        if object_type != b"blob" or not path_label:
            continue
        failures.extend(
            f"history:{object_id[:12]}:{item}"
            for item in _scan_path_name(path_label)
        )
        size = int(_git("cat-file", "-s", object_id).stdout)
        if size > MAX_RELEASE_FILE_BYTES:
            failures.append(
                f"history:{object_id[:12]}:{path_label}: oversized historical blob"
            )
            continue
        data = _git("cat-file", "blob", object_id).stdout
        text = _decode_text(data)
        if text is None:
            failure = historical_binary_failure(path_label, data)
            if failure:
                failures.append(f"history:{object_id[:12]}:{failure}")
            continue
        failures.extend(
            f"history:{object_id[:12]}:{item}"
            for item in _scan_text(path_label, text)
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Contingency Atlas public-release boundaries."
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="also inspect every blob reachable from local Git history",
    )
    parser.add_argument(
        "--staging",
        type=Path,
        help="also inspect a tracked-files-only release staging directory",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        type=Path,
        help="also inspect a built wheel or source archive (repeatable)",
    )
    args = parser.parse_args()

    failures = scan_worktree(release_files())
    failures.extend(validate_fixtures())
    failures.extend(validate_prompts())
    if args.staging is not None:
        failures.extend(scan_staging(args.staging))
    for artifact in args.artifact:
        failures.extend(scan_release_artifact(artifact))
    if args.history:
        failures.extend(scan_history())

    if failures:
        print("PUBLIC RELEASE GATE: FAILED", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    history_note = " and reachable history" if args.history else ""
    print(f"PUBLIC RELEASE GATE: PASSED (worktree{history_note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
