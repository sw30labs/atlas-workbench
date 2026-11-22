"""Scanner behaviour for the public-release gate.

The gate fails closed, so a false positive is not harmless: it trains the
operator to read a FAILED verdict as noise, which is how a real leak ships.
"""

from __future__ import annotations

import io
import hashlib
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import public_release_gate as gate  # noqa: E402


def _joined(*parts: str) -> str:
    """Build audit terms so this file does not trip the gate it tests."""
    return "".join(parts)


HOME = _joined("/", "home", "/")
USERS = _joined("/", "Users", "/")
AT = _joined("@")


def _labels(text: str) -> set[str]:
    return {finding.split(": ", 1)[1] for finding in gate._scan_text("f.md", text)}


def test_cited_url_is_not_a_local_path():
    """A link whose path contains the home directory of either platform is not
    a leaked path — a businesswire.com news URL failed the gate this way.
    """
    news_url = f"https://www.businesswire.com/news{HOME}20250916892124/en/"
    assert "private Linux home path" not in _labels(f"([Business Wire]({news_url}))")
    assert "private macOS home path" not in _labels(
        f"see https://example.org{USERS}guide for details"
    )


@pytest.mark.parametrize(
    ("text", "label"),
    [
        (f"deployed from {HOME}deploy/atlas", "private Linux home path"),
        (f"ran in {USERS}someone/REPOS/x", "private macOS home path"),
    ],
)
def test_real_home_paths_are_still_caught(text, label):
    assert label in _labels(text)


def test_a_path_leak_beside_a_url_is_still_caught():
    """Blanking URLs must not blank the rest of the line."""
    text = f"see https://x.example.org/news{HOME}1 and also {HOME}deploy/secret"
    assert "private Linux home path" in _labels(text)


def test_credentials_in_urls_are_still_read():
    """Only the home-path rules ignore URL text; this rule needs it."""
    assert "credential in URL" in _labels(
        f"https://user:token{AT}example.org/x"
    )


def test_personal_contact_is_not_exempted_from_manuscript():
    """Public attribution does not create a blanket personal-contact exception."""
    manuscript = "docs/research/whitepaper/manuscript/workbench_atlas_whitepaper.tex"
    address = f"someone{AT}example.org"
    assert [
        f for f in gate._scan_text(manuscript, address) if "email address" in f
    ]
    assert [
        finding
        for finding in gate._scan_text("docs/other.md", address)
        if "email address" in finding
    ]
    # An exemption is per rule, not a blanket pass for the file.
    assert [
        f
        for f in gate._scan_text(manuscript, f"built in {USERS}someone/x")
        if "private macOS home path" in f
    ]


def test_disclosure_exemptions_are_per_rule_and_path():
    industry_term = _joined("O", "RX")
    employer_term = _joined("U", "BS")
    relative = "README.md"
    assert not gate._scan_text(relative, industry_term)
    assert gate._scan_text(relative, employer_term)
    assert gate._scan_text("docs/some-other-file.md", industry_term)


def test_every_reviewed_binary_still_exists():
    """A stale path or digest must invalidate the reviewed-binary set."""
    for relative in gate.REVIEWED_BINARY_PATHS:
        path = ROOT / relative
        assert path.is_file(), f"{relative} is allowlisted but missing"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            gate.REVIEWED_BINARY_SHA256[relative]
        ), f"{relative} changed after binary review"


def test_reviewed_bytes_remain_valid_at_their_old_path():
    """The docs shelf moved reviewed figures. History still has the old paths."""
    current = "docs/research/whitepaper/figures/figure1_taskb_baselines.png"
    former = "docs/whitepaper/figures/figure1_taskb_baselines.png"
    data = (ROOT / current).read_bytes()
    assert gate.historical_binary_failure(current, data) is None
    assert gate.historical_binary_failure(former, data) is None
    mutated = data + b"one-byte-review-invalidated"
    assert gate.historical_binary_failure(former, mutated) == (
        f"{former}: binary historical blob"
    )
    assert "reviewed binary digest mismatch" in (
        gate.historical_binary_failure(current, mutated) or ""
    )


def test_reviewed_binary_path_does_not_allow_mutated_bytes():
    relative = "docs/research/whitepaper/figures/figure1_taskb_baselines.png"
    mutated = (ROOT / relative).read_bytes() + b"one-byte-review-invalidated"
    findings = gate._scan_entry(relative, mutated)
    assert any("reviewed binary digest mismatch" in item for item in findings)


def test_every_rule_exemption_points_at_a_real_file():
    for label, paths in gate.RULE_EXEMPTIONS.items():
        assert label in gate.GLOBAL_FORBIDDEN, f"{label} exempts an unknown rule"
        for relative in paths:
            assert (ROOT / relative).is_file(), f"{relative} is exempt but missing"


def test_every_disclosure_exemption_is_rule_specific_and_used():
    for label, paths in gate.DISCLOSURE_EXEMPTIONS.items():
        assert label in gate.DISCLOSURE_ONLY, f"{label} is not a disclosure rule"
        pattern = gate.DISCLOSURE_ONLY[label]
        for relative in paths:
            path = ROOT / relative
            assert path.is_file(), f"{relative} is exempt but missing"
            assert pattern.search(path.read_text(encoding="utf-8")), (
                f"{relative} has a stale {label} exemption"
            )


def test_planted_schema_forbidden_column_fails():
    """A synthetic file that reintroduces a consortium column name fails closed."""
    column = _joined("Story ", "Reference Number")
    findings = gate._scan_text("examples/planted.json", f'{{"{column}": "x"}}')
    assert any("legacy event identifier" in item for item in findings)


def test_planted_prompt_alias_key_fails():
    """A retired inventory substitution key is residue, not a reader alias."""
    from workbench_atlas.independence_lexicon import scan_prompt_residue

    planted = "{" + _joined("cm", "db", "_info_", "CM", "DBID") + "}"
    hits = scan_prompt_residue(planted)
    assert "leftover inventory alias key" in hits


def test_canonical_prompt_json_is_not_an_alias_hit():
    from workbench_atlas.independence_lexicon import scan_prompt_residue

    assert not scan_prompt_residue(
        '{"event_driver_class": "x", "primary_cause_class": "y"}'
    )


def test_gate_terms_come_from_the_lexicon():
    from workbench_atlas.independence_lexicon import (
        DISCLOSURE_ONLY,
        STRING_FORBIDDEN,
    )

    assert gate.GLOBAL_FORBIDDEN is STRING_FORBIDDEN
    assert gate.DISCLOSURE_ONLY is DISCLOSURE_ONLY


def test_normalized_path_and_embedded_identifier_fail_closed():
    planted = _joined("Story", "Reference", "Number")
    findings = gate._scan_path_name(f"exports/archive{planted}V2.json")
    assert any("legacy event identifier" in item for item in findings)
    assert any("legacy schema identifier" in item for item in findings)


def test_snake_case_and_embedded_content_identifiers_fail_closed():
    snake = _joined("Story", "_Reference_", "Number")
    embedded = _joined("archive", "CM", "DB", "RecordV2")
    embedded_disclosure = _joined("U", "BS", "Internal")

    snake_findings = gate._scan_text("examples/planted.json", f'{{"{snake}": 1}}')
    embedded_findings = gate._scan_text(
        "examples/planted.json",
        f'{{"key": "{embedded}"}}',
    )
    disclosure_findings = gate._scan_text(
        "examples/planted.json",
        f'{{"key": "{embedded_disclosure}"}}',
    )

    assert any("legacy event identifier" in item for item in snake_findings)
    assert any("legacy schema identifier" in item for item in embedded_findings)
    assert any("employer disclosure" in item for item in disclosure_findings)


def test_staging_rejects_untracked_files(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("safe release", encoding="utf-8")
    (tmp_path / "runtime.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gate, "tracked_file_labels", lambda: frozenset({"README.md"}))
    monkeypatch.setattr(gate, "REQUIRED_FILES", {"README.md"})

    findings = gate.scan_staging(tmp_path)

    assert any(
        "runtime.json: staging file is not Git-tracked" in item
        for item in findings
    )


def test_wheel_inspection_rejects_nontracked_payload(tmp_path, monkeypatch):
    artifact = tmp_path / "package-1-py3-none-any.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("workbench_atlas/__init__.py", "__version__ = '1'")
        archive.writestr("package-1.dist-info/METADATA", "Name: package\n")
        archive.writestr("private-export.json", "{}")
    monkeypatch.setattr(
        gate,
        "tracked_file_labels",
        lambda: frozenset({"workbench_atlas/__init__.py"}),
    )

    findings = gate.scan_release_artifact(artifact)

    assert any(
        "private-export.json: package member is not tracked" in item
        for item in findings
    )
    assert not any("dist-info/METADATA: package member" in item for item in findings)


def test_wheel_inspection_maps_installed_ontology_data_to_tracked_source(
    tmp_path,
    monkeypatch,
):
    artifact = tmp_path / "package-1-py3-none-any.whl"
    member = "package-1.data/data/share/workbench-atlas/ontology/guard.py"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(member, (ROOT / "ontology" / "guard.py").read_bytes())
        archive.writestr("package-1.dist-info/METADATA", "Name: package\n")
    monkeypatch.setattr(
        gate,
        "tracked_file_labels",
        lambda: frozenset({"ontology/guard.py"}),
    )

    findings = gate.scan_release_artifact(artifact)

    assert not any("package member is not tracked" in item for item in findings)
    assert not any("differs from tracked source" in item for item in findings)


def test_wheel_inspection_rejects_modified_ontology_data_file(
    tmp_path,
    monkeypatch,
):
    artifact = tmp_path / "package-1-py3-none-any.whl"
    member = "package-1.data/data/share/workbench-atlas/ontology/guard.py"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(member, "# modified after packaging\n")
        archive.writestr("package-1.dist-info/METADATA", "Name: package\n")
    monkeypatch.setattr(
        gate,
        "tracked_file_labels",
        lambda: frozenset({"ontology/guard.py"}),
    )

    findings = gate.scan_release_artifact(artifact)

    assert any("differs from tracked source" in item for item in findings)


def test_package_description_inherits_only_readme_disclosure_permissions(
    tmp_path,
    monkeypatch,
):
    artifact = tmp_path / "package-1-py3-none-any.whl"
    industry_term = _joined("O", "RX")
    employer_term = _joined("U", "BS")
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("package/__init__.py", "")
        archive.writestr("package-1.dist-info/METADATA", industry_term)
        archive.writestr("package-1.dist-info/WHEEL", employer_term)
    monkeypatch.setattr(
        gate,
        "tracked_file_labels",
        lambda: frozenset({"package/__init__.py"}),
    )

    findings = gate.scan_release_artifact(artifact)

    assert not any(
        "METADATA" in item and "external industry resource" in item
        for item in findings
    )
    assert any(
        "WHEEL" in item and "employer disclosure" in item
        for item in findings
    )


def test_sdist_inspection_normalizes_root_and_rejects_runtime_data(
    tmp_path,
    monkeypatch,
):
    artifact = tmp_path / "package-1.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        for name, data in (
            ("package-1/README.md", b"safe release"),
            ("package-1/PKG-INFO", b"Name: package\n"),
            ("package-1/local.db", b"not a release database"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(
        gate,
        "tracked_file_labels",
        lambda: frozenset({"README.md"}),
    )

    findings = gate.scan_release_artifact(artifact)

    assert not any("README.md: package member" in item for item in findings)
    assert not any("PKG-INFO: package member" in item for item in findings)
    assert any("local.db: package member is not tracked" in item for item in findings)
    assert any("local.db: runtime database" in item for item in findings)


def test_history_scans_commit_metadata_and_normalized_path_names(monkeypatch):
    commit_id = "a" * 40
    blob_id = "b" * 40
    tag_id = "d" * 40
    employer_term = _joined("U", "BS")
    retired = _joined("CM", "DB", "Archive")
    former_project = _joined("grey", "swan")

    def completed(args, stdout=b"", returncode=0):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=b"")

    def fake_git(*args, check=True):
        if args[:2] == ("rev-parse", "--verify"):
            return completed(args)
        if args == ("rev-list", "--all"):
            return completed(args, f"{commit_id}\n".encode())
        if args == ("cat-file", "commit", commit_id):
            metadata = (
                f"tree {'c' * 40}\nauthor Example <person@example.test> 0 +0000\n"
                f"committer Example <person@example.test> 0 +0000\n\n{employer_term}\n"
            )
            return completed(args, metadata.encode())
        if args == (
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(objecttype)",
        ):
            return completed(
                args,
                f"refs/tags/{retired}\0{tag_id}\0tag\n".encode(),
            )
        if args == ("cat-file", "tag", tag_id):
            tag_metadata = (
                f"object {commit_id}\ntype commit\ntag v1\n\n{former_project}\n"
            )
            return completed(
                args,
                tag_metadata.encode(),
            )
        if args == ("rev-list", "--objects", "--all"):
            return completed(args, f"{blob_id} exports/{retired}.json\n".encode())
        if args == ("cat-file", "-t", blob_id):
            return completed(args, b"blob\n")
        if args == ("cat-file", "-s", blob_id):
            return completed(args, b"4\n")
        if args == ("cat-file", "blob", blob_id):
            return completed(args, b"safe")
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_git", fake_git)

    findings = gate.scan_history()

    assert any("commit" in item and "employer disclosure" in item for item in findings)
    assert any(
        "ref" in item and "legacy schema identifier" in item
        for item in findings
    )
    assert any("tag" in item and "legacy project identity" in item for item in findings)
    assert any(
        "exports/" in item and "legacy schema identifier" in item
        for item in findings
    )
    assert not any("email address" in item for item in findings)
