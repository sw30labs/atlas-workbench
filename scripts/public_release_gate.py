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
from workbench_atlas.independence_lexicon import (
    DISCLOSURE_ONLY,
    scan_identifier_residue,
)
from workbench_atlas.independence_lexicon import (
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
    "workbench_atlas/source_registry.yaml",
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
            "workbench_atlas/web/about.yaml",
            "docs/research/essays/checklist-empty-cells/article.md",
            "docs/deck/index.html",
            "docs/REFERENCE_README.md",
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
            "workbench_atlas/web/about.yaml",
            "docs/deck/index.html",
            "docs/REFERENCE_README.md",
        }
    ),
}
# The same disclosure sentences lived at these paths before the docs shelf
# and the package rename. The files are gone; history still has the blobs.
HISTORICAL_DISCLOSURE_PATHS: dict[str, frozenset[str]] = {
    "external industry resource": frozenset(
        {
            "PUBLIC_RELEASE_CHECKLIST.md",
            "contingency_atlas/web/about.yaml",
            "docs/essays/checklist-empty-cells/article.md",
        }
    ),
    "author acknowledgment": frozenset(
        {
            "PUBLIC_RELEASE_CHECKLIST.md",
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
    # Rebuilt 2026-11-21 from the manuscript. No embedded local path or
    # username, document metadata empty (/Author, /Title, /Subject, /Keywords),
    # producer pdfTeX. The manuscript contains attribution but no personal
    # contact data.
    "docs/research/whitepaper/manuscript/workbench_atlas_whitepaper.pdf": "aa815b158d0556ede80e1d1ba4c9252d2bfd918a05c5206bd19ba7df70cca158",
    # Essay hero figure reviewed 2026-08-09: a hand-authored conceptual diagram
    # (checklist rows vs an axis matrix) built from the generic taxonomy labels
    # already in the essay. No institution, person, path or dataset appears;
    # the vector source sits beside it as checklist_vs_search.svg.
    "docs/research/essays/checklist-empty-cells/figures/checklist_vs_search.png": "0e164c5415c7162d4ab2653ebf6432249783171b977601711d3c4929351da0a2",
    # Deck frames reviewed 2026-11-21: loopback screenshots of the fictional
    # demo from a clean install. The header reads ATLAS WORKBENCH.
    # 14c-draft-open.png is a completed intake of a public BIS page that
    # returned no candidates. Send stayed disabled. Nothing was sent,
    # approved, or saved.
    "docs/deck/frames/01-briefing.png": "f25ba45729e37cf792c7717aed642f0bb5e3900ef5e519e4cff765b23d16e881",
    "docs/deck/frames/02-constellation.png": "a781eb2504370926e63a8f9998cd8dba123b72de000c3fcfd71a3c77d919a15b",
    "docs/deck/frames/05-library.png": "0d4308d00d15b0415c5edb7ee2516df27295f9c8899be39dfbe226872260e859",
    "docs/deck/frames/06-brief.png": "e6c77a962de56156edab642fefd302b2ccdc1761938e1045a3020c262dc5bdd4",
    "docs/deck/frames/08-signals.png": "f343c6de41490cea8c154930dc7333d2460fdb33e3f4059925ce0250b2356dc3",
    "docs/deck/frames/10-review.png": "1bd7ee11c4e307327fe82a07c880fb1f461b67c1d9f49c4f25c571bab35c515a",
    "docs/deck/frames/11-analyze.png": "d125f36e085dabf01b9734dc528150bfedbf120fe1d193185a72ef45e3d4788d",
    "docs/deck/frames/12-organization.png": "2b20d29a354ea39d1481a9efe65fa5052ae14ee4d56434e83ef363dacf3b912f",
    "docs/deck/frames/14-add-research.png": "150563569f564ac9a8f46187ba1fe4e99664a092b2d9289dc7e101b3f2a4a6c7",
    "docs/deck/frames/14b-preparing.png": "c59d93f3c520d658901c2ee255a4960a33009bd0da349a8221a31a6f7c32b934",
    "docs/deck/frames/14c-draft-open.png": "e6b4e307954f6125eaa9928aef97a27c4d4e56ca83d7fdb8df5c4568d38e0fbc",
    "docs/deck/frames/15b-export-preview.png": "393eade804ec15d515635898c178559cf2f24097a3be84ec1b9e593cca5e1728",
    # Synthetic ACME Bank Office pack, generator acme-pack-1, seed 20260930.
    # Reviewed 2026-11-21: fictional institution, .example contacts, and the
    # deliberate traps (lock file, byte-identical duplicate, corrupt workbook,
    # canteen menu, fictional staff list). No real party is alleged.
    "examples/acme-bank/pack/01_operational_resilience/BIA_Retail_Payments_v3.1.xlsx": "1726ec3e890d692d196bb757fbd4f290155e69e66ba51e718cbbd1a8a36e99c4",
    "examples/acme-bank/pack/01_operational_resilience/BIA_Retail_Payments_v3.2.xlsx": "cdb7f2096bf9100732102dbe9c2d492a392c0a6bfac7ec2e818e65dff97676c3",
    "examples/acme-bank/pack/01_operational_resilience/IBS_register_FY26.xlsx": "5e9f8d196177d20ffce2621960d6eebbc5e699c9efe08be43b814fb2e8605671",
    "examples/acme-bank/pack/01_operational_resilience/impact_tolerance_statements.docx": "9816dd95d6db6dbb0e06508f8cdf1c1e4e45472b14e4085165e53ac5ea1c32d7",
    "examples/acme-bank/pack/01_operational_resilience/self_assessment_2026_summary.pdf": "944924c2beed812b3e59d07169c042a61a0c39152e6d8d8e2060b5b3687ebc52",
    "examples/acme-bank/pack/01_operational_resilience/~$IBS_register_FY26.xlsx": "2ad9f1b4eb68f0f26fc2f921d4ccc42c4be8c653301f0783b678d8c2eb6c991e",
    "examples/acme-bank/pack/02_scenarios_and_taxonomy/ACME_Scenario_Register_2025.xlsx": "c14ee492df885f304210a3458d682d3cc69454aef8285d0a414ba0f1fe322c72",
    "examples/acme-bank/pack/02_scenarios_and_taxonomy/NFR_Taxonomy_v4.xlsx": "0d0a266f996a91043bbdab5dd673c7dc48930788fb2e564d3f7dbb1b3e694fde",
    "examples/acme-bank/pack/02_scenarios_and_taxonomy/board_risk_committee_scenarios_Q2.pptx": "66ad03f3726ade438c8c65c8fc1efedb91eb3aa301ff8bb4610de92ea405f565",
    "examples/acme-bank/pack/02_scenarios_and_taxonomy/scenario_workshop_minutes_2025-11-12.docx": "62520efcf5e744dbc278b1983e09351116e6a52f9f280f9d3450af25af09d644",
    "examples/acme-bank/pack/03_internal_loss_data/PIR_2025-INC-0442_card_auth_outage.docx": "b1216e2a0994c2e671968b0d38d46038c47a48ab0d97122cc3429bdc5cbcdfec",
    "examples/acme-bank/pack/03_internal_loss_data/near_miss_log_2026.xlsx": "423e16c76d789fe726fbb031ea85a05ad4b3fcf385e2c4bab048b4daee3c7209",
    "examples/acme-bank/pack/04_external_loss_watchlist/external_loss_watchlist_2026Q3.xlsx": "6385dc8bc3c30314ace05ea5cb0d2f66d52fa1c48871d1e19f9f8f0be746a4bc",
    "examples/acme-bank/pack/05_controls_and_testing/internal_audit_finding_IA-2026-031.pdf": "470ae9fde4a94d61407a06378915a25f6ad762c28852ced930d384c867ebada6",
    "examples/acme-bank/pack/05_controls_and_testing/key_controls_library.xlsx": "68fa5c1ef3ba84852ae2523236afd5776764a728870a6eedd85a2a6cd626bfa8",
    "examples/acme-bank/pack/05_controls_and_testing/resilience_exercise_2026_cyber_tabletop.docx": "41e16228f78b6f674996a5a9f831dbfa3c031adbf447fb30f35e856ec2ddc25d",
    "examples/acme-bank/pack/06_prior_reports/ICAAP_2025_oprisk_scenario_chapter.pdf": "8c5ae66517fd34efc36d3f04ef2ed3d4a5319ca68c84f071a79f351ad2fde2b1",
    "examples/acme-bank/pack/06_prior_reports/annual_oprisk_report_2025.docx": "80234f831390af7d71f557d284e91f85f09036e23d17aa194336bf026139243f",
    "examples/acme-bank/pack/07_misc/BIA_Retail_Payments_v3.2 (copy).xlsx": "cdb7f2096bf9100732102dbe9c2d492a392c0a6bfac7ec2e818e65dff97676c3",
    "examples/acme-bank/pack/07_misc/canteen_menu_october.docx": "77e184a61134ea2af608a16a91818ff64cb73dcc6a17cb56c1c4e1525ff2926b",
    "examples/acme-bank/pack/07_misc/corrupted_export.xlsx": "dda1310bcd6a3b5c517985ae4aa9acf2748c7b62c3002bbc5bf1412cfcbabd86",
    "examples/acme-bank/pack/07_misc/team_contact_list.xlsx": "cc072d075cb2b5b7c09c0922f27b145b6bee222f2f928ff787d0bf0e192fbfb3",
}
REVIEWED_BINARY_PATHS = frozenset(REVIEWED_BINARY_SHA256)
# Older commits store these same figure bytes at docs/whitepaper/ and
# docs/essays/. The 2026-08-29 manuscript digest stays valid after the rebuild.
# A historical blob is reviewed when its digest matches, even after the move.
_HISTORICAL_REVIEWED_DIGESTS = frozenset({
    # Whitepaper PDF before the 2026-11-21 rebuild.
    "dd4f314682630d07cbea8fa2472e0bdb8fc4db8deac668c27ec9fb47e93384f3",
    # Deck frames before the 2026-11-21 rename. Same fictional demo.
    "342eee701e8a26d5a715f81b58014d1a04988d333c47f6b3013b1e60529b8c4d",
    "a60106f6f0c59938d4a7d61990ada207dfb31117c732f43057f15dcdf91b39cb",
    "290a94a755bfb09ab5445d7f615a740fe10cd4a133d0bf6f333d0e1d55395971",
    "636a28bf7121113ece5ca2bf1ff8c7b60706e8d1d1a0404bf9819a169284dc05",
    "82eb7cd36576d4ae2c3ad55b13109af42aee08e6d26d00759029e035adfdf696",
    "6539ca433a43751f4e04780dbd5a8f4458575ed1926ee6c6202141053918f4d0",
    "ac2c49bf8f1721fdb93b80a31bfde6fdeb5ece4924cc5ad0c97ded1abd3a09be",
    "6fae289f2f00096fa92ad25ce1c40487973dee8288fcb9eae54578eec2a3fabe",
    "54c6b20903dbfd3b7706c55139d5aa1b29f5604e0fb79bc1a8f8b35b9a977daf",
    "1df09e16937be6214d6d84a3138c2def270e373644de3298a4c78e77f2937fe1",
    "e209ca49d003cb5b3093d68a66d7a07b8f6d8193b99a38be4020e0f097c9e605",
    "201935ccc04e8cae088204ea448486e0b87c016208f8c51b903855068ca4ea3d",
})
REVIEWED_BINARY_DIGESTS = (
    frozenset(REVIEWED_BINARY_SHA256.values()) | _HISTORICAL_REVIEWED_DIGESTS
)


def historical_binary_failure(path_label: str, data: bytes) -> str | None:
    """Return a finding for an unreviewed binary, or None when the bytes were reviewed."""
    digest = hashlib.sha256(data).hexdigest()
    # A replaced file keeps its path. The previous bytes stay reviewed when
    # their digest was recorded, including the rebuilt whitepaper and the
    # deck frames from before the rename.
    if digest in _HISTORICAL_REVIEWED_DIGESTS:
        return None
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
# The old quick start wrote this placeholder. It is not a person's home.
_PLACEHOLDER_MACOS_HOME = "/" + "Users" + "/" + "you" + "/"
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


def _disclosure_path_allowed(label: str, path: str) -> bool:
    """Current disclosure files and the paths those sentences used to have."""
    return path in DISCLOSURE_EXEMPTIONS.get(
        label, ()
    ) or path in HISTORICAL_DISCLOSURE_PATHS.get(label, ())


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
    without_urls = without_urls.replace(
        _PLACEHOLDER_MACOS_HOME, " " * len(_PLACEHOLDER_MACOS_HOME)
    )
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
        if label in excluded_rules or _disclosure_path_allowed(label, exemption_path):
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
        r"[^/]+\.data/data/share/workbench-atlas/(ontology/.+)",
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
        from workbench_atlas.categories import (
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
        from workbench_atlas.prompt_registry import inspect_prompt_registry

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
        description="Validate Atlas Workbench public-release boundaries."
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
