"""I test my offline MITRE ATLAS v6 catalog boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from contingency_atlas.frameworks import (
    FrameworkImportError,
    ImportSizeError,
    ReleaseCollisionError,
    activate_release,
    framework_status,
    import_mitre_atlas,
    migrate_frameworks,
    search_techniques,
    technique_context,
)
from contingency_atlas.frameworks.cli import main as framework_main
from contingency_atlas.frameworks.mitre_atlas import parse_mitre_atlas


def _document(version: str = "2026.07") -> dict:
    return {
        "format-version": "6.0.0",
        "collection": {
            "id": "ATLAS-collection",
            "object-type": "collection",
            "name": "ATLAS",
            "description": "Synthetic test catalog",
            "version": version,
        },
        "matrix": {
            "id": "ATLAS-matrix",
            "object-type": "matrix",
            "name": "ATLAS",
            "description": "Synthetic matrix",
        },
        "tactics": {
            "AML.TA0001": {
                "id": "AML.TA0001",
                "object-type": "tactic",
                "name": "AI Attack Staging",
                "description": "Prepare an AI-specific attack.",
                "references": [],
            }
        },
        "techniques": {
            "AML.T0051": {
                "id": "AML.T0051",
                "object-type": "technique",
                "name": "LLM Prompt Injection",
                "description": "Inject instructions into a model context.",
                "references": [],
                "maturity": "Demonstrated",
                "platforms": ["Generative AI", "Agentic AI"],
            },
            "AML.T0051.000": {
                "id": "AML.T0051.000",
                "object-type": "technique",
                "name": "Direct Prompt Injection",
                "description": "Supply an instruction directly.",
                "references": [],
                "maturity": "Realized",
                "platforms": ["Agentic AI"],
            },
        },
        "mitigations": {
            "AML.M0030": {
                "id": "AML.M0030",
                "object-type": "mitigation",
                "name": "Restrict Tool Invocation",
                "description": "Restrict tools when untrusted data is present.",
                "references": [],
                "lifecycle-phases": ["Deployment"],
                "categories": ["Technical - AI"],
            }
        },
        "case-studies": {
            "AML.CS0001": {
                "id": "AML.CS0001",
                "object-type": "case-study",
                "name": "Synthetic prompt injection exercise",
                "description": "A synthetic exercise for importer tests.",
                "references": [],
                "type": "Exercise",
                "actor": "Synthetic research team",
                "target": "Synthetic application",
                "date": "2026-01-01",
                "date-granularity": "Day",
            }
        },
        "relationships": {
            "ATLAS-matrix": {
                "sequences": [
                    {
                        "source": "ATLAS-matrix",
                        "target": "AML.TA0001",
                        "relationship-type": "sequences",
                        "position": 1,
                    }
                ]
            },
            "AML.T0051": {
                "achieves": [
                    {
                        "source": "AML.T0051",
                        "target": "AML.TA0001",
                        "relationship-type": "achieves",
                    }
                ]
            },
            "AML.T0051.000": {
                "achieves": [
                    {
                        "source": "AML.T0051.000",
                        "target": "AML.TA0001",
                        "relationship-type": "achieves",
                    }
                ],
                "specializes": [
                    {
                        "source": "AML.T0051.000",
                        "target": "AML.T0051",
                        "relationship-type": "specializes",
                    }
                ],
            },
            "AML.M0030": {
                "mitigates": [
                    {
                        "source": "AML.M0030",
                        "target": "AML.T0051.000",
                        "relationship-type": "mitigates",
                        "description": "Require a trust boundary.",
                    }
                ]
            },
            "AML.CS0001": {
                "employs": [
                    {
                        "source": "AML.CS0001",
                        "target": "AML.T0051.000",
                        "relationship-type": "employs",
                        "description": "A synthetic procedure step.",
                        "tactic": "AML.TA0001",
                        "step-id": "S00",
                        "leads-to": [],
                    }
                ]
            },
        },
    }


def _write(path: Path, document: dict) -> bytes:
    encoded = yaml.safe_dump(document, sort_keys=False).encode("utf-8")
    path.write_bytes(encoded)
    return encoded


def test_imports_valid_v6_catalog_and_resolves_context(tmp_path):
    source = tmp_path / "ATLAS-2026.07.yaml"
    raw = _write(source, _document())
    db = tmp_path / "atlas.db"

    summary = import_mitre_atlas(
        db,
        source,
        "https://example.test/ATLAS-2026.07.yaml",
        activate=True,
    )

    assert summary == {
        "framework": "mitre-atlas",
        "content_version": "2026.07",
        "format_version": "6.0.0",
        "objects": {
            "tactics": 1,
            "techniques": 2,
            "mitigations": 1,
            "case_studies": 1,
        },
        "relationships": 6,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "activated": True,
        "already_imported": False,
    }
    status = framework_status(db)
    assert status["active_release"] == "2026.07"
    assert status["counts"] == {
        "tactics": 1,
        "techniques": 2,
        "mitigations": 1,
        "case_studies": 1,
        "relationships": 6,
    }

    hits = search_techniques(
        db,
        "direct",
        platform="Agentic AI",
        maturity="Realized",
    )
    assert [hit["technique_id"] for hit in hits] == ["AML.T0051.000"]
    assert hits[0]["platforms"] == ["Agentic AI"]

    context = technique_context(db, "2026.07", "AML.T0051.000")
    assert context is not None
    assert [item["object_id"] for item in context["tactics"]] == ["AML.TA0001"]
    assert [item["object_id"] for item in context["mitigations"]] == ["AML.M0030"]
    assert [item["object_id"] for item in context["case_studies"]] == ["AML.CS0001"]
    assert context["parent_technique"]["object_id"] == "AML.T0051"
    assert context["mitigations"][0]["relationships"][0]["description"] == (
        "Require a trust boundary."
    )
    assert "metadata_json" not in context["mitigations"][0]


def test_migration_and_empty_status_are_idempotent(tmp_path):
    db = tmp_path / "atlas.db"
    migrate_frameworks(db)
    migrate_frameworks(db)

    status = framework_status(db)
    assert status["installed"] is False
    assert status["available"] is False
    assert status["active_release"] is None
    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "framework_releases",
        "framework_objects",
        "framework_relationships",
    } <= tables


def test_same_source_hash_is_an_idempotent_import(tmp_path):
    source = tmp_path / "atlas.yaml"
    _write(source, _document())
    db = tmp_path / "atlas.db"
    first = import_mitre_atlas(db, source, "https://example.test/atlas.yaml")
    second = import_mitre_atlas(
        db,
        source,
        "https://a-different-provenance-label.test/atlas.yaml",
        activate=True,
    )

    assert first["already_imported"] is False
    assert second["already_imported"] is True
    assert second["activated"] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM framework_releases").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM framework_objects").fetchone()[0] == 5


def test_reused_content_version_with_different_bytes_is_rejected(tmp_path):
    source = tmp_path / "atlas.yaml"
    db = tmp_path / "atlas.db"
    _write(source, _document())
    import_mitre_atlas(db, source, "https://example.test/atlas.yaml")
    changed = _document()
    changed["tactics"]["AML.TA0001"]["name"] = "Changed upstream bytes"
    _write(source, changed)

    with pytest.raises(ReleaseCollisionError, match="different bytes"):
        import_mitre_atlas(db, source, "https://example.test/atlas.yaml")

    with sqlite3.connect(db) as conn:
        name = conn.execute(
            "SELECT name FROM framework_objects WHERE object_id = 'AML.TA0001'"
        ).fetchone()[0]
    assert name == "AI Attack Staging"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: d["techniques"]["AML.T0051"].update(
                {"object-type": "mitigation"}
            ),
            "object-type",
        ),
        (
            lambda d: d["techniques"]["AML.T0051"].update(
                {"maturity": "Speculative"}
            ),
            "maturity",
        ),
        (
            lambda d: d["techniques"]["AML.T0051"].update(
                {"platforms": ["Quantum AI"]}
            ),
            "platforms",
        ),
        (
            lambda d: d["tactics"]["AML.TA0001"].update(
                {"id": "AML.TA001"}
            ),
            "map key",
        ),
    ],
)
def test_strict_object_validation_rolls_back(tmp_path, mutate, message):
    document = _document()
    mutate(document)
    source = tmp_path / "invalid.yaml"
    _write(source, document)
    db = tmp_path / "atlas.db"

    with pytest.raises(FrameworkImportError, match=message):
        import_mitre_atlas(db, source, "https://example.test/invalid.yaml")
    assert not db.exists()


def test_requires_exact_v6_top_level_contract(tmp_path):
    document = _document()
    document["unexpected"] = {}
    source = tmp_path / "invalid.yaml"
    _write(source, document)

    with pytest.raises(FrameworkImportError, match="top-level keys"):
        import_mitre_atlas(
            tmp_path / "atlas.db",
            source,
            "https://example.test/invalid.yaml",
        )


def test_rejects_dangling_relationship_before_writing_release(tmp_path):
    document = _document()
    document["relationships"]["AML.M0030"]["mitigates"][0]["target"] = (
        "AML.T9999"
    )
    source = tmp_path / "dangling.yaml"
    _write(source, document)
    db = tmp_path / "atlas.db"

    with pytest.raises(FrameworkImportError, match="dangling"):
        import_mitre_atlas(db, source, "https://example.test/dangling.yaml")
    assert not db.exists()


def test_database_failure_rolls_back_release_and_objects(tmp_path):
    source = tmp_path / "atlas.yaml"
    _write(source, _document())
    db = tmp_path / "atlas.db"
    migrate_frameworks(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_test_relationship
            BEFORE INSERT ON framework_relationships
            BEGIN SELECT RAISE(ABORT, 'synthetic relationship failure'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic"):
        import_mitre_atlas(db, source, "https://example.test/atlas.yaml")

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM framework_releases").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM framework_objects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM framework_relationships").fetchone()[0] == 0


def test_size_limit_is_checked_before_yaml_parse(tmp_path, monkeypatch):
    source = tmp_path / "too-large.yaml"
    source.write_bytes(b"x" * 65)

    def must_not_parse(_raw):
        raise AssertionError("safe_load must not run for oversized input")

    monkeypatch.setattr(
        "contingency_atlas.frameworks.mitre_atlas.yaml.load", must_not_parse
    )
    with pytest.raises(ImportSizeError, match="limit is 64"):
        import_mitre_atlas(
            tmp_path / "atlas.db",
            source,
            "https://example.test/too-large.yaml",
            max_bytes=64,
        )


def test_file_read_is_descriptor_bound_and_capped_at_limit_plus_one(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "atlas.yaml"
    raw = _write(source, _document())
    db = tmp_path / "atlas.db"
    max_bytes = len(raw) + 17
    original_open = Path.open
    read_sizes: list[int] = []

    class TrackedFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            read_sizes.append(size)
            return self.handle.read(size)

    def tracked_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return TrackedFile(handle) if path == source else handle

    def reject_path_lookup(*_args, **_kwargs):
        raise AssertionError("I must inspect and read the opened descriptor")

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(Path, "stat", reject_path_lookup)
    monkeypatch.setattr(Path, "is_file", reject_path_lookup)
    monkeypatch.setattr(Path, "read_bytes", reject_path_lookup)

    result = import_mitre_atlas(
        db,
        source,
        "https://example.test/atlas.yaml",
        max_bytes=max_bytes,
    )

    assert result["content_version"] == "2026.07"
    assert read_sizes == [max_bytes + 1]


def test_expected_sha256_accepts_a_case_insensitive_exact_pin(tmp_path):
    source = tmp_path / "atlas.yaml"
    raw = _write(source, _document())
    expected = hashlib.sha256(raw).hexdigest()

    result = import_mitre_atlas(
        tmp_path / "atlas.db",
        source,
        "https://operator.example/asserted-source",
        expected_sha256=expected.upper(),
    )

    assert result["source_sha256"] == expected


def test_invalid_expected_sha256_fails_before_open_or_database_creation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "atlas.yaml"
    _write(source, _document())
    db = tmp_path / "atlas.db"

    def must_not_open(*_args, **_kwargs):
        raise AssertionError("I must validate the digest pin before opening input")

    monkeypatch.setattr(Path, "open", must_not_open)
    with pytest.raises(FrameworkImportError, match="exactly 64 hexadecimal"):
        import_mitre_atlas(
            db,
            source,
            "https://example.test/atlas.yaml",
            expected_sha256="not-a-sha256",
        )

    assert not db.exists()


def test_expected_sha256_mismatch_fails_before_yaml_or_database(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "atlas.yaml"
    raw = _write(source, _document())
    db = tmp_path / "atlas.db"
    wrong = "0" * 64
    assert hashlib.sha256(raw).hexdigest() != wrong

    def must_not_parse(*_args, **_kwargs):
        raise AssertionError("I must compare the exact bytes before YAML parsing")

    monkeypatch.setattr(
        "contingency_atlas.frameworks.mitre_atlas.yaml.load",
        must_not_parse,
    )
    with pytest.raises(FrameworkImportError, match="does not match expected_sha256"):
        import_mitre_atlas(
            db,
            source,
            "https://example.test/atlas.yaml",
            expected_sha256=wrong,
        )

    assert not db.exists()


def test_recursive_yaml_alias_fails_closed_without_creating_a_database(tmp_path):
    source = tmp_path / "recursive.yaml"
    source.write_text("loop: &loop\n  - *loop\n")
    db = tmp_path / "atlas.db"

    with pytest.raises(FrameworkImportError, match="recursive YAML alias"):
        import_mitre_atlas(db, source, "https://example.test/recursive.yaml")

    assert not db.exists()


def test_duplicate_yaml_key_fails_closed_without_creating_a_database(tmp_path):
    source = tmp_path / "duplicate.yaml"
    source.write_text("format-version: 6.0.0\nformat-version: 6.0.1\n")
    db = tmp_path / "atlas.db"

    with pytest.raises(FrameworkImportError, match="duplicate YAML key"):
        import_mitre_atlas(db, source, "https://example.test/duplicate.yaml")

    assert not db.exists()


@pytest.mark.parametrize(
    ("constant", "limit", "content", "message"),
    [
        ("MAX_YAML_DEPTH", 3, "a: [[[[value]]]]\n", "nesting exceeds"),
        ("MAX_YAML_NODES", 5, "a: [1, 2, 3, 4, 5, 6]\n", "nodes exceed"),
        (
            "MAX_YAML_ALIASES",
            2,
            "anchor: &item [value]\na: *item\nb: *item\nc: *item\n",
            "aliases exceed",
        ),
    ],
)
def test_yaml_structure_bounds_fail_before_schema_validation(
    tmp_path,
    monkeypatch,
    constant,
    limit,
    content,
    message,
):
    source = tmp_path / f"{constant}.yaml"
    source.write_text(content)
    monkeypatch.setattr(
        f"contingency_atlas.frameworks.mitre_atlas.{constant}",
        limit,
    )

    with pytest.raises(FrameworkImportError, match=message):
        import_mitre_atlas(
            tmp_path / "atlas.db",
            source,
            "https://example.test/bounded.yaml",
        )


def test_activation_preserves_older_catalogs(tmp_path):
    db = tmp_path / "atlas.db"
    first_path = tmp_path / "first.yaml"
    second_path = tmp_path / "second.yaml"
    _write(first_path, _document("2026.06"))
    newer = _document("2026.07")
    newer["techniques"]["AML.T0051"]["description"] = "New release text."
    _write(second_path, newer)

    import_mitre_atlas(
        db, first_path, "https://example.test/first.yaml", activate=True
    )
    import_mitre_atlas(db, second_path, "https://example.test/second.yaml")
    assert framework_status(db)["active_release"] == "2026.06"
    activate_release(db, "2026.07")

    status = framework_status(db)
    assert status["active_release"] == "2026.07"
    assert {release["content_version"] for release in status["releases"]} == {
        "2026.06",
        "2026.07",
    }
    assert search_techniques(db, "New release text")[0]["content_version"] == "2026.07"
    old = search_techniques(
        db, "Inject instructions", content_version="2026.06"
    )
    assert old[0]["content_version"] == "2026.06"


def test_import_seals_release_before_activation_and_blocks_appends(tmp_path):
    source = tmp_path / "atlas.yaml"
    _write(source, _document())
    db = tmp_path / "atlas.db"

    import_mitre_atlas(
        db,
        source,
        "https://example.test/atlas.yaml",
        activate=True,
    )

    status = framework_status(db)
    assert status["active"]["is_sealed"] is True
    with sqlite3.connect(db) as conn:
        sealed = conn.execute(
            """
            SELECT is_sealed FROM framework_releases
            WHERE framework = 'mitre-atlas' AND content_version = '2026.07'
            """
        ).fetchone()[0]
        assert sealed == 1
        with pytest.raises(sqlite3.IntegrityError, match="release is sealed"):
            conn.execute(
                """
                INSERT INTO framework_objects VALUES (
                    'mitre-atlas', '2026.07', 'AML.T9999', 'technique',
                    'Late object', '', 'Feasible', '[]', 'hash', '{}'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="release is sealed"):
            conn.execute(
                """
                INSERT INTO framework_relationships VALUES (
                    'mitre-atlas', '2026.07', 'late-rel', 'AML.T0051',
                    'achieves', 'AML.TA0001', '{}'
                )
                """
            )
        conn.execute(
            """
            UPDATE framework_releases SET is_active = 0
            WHERE framework = 'mitre-atlas' AND content_version = '2026.07'
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="seal is immutable"):
            conn.execute(
                """
                UPDATE framework_releases SET is_sealed = 0
                WHERE framework = 'mitre-atlas' AND content_version = '2026.07'
                """
            )


def _create_pre_seal_catalog(db: Path, *, include_seal: bool) -> None:
    seal_column = (
        ", is_sealed INTEGER NOT NULL DEFAULT 0" if include_seal else ""
    )
    with sqlite3.connect(db) as conn:
        conn.executescript(
            f"""
            CREATE TABLE framework_releases (
                framework TEXT NOT NULL,
                content_version TEXT NOT NULL,
                format_version TEXT NOT NULL,
                release_date TEXT,
                source_url TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0
                {seal_column},
                PRIMARY KEY (framework, content_version)
            );
            CREATE TABLE framework_objects (
                framework TEXT NOT NULL,
                content_version TEXT NOT NULL,
                object_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                maturity TEXT,
                platforms_json TEXT NOT NULL DEFAULT '[]',
                object_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (framework, content_version, object_id)
            );
            CREATE TABLE framework_relationships (
                framework TEXT NOT NULL,
                content_version TEXT NOT NULL,
                relationship_key TEXT NOT NULL,
                source_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{{}}',
                PRIMARY KEY (framework, content_version, relationship_key)
            );
            """
        )
        release_values = (
            "('mitre-atlas', '2026.01', '6.0.0', NULL, "
            "'https://example.test/old.yaml', 'old-hash', '2026-02-01', 0"
            + (", 0" if include_seal else "")
            + ")"
        )
        conn.execute(f"INSERT INTO framework_releases VALUES {release_values}")
        conn.execute(
            """
            INSERT INTO framework_objects VALUES (
                'mitre-atlas', '2026.01', 'AML.T0001', 'technique',
                'Old object', '', 'Feasible', '["Enterprise"]', 'hash', '{}'
            )
            """
        )


def test_migration_seals_releases_from_schema_without_seal_column(
    tmp_path,
):
    db = tmp_path / "old-atlas.db"
    _create_pre_seal_catalog(db, include_seal=False)

    migrate_frameworks(db)
    migrate_frameworks(db)

    with sqlite3.connect(db) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(framework_releases)")
        }
        assert "is_sealed" in columns
        assert conn.execute(
            "SELECT is_sealed FROM framework_releases"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="release is sealed"):
            conn.execute(
                """
                INSERT INTO framework_objects VALUES (
                    'mitre-atlas', '2026.01', 'AML.T0002', 'technique',
                    'Late object', '', 'Feasible', '[]', 'hash-2', '{}'
                )
                """
            )


def test_migration_preserves_explicit_unsealed_import_in_progress(tmp_path):
    db = tmp_path / "unsealed-atlas.db"
    _create_pre_seal_catalog(db, include_seal=True)

    migrate_frameworks(db)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT is_sealed FROM framework_releases"
        ).fetchone()[0] == 0
    with pytest.raises(ValueError, match="Cannot activate unsealed"):
        activate_release(db, "2026.01")


def test_canonical_hashes_do_not_depend_on_mapping_order():
    first = _document()
    second = {
        key: copy.deepcopy(value)
        for key, value in reversed(list(first.items()))
    }
    tactic = second["tactics"]["AML.TA0001"]
    second["tactics"]["AML.TA0001"] = {
        key: value for key, value in reversed(list(tactic.items()))
    }

    parsed_first = parse_mitre_atlas(first)
    parsed_second = parse_mitre_atlas(second)
    assert {
        item["object_id"]: item["object_hash"] for item in parsed_first.objects
    } == {
        item["object_id"]: item["object_hash"] for item in parsed_second.objects
    }
    assert [
        item["relationship_key"] for item in parsed_first.relationships
    ] == [item["relationship_key"] for item in parsed_second.relationships]


def test_search_treats_wildcards_literally_and_bounds_results(tmp_path):
    source = tmp_path / "atlas.yaml"
    _write(source, _document())
    db = tmp_path / "atlas.db"
    import_mitre_atlas(
        db, source, "https://example.test/atlas.yaml", activate=True
    )

    assert search_techniques(db, "%") == []
    assert len(search_techniques(db, limit=10_000)) == 2
    assert search_techniques(db, limit=0) == []
    with pytest.raises(ValueError, match="Unknown MITRE ATLAS platform"):
        search_techniques(db, platform="Unknown")


def test_cli_import_status_and_error_are_machine_readable(tmp_path, capsys):
    source = tmp_path / "atlas.yaml"
    raw = _write(source, _document())
    expected_sha256 = hashlib.sha256(raw).hexdigest()
    db = tmp_path / "atlas.db"
    assert framework_main(
        [
            "mitre-import",
            "--db",
            str(db),
            "--input",
            str(source),
            "--source-url",
            "https://example.test/atlas.yaml",
            "--expected-sha256",
            expected_sha256,
            "--activate",
        ]
    ) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["content_version"] == "2026.07"

    assert framework_main(["status", "--db", str(db)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["active_release"] == "2026.07"

    assert framework_main(["activate", "missing", "--db", str(db)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert "Unknown mitre-atlas content version" in error["error"]
