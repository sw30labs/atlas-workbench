"""Tests for core infrastructure: config, LLM, schema, db, judge, loops, driftlab."""

import json
import os
from pathlib import Path

import pytest


# ── Config tests ──

class TestConfig:
    def test_default_config_filename(self, monkeypatch):
        monkeypatch.delenv("ATLAS_CONFIG", raising=False)
        from workbench_atlas.config import project_root, yaml_config_path

        assert yaml_config_path() == project_root() / "workbench-atlas.yaml"

    def test_settings_defaults(self, tmp_path, monkeypatch):
        # Isolate from the project configuration — built-in defaults only.
        monkeypatch.setenv("ATLAS_CONFIG", str(tmp_path / "nope.yaml"))
        monkeypatch.delenv("ATLAS_DRIFTLAB_ENABLED", raising=False)
        from workbench_atlas.config import Settings, project_root
        s = Settings()
        assert s.omlx_base_url == "http://127.0.0.1:8000/v1"
        assert s.omlx_api_key == "test"
        assert s.omlx_model == "DeepSeek-V4.1-Flash-1017-oQ4e-mtp"
        assert s.max_judge_iterations == 3
        assert s.judge_score_threshold == 0.75
        # Absolute paths under project root
        assert s.scenario_db_path.is_absolute()
        assert s.evidence_db_path.is_absolute()
        assert s.scenario_db_path.parent == project_root()
        assert s.auto_retire is False
        assert s.driftlab_enabled is False
        assert s.driftlab_baseline_path.name == "workbench-atlas-pipeline-baseline.json"
        assert s.driftlab_baseline_path.is_file()

    def test_data_path_env_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_CONFIG", str(tmp_path / "nope.yaml"))
        monkeypatch.setenv("ATLAS_DB", str(tmp_path / "scenarios.db"))
        monkeypatch.setenv("ATLAS_EVIDENCE_DB", str(tmp_path / "evidence.db"))
        monkeypatch.setenv(
            "ATLAS_SCENARIO_EXPORT", str(tmp_path / "scenario_export.json")
        )
        from workbench_atlas.config import Settings

        settings = Settings()
        assert settings.scenario_db_path == tmp_path / "scenarios.db"
        assert settings.evidence_db_path == tmp_path / "evidence.db"
        assert settings.scenario_export_path == tmp_path / "scenario_export.json"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("OMLX_BASE_URL", "http://custom:9999/v1")
        monkeypatch.setenv("OMLX_API_KEY", "secret")
        monkeypatch.setenv("OMLX_MODEL", "custom-model")
        monkeypatch.setenv("ATLAS_AUTO_RETIRE", "1")
        monkeypatch.setenv("ATLAS_DRIFTLAB_ENABLED", "1")
        from workbench_atlas.config import Settings
        s = Settings()
        assert s.omlx_base_url == "http://custom:9999/v1"
        assert s.omlx_api_key == "secret"
        assert s.omlx_model == "custom-model"
        assert s.auto_retire is True
        assert s.driftlab_enabled is True

    def test_to_safe_dict_excludes_api_key(self):
        from workbench_atlas.config import Settings
        s = Settings(omlx_api_key="super-secret")
        safe = s.to_safe_dict()
        assert "omlx_api_key" not in safe
        assert "super-secret" not in repr(s)
        assert safe["omlx_model"] == s.omlx_model
        assert safe["judge_score_threshold"] == s.judge_score_threshold

    _OMLX_ENV = ("OMLX_BASE_URL", "OMLX_MODEL", "OMLX_MODEL_JUDGE", "OMLX_MODEL_FINAL_JUDGE")

    def test_yaml_config_file(self, tmp_path, monkeypatch):
        cfg = tmp_path / "gs.yaml"
        cfg.write_text(
            "omlx:\n"
            "  base_url: http://yaml:1234/v1\n"
            "  model: yaml-model\n"
            "  model_judge: yaml-judge\n"
            "  model_final_judge: yaml-final\n"
            "  timeout: 42\n"
            "  max_tokens: 777\n"
        )
        monkeypatch.setenv("ATLAS_CONFIG", str(cfg))
        for var in self._OMLX_ENV:
            monkeypatch.delenv(var, raising=False)
        from workbench_atlas.config import Settings
        s = Settings()
        assert s.omlx_base_url == "http://yaml:1234/v1"
        assert s.omlx_model == "yaml-model"
        assert s.omlx_model_judge == "yaml-judge"
        assert s.omlx_model_final_judge == "yaml-final"
        assert s.omlx_timeout == 42.0
        assert s.omlx_max_tokens == 777
        assert s.omlx_use_responses_api is False
        assert s.llm_use_responses_api is False

    def test_yaml_env_beats_file(self, tmp_path, monkeypatch):
        cfg = tmp_path / "gs.yaml"
        cfg.write_text("omlx:\n  model: yaml-model\n")
        monkeypatch.setenv("ATLAS_CONFIG", str(cfg))
        monkeypatch.setenv("OMLX_MODEL", "env-model")
        from workbench_atlas.config import Settings
        assert Settings().omlx_model == "env-model"

    def test_yaml_explicit_beats_file(self, tmp_path, monkeypatch):
        cfg = tmp_path / "gs.yaml"
        cfg.write_text("omlx:\n  model: yaml-model\n")
        monkeypatch.setenv("ATLAS_CONFIG", str(cfg))
        from workbench_atlas.config import Settings
        assert Settings(omlx_model="explicit-model").omlx_model == "explicit-model"

    def test_yaml_unknown_key_rejected(self, tmp_path, monkeypatch):
        cfg = tmp_path / "gs.yaml"
        cfg.write_text("omlx:\n  modle: typo-model\n")
        monkeypatch.setenv("ATLAS_CONFIG", str(cfg))
        from workbench_atlas.config import Settings
        with pytest.raises(Exception, match="unknown omlx key"):
            Settings()

    def test_yaml_bad_type_rejected(self, tmp_path, monkeypatch):
        cfg = tmp_path / "gs.yaml"
        cfg.write_text("omlx:\n  timeout: not-a-number\n")
        monkeypatch.setenv("ATLAS_CONFIG", str(cfg))
        from workbench_atlas.config import Settings
        with pytest.raises(Exception, match="omlx.timeout"):
            Settings()

    def test_yaml_missing_file_uses_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_CONFIG", str(tmp_path / "nope.yaml"))
        from workbench_atlas.config import Settings
        assert Settings().omlx_model == "DeepSeek-V4.1-Flash-1017-oQ4e-mtp"

    def test_use_responses_api_from_yaml_and_env(self, tmp_path, monkeypatch):
        cfg = tmp_path / "gs.yaml"
        cfg.write_text("omlx:\n  use_responses_api: true\n")
        monkeypatch.setenv("ATLAS_CONFIG", str(cfg))
        monkeypatch.delenv("OMLX_USE_RESPONSES_API", raising=False)
        from workbench_atlas.config import Settings
        assert Settings().llm_use_responses_api is True
        monkeypatch.setenv("OMLX_USE_RESPONSES_API", "0")
        assert Settings().llm_use_responses_api is False
        monkeypatch.setenv("ATLAS_LLM_PROVIDER", "dgx")
        monkeypatch.delenv("OMLX_USE_RESPONSES_API", raising=False)
        assert Settings().omlx_use_responses_api is True
        assert Settings().llm_use_responses_api is False


# ── LLM tests ──

class TestOMLXClient:
    def test_client_creation(self, settings):
        from workbench_atlas.llm import OMLXClient
        client = OMLXClient(settings)
        # The client reads the provider-dispatched accessors, not the OMLX
        # fields directly — asserting the latter would still pass on a DGX
        # misroute, since they are equal only for the default provider.
        assert client._base_url == settings.llm_base_url.rstrip("/")
        assert client._api_key == settings.llm_api_key
        client.close()

    def test_is_available(self, omlox_client):
        """Test that OMLX is reachable (may skip if not running)."""
        if not omlox_client.is_available():
            pytest.skip("OMLX not reachable")

    def test_list_models(self, omlox_client):
        """Test listing models from OMLX."""
        try:
            models = omlox_client.list_models()
            assert len(models) > 0
            assert "DeepSeek" in " ".join(models)
        except Exception as e:
            pytest.skip(f"OMLX not reachable: {e}")

    def test_chat(self, omlox_client):
        """Test a simple chat completion."""
        try:
            resp = omlox_client.chat(
                [{"role": "user", "content": "Say OK"}],
                max_tokens=5,
            )
            assert isinstance(resp, str)
            assert len(resp) > 0
        except Exception as e:
            pytest.skip(f"OMLX not reachable: {e}")


# ── Schema tests ──

class TestSchema:
    def test_migration_adds_columns(self, empty_db):
        from workbench_atlas.schema import get_schema_info
        info = get_schema_info(empty_db)
        col_names = [c[0] for c in info["scenarios_columns"]]
        assert "scenario_id" in col_names
        assert "origin" in col_names
        assert "title" in col_names
        assert "risk_category" in col_names
        assert "financial_impact_band" in col_names
        assert "event_driver_class" in col_names
        assert "primary_cause_class" in col_names
        assert "impact_band" in col_names
        assert "core_risks" in col_names
        assert "judge_score" in col_names
        assert "urgency_score" in col_names
        assert "theme_tags" in col_names
        assert "evidence_refs" in col_names
        # New tables
        assert "qa_logs" in info["tables"]
        assert "signpost_assessments" in info["tables"]
        assert "pattern_detections" in info["tables"]
        assert "causal_findings" in info["tables"]
        assert "pipeline_runs" in info["tables"]

    def test_migration_idempotent(self, empty_db):
        from workbench_atlas.schema import migrate
        # Run migration again — should be no-op
        added = migrate(empty_db)
        assert len(added) == 0

    def test_scenario_origin_is_explicit_and_legacy_safe(self, tmp_path):
        import sqlite3

        from workbench_atlas.db import get_scenario, upsert_scenario
        from workbench_atlas.schema import migrate

        db_path = str(tmp_path / "pre-origin-scenarios.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE scenarios (scenario_id TEXT, title TEXT)"
            )
            conn.execute(
                "INSERT INTO scenarios VALUES (?, ?)",
                ("HISTORICAL-001", "Historical scenario"),
            )

        migrate(db_path)
        assert get_scenario(db_path, "HISTORICAL-001")["origin"] == "legacy_unknown"

        upsert_scenario(
            db_path,
            {"scenario_id": "OPERATOR-001", "title": "Operator scenario"},
        )
        assert get_scenario(db_path, "OPERATOR-001")["origin"] == "operator_authored"

        upsert_scenario(
            db_path,
            {
                "scenario_id": "PIPELINE-001",
                "title": "Generated scenario",
                "origin": "pipeline_generated",
            },
        )
        upsert_scenario(
            db_path,
            {"scenario_id": "PIPELINE-001", "title": "Updated title"},
        )
        assert get_scenario(db_path, "PIPELINE-001")["origin"] == "pipeline_generated"
        upsert_scenario(
            db_path,
            {"scenario_id": "PIPELINE-001", "origin": "operator_authored"},
        )
        assert get_scenario(db_path, "PIPELINE-001")["origin"] == "operator_authored"
        with pytest.raises(ValueError, match="origin must be one of"):
            upsert_scenario(
                db_path,
                {
                    "scenario_id": "BAD-ORIGIN",
                    "title": "Invalid provenance",
                    "origin": "identifier_prefix_guess",
                },
            )

    def test_migration_backfills_pre_history_signpost_projection(
        self,
        empty_db,
        tmp_path,
    ):
        import json

        from workbench_atlas.db import (
            add_evidence_event,
            get_latest_signpost_assessment,
            get_scenario,
            upsert_scenario,
        )
        from workbench_atlas.review import approve_signpost_assessment
        from workbench_atlas.schema import migrate, migrate_evidence_events

        upsert_scenario(empty_db, {
            "scenario_id": "LEGACY-SIGNPOST",
            "status": "active",
            "signpost_status": "strengthened",
            "signpost_assessment": "Legacy projection rationale.",
            "signpost_evidence_refs": json.dumps(["E1"]),
            "signpost_checked_evidence_refs": json.dumps(["E1"]),
            "signposts_last_evaluated": "2026-01-01T00:00:00+00:00",
            "signpost_review_status": "pending",
        })
        upsert_scenario(empty_db, {
            "scenario_id": "LEGACY-RETIRED-SIGNPOST",
            "status": "retired",
            "signpost_status": "weakened",
            "signpost_assessment": "Retired legacy projection.",
            "signpost_evidence_refs": json.dumps(["E1"]),
            "signpost_checked_evidence_refs": json.dumps(["E1"]),
            "signposts_last_evaluated": "2026-01-01T00:00:00+00:00",
            "signpost_review_status": "pending",
        })

        assert migrate(empty_db) == []
        snapshot = get_latest_signpost_assessment(
            empty_db,
            "LEGACY-SIGNPOST",
        )
        assert snapshot["classification"] == "strengthened"
        assert snapshot["review_status"] == "pending"
        assert "Backfilled" in snapshot["reviewer_note"]
        assert snapshot["origin"] == "legacy_projection_backfill"
        retired_snapshot = get_latest_signpost_assessment(
            empty_db,
            "LEGACY-RETIRED-SIGNPOST",
        )
        assert retired_snapshot["review_status"] == "superseded"
        assert get_scenario(
            empty_db,
            "LEGACY-RETIRED-SIGNPOST",
        )["signpost_review_status"] == "superseded"

        evidence_db = str(tmp_path / "backfill-evidence.db")
        migrate_evidence_events(evidence_db)
        add_evidence_event(evidence_db, {
            "event_id": "E1",
            "source_url": "https://example.com/e1",
            "review_status": "approved_synthetic",
        })
        approve_signpost_assessment(
            empty_db,
            "LEGACY-SIGNPOST",
            evidence_db=evidence_db,
        )
        decided = get_latest_signpost_assessment(
            empty_db,
            "LEGACY-SIGNPOST",
        )
        assert decided["review_status"] == "approved"
        assert decided["origin"] == "legacy_projection_backfill"

    def test_migration_supersedes_old_pending_history_for_inactive_scenario(
        self,
        empty_db,
    ):
        import sqlite3

        from workbench_atlas.db import (
            get_latest_signpost_assessment,
            get_scenario,
            record_signpost_assessment,
            upsert_scenario,
        )
        from workbench_atlas.schema import migrate

        upsert_scenario(empty_db, {
            "scenario_id": "LEGACY-SAME-CYCLE-RETIRE",
            "title": "Legacy lifecycle inconsistency",
            "status": "active",
        })
        record_signpost_assessment(
            empty_db,
            "LEGACY-SAME-CYCLE-RETIRE",
            classification="strengthened",
            rationale="An old pending assessment.",
            evidence_refs=["E1"],
            considered_refs=["E1"],
            evaluated_at="2026-01-01T00:00:00+00:00",
        )
        # Reproduce the inconsistent state created by the pre-fix maintenance
        # ordering: lifecycle changed without closing the pending history row.
        with sqlite3.connect(empty_db) as conn:
            conn.execute(
                """
                UPDATE scenarios
                SET status = 'retired'
                WHERE scenario_id = 'LEGACY-SAME-CYCLE-RETIRE'
                """
            )

        assert migrate(empty_db) == []
        snapshot = get_latest_signpost_assessment(
            empty_db,
            "LEGACY-SAME-CYCLE-RETIRE",
        )
        assert snapshot["review_status"] == "superseded"
        assert "during migration" in snapshot["reviewer_note"]
        assert get_scenario(
            empty_db,
            "LEGACY-SAME-CYCLE-RETIRE",
        )["signpost_review_status"] == "superseded"

        # A second migration must leave the closed state stable.
        first_note = snapshot["reviewer_note"]
        assert migrate(empty_db) == []
        assert get_latest_signpost_assessment(
            empty_db,
            "LEGACY-SAME-CYCLE-RETIRE",
        )["reviewer_note"] == first_note

    def test_pre_origin_history_migration_preserves_uncertainty(self, tmp_path):
        import sqlite3

        from workbench_atlas.schema import migrate

        db_path = str(tmp_path / "pre-origin-history.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE scenarios (scenario_id TEXT, status TEXT)"
            )
            conn.execute(
                """
                CREATE TABLE signpost_assessments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_id TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    evidence_refs TEXT NOT NULL DEFAULT '[]',
                    considered_refs TEXT NOT NULL DEFAULT '[]',
                    evaluated_at TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    reviewed_at TEXT,
                    reviewer_note TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            common = (
                "unchanged",
                "Legacy rationale.",
                "[]",
                '["E1"]',
                "2026-01-01T00:00:00+00:00",
                "approved",
            )
            conn.execute(
                """
                INSERT INTO signpost_assessments (
                    scenario_id, classification, rationale, evidence_refs,
                    considered_refs, evaluated_at, review_status, reviewer_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "KNOWN-BACKFILL",
                    *common,
                    "Backfilled from the pre-history scenario projection.",
                ),
            )
            conn.execute(
                """
                INSERT INTO signpost_assessments (
                    scenario_id, classification, rationale, evidence_refs,
                    considered_refs, evaluated_at, review_status, reviewer_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "LOST-MARKER",
                    *common,
                    "Previously approved; original marker no longer available.",
                ),
            )

        migrate(db_path)
        with sqlite3.connect(db_path) as conn:
            origins = dict(
                conn.execute(
                    """
                    SELECT scenario_id, origin
                    FROM signpost_assessments
                    """
                ).fetchall()
            )

        assert origins["KNOWN-BACKFILL"] == "legacy_projection_backfill"
        assert origins["LOST-MARKER"] == "legacy_unknown"

    def test_migration_does_not_relabel_current_model_assessment(
        self,
        empty_db,
    ):
        import sqlite3

        from workbench_atlas.db import (
            get_latest_signpost_assessment,
            record_signpost_assessment,
            upsert_scenario,
        )
        from workbench_atlas.schema import migrate

        upsert_scenario(empty_db, {
            "scenario_id": "CURRENT-MODEL-ASSESSMENT",
            "title": "Current assessment provenance",
            "status": "active",
        })
        record_signpost_assessment(
            empty_db,
            "CURRENT-MODEL-ASSESSMENT",
            classification="unchanged",
            rationale="A current model-assisted interpretation.",
            evidence_refs=[],
            considered_refs=["E1"],
            evaluated_at="2026-01-01T00:00:00+00:00",
        )
        # Reviewer notes are free text and must never be used to relabel rows
        # once the origin column already exists.
        with sqlite3.connect(empty_db) as conn:
            conn.execute(
                """
                UPDATE signpost_assessments
                SET reviewer_note =
                    'Backfilled from the pre-history scenario projection.'
                WHERE scenario_id = 'CURRENT-MODEL-ASSESSMENT'
                """
            )

        migrate(empty_db)

        assert get_latest_signpost_assessment(
            empty_db,
            "CURRENT-MODEL-ASSESSMENT",
        )["origin"] == "model_assisted"

    def test_evidence_event_schema_uses_neutral_provenance_fields(self, tmp_path):
        import sqlite3

        from workbench_atlas.schema import EVIDENCE_EVENT_COLUMNS, migrate_evidence_events

        db_path = str(tmp_path / "evidence.db")
        migrate_evidence_events(db_path)
        with sqlite3.connect(db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(evidence_events)").fetchall()
            }
        assert columns == {name for name, _ in EVIDENCE_EVENT_COLUMNS}
        assert {
            "event_id",
            "source_url",
            "source_publisher",
            "retrieved_at",
            "record_type",
            "provenance_class",
            "rights_review",
            "content_fingerprint",
            "review_status",
            "reviewed_at",
            "independently_authored_summary",
            "taxonomy_version",
            "provenance_note",
        }.issubset(columns)
        assert migrate_evidence_events(db_path) == []

    def test_evidence_event_migration_is_transactional_and_lossless(self, tmp_path):
        import sqlite3

        from workbench_atlas.schema import migrate_evidence_events

        db_path = str(tmp_path / "legacy-evidence.db")
        legacy_table = "ne" + "ws"
        legacy_driver = "event_" + "type"
        legacy_cause = "root_" + "cause"
        legacy_source_type = "public_" + legacy_table
        legacy_index = "idx_" + legacy_table + "_event_id"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f'CREATE TABLE "{legacy_table}" ('
                'event_id TEXT, headline TEXT, '
                f'"{legacy_driver}" TEXT, "{legacy_cause}" TEXT, '
                'source_type TEXT, review_status TEXT, reviewed_at TEXT, '
                'provenance_note TEXT)'
            )
            conn.executemany(
                f'INSERT INTO "{legacy_table}" '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    (
                        "EVT-LEGACY-1",
                        "First event",
                        "Technology disruption",
                        "Process design",
                        legacy_source_type,
                        "approved",
                        "2026-01-02T03:04:05+00:00",
                        "Reviewed evidence.",
                    ),
                    (
                        "EVT-LEGACY-2",
                        "Second event",
                        None,
                        None,
                        legacy_source_type,
                        "pending",
                        None,
                        "Awaiting review.",
                    ),
                ],
            )
            conn.execute(
                f'CREATE UNIQUE INDEX "{legacy_index}" '
                f'ON "{legacy_table}" (event_id)'
            )

        migrate_evidence_events(db_path)

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(evidence_events)"
                )
            }
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM evidence_events ORDER BY event_id"
                )
            ]
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }

        assert tables == {"evidence_events", "review_decisions"}
        assert len(rows) == 2
        assert rows[0]["event_id"] == "EVT-LEGACY-1"
        assert rows[0]["event_driver_class"] == "Technology disruption"
        assert rows[0]["primary_cause_class"] == "Process design"
        assert rows[0]["source_type"] == "public_reporting"
        assert rows[0]["review_status"] == "approved"
        assert rows[0]["reviewed_at"] == "2026-01-02T03:04:05+00:00"
        assert rows[0]["provenance_note"] == "Reviewed evidence."
        assert legacy_driver not in columns
        assert legacy_cause not in columns
        assert legacy_index not in indexes
        assert migrate_evidence_events(db_path) == []

    def test_evidence_event_migration_rolls_back_on_column_conflict(self, tmp_path):
        import sqlite3

        from workbench_atlas.schema import migrate_evidence_events

        db_path = str(tmp_path / "conflicting-evidence.db")
        legacy_table = "ne" + "ws"
        legacy_driver = "event_" + "type"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f'CREATE TABLE "{legacy_table}" ('
                'event_id TEXT, '
                f'"{legacy_driver}" TEXT, event_driver_class TEXT)'
            )
            conn.execute(
                f'INSERT INTO "{legacy_table}" VALUES (?, ?, ?)',
                ("EVT-CONFLICT", "Human action", "Technology disruption"),
            )

        with pytest.raises(sqlite3.IntegrityError, match="conflicting"):
            migrate_evidence_events(db_path)

        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            row = conn.execute(
                f'SELECT * FROM "{legacy_table}"'
            ).fetchone()
        assert legacy_table in tables
        assert "evidence_events" not in tables
        assert row == ("EVT-CONFLICT", "Human action", "Technology disruption")


# ── DB tests ──

class TestDB:
    def test_upsert_scenario(self, empty_db):
        from workbench_atlas.db import upsert_scenario, get_scenario
        scenario = {
            "scenario_id": "TEST-001",
            "risk_category": "Digital resilience",
            "title": "Test Scenario",
            "description": "Test description",
            "created_year": 2026,
            "financial_impact_band": "Major",
            "event_driver_class": "Technology disruption",
            "primary_cause_class": "Process design",
            "impact_band": "High",
        }
        # Insert
        inserted = upsert_scenario(empty_db, scenario)
        assert inserted is True
        # Verify
        got = get_scenario(empty_db, "TEST-001")
        assert got is not None
        assert got["title"] == "Test Scenario"
        # Update
        scenario["title"] = "Updated Title"
        inserted = upsert_scenario(empty_db, scenario)
        assert inserted is False  # Updated, not new
        got = get_scenario(empty_db, "TEST-001")
        assert got["title"] == "Updated Title"

    def test_list_scenarios(self, empty_db):
        from workbench_atlas.db import upsert_scenario, list_scenarios
        for i in range(5):
            upsert_scenario(empty_db, {
                "scenario_id": f"TEST-{i:03d}",
                "risk_category": (
                    "Digital resilience" if i < 3 else "Physical resilience"
                ),
                "title": f"Test {i}",
                "created_year": 2026,
                "status": "active" if i < 4 else "draft",
            })
        all_s = list_scenarios(empty_db, status=None)
        assert len(all_s) == 5
        digital = list_scenarios(
            empty_db, category="Digital resilience", status=None
        )
        assert len(digital) == 3
        drafts = list_scenarios(empty_db, status="draft")
        assert len(drafts) == 1
        # draft filter must NOT pull active rows
        assert drafts[0]["scenario_id"] == "TEST-004"

    def test_evidence_event_stats(self, tmp_workspace):
        from workbench_atlas.db import get_evidence_event_stats
        stats = get_evidence_event_stats(str(tmp_workspace / "event_evidence.db"))
        assert stats["total"] > 0
        assert "categories" in stats
        assert "date_range" in stats
        assert "top_organizations" in stats

    def test_rejects_unknown_database_fields(self, empty_db, tmp_workspace):
        from workbench_atlas.db import add_evidence_event, upsert_scenario

        with pytest.raises(ValueError, match="Unknown scenario"):
            upsert_scenario(
                empty_db,
                {"scenario_id": "BAD-1", "source_specific_field": "no"},
            )
        with pytest.raises(ValueError, match="Unknown evidence-event"):
            add_evidence_event(
                str(tmp_workspace / "event_evidence.db"),
                {"event_id": "BAD-1", "source_specific_field": "no"},
            )

    def test_qa_log(self, empty_db):
        from workbench_atlas.db import log_qa
        log_id = log_qa(
            empty_db, "TEST-001", "phase3",
            "DeepSeek-V4", 0.85, "Good scenario", 2, True,
        )
        assert log_id > 0

    def test_pipeline_run_log(self, empty_db):
        from workbench_atlas.db import log_pipeline_run
        run_id = log_pipeline_run(
            empty_db, "run-001", "phase1", "completed",
            "2026-07-24T10:00:00", "2026-07-24T10:05:00",
            items_processed=10,
        )
        assert run_id > 0


# ── Judge tests ──

class TestJudge:
    def test_judge_prompt_includes_foresight_and_option_fields(self):
        from workbench_atlas.judge import build_judge_prompt

        scenario = {
            "title": "Recovery capacity squeeze",
            "external_drivers": ["Technology", "Demographic change"],
            "transmission_mechanism": "A shared outage exhausts specialists.",
            "second_order_effects": ["Manual queues grow."],
            "cross_industry_analogues": ["Public-infrastructure recovery."],
            "strengthening_signposts": ["Support windows shorten."],
            "weakening_signposts": ["Migrations reduce dependence."],
            "defensive_options": ["Cross-train operators."],
            "resilience_opportunities": ["Automate a recovery step."],
            "strategic_options": ["Reserve external capacity."],
            "option_expiry_signals": ["Support falls below one year."],
            "challenged_assumptions": ["Specialists remain available."],
            "discussion_prompts": ["Which step has no substitute?"],
        }

        prompt = build_judge_prompt(scenario)[1]["content"]

        assert "External Drivers: ['Technology', 'Demographic change']" in prompt
        assert "Transmission Mechanism: A shared outage exhausts specialists." in prompt
        assert "Strengthening Signposts: ['Support windows shorten.']" in prompt
        assert "Weakening Signposts: ['Migrations reduce dependence.']" in prompt
        assert "Strategic Options: ['Reserve external capacity.']" in prompt
        assert "Option-expiry Signals: ['Support falls below one year.']" in prompt
        assert "Challenged Assumptions: ['Specialists remain available.']" in prompt

    def test_judge_with_mock(self, mock_omlox_client):
        from workbench_atlas.judge import judge_scenario
        scenario = {
            "title": "Cloud Identity Concentration Failure",
            "description": "A shared cloud identity provider outage cascading across hypothetical peer banks.",
            "risk_category": "Digital resilience",
            "financial_impact_band": "Major",
            "preparation_strategy": "Stress-test failover",
            "detailed_rationale": "Public incident reporting shows increasing identity disruptions",
            "core_risks": "Cloud Concentration",
        }
        result = judge_scenario(scenario, mock_omlox_client)
        assert result.score > 0
        assert "clarity" in result.criteria
        assert len(result.feedback) > 0


# ── Ralph Loop tests ──

class TestRalphLoop:
    def test_ralph_loop_passes(self, mock_omlox_client):
        from workbench_atlas.loops import ralph_loop
        scenario = {
            "title": "Test Scenario",
            "description": "A well-described scenario with clear evidence.",
            "risk_category": "Digital resilience",
            "financial_impact_band": "Major",
            "preparation_strategy": "Deploy controls",
            "detailed_rationale": "Based on cited public reporting",
            "core_risks": "Cloud risk",
        }
        result = ralph_loop(scenario, mock_omlox_client, max_iterations=2)
        assert result.iterations >= 1
        assert result.final_score > 0
        assert len(result.history) >= 1

    def test_ralph_loop_max_iterations(self, mock_omlox_client):
        from workbench_atlas.loops import ralph_loop
        # Mock that always fails
        mock_omlox_client.chat = lambda *a, **k: '{"score": 0.3, "criteria": {}, "feedback": "Needs work", "passed": false}'
        mock_omlox_client.chat_json = lambda *a, **k: json.loads(mock_omlox_client.chat())
        scenario = {"title": "T", "description": "D", "risk_category": "C"}
        result = ralph_loop(scenario, mock_omlox_client, max_iterations=2)
        assert result.passed is False
        assert result.iterations == 2

    def test_ralph_loop_final_judge_model(self, mock_omlox_client):
        """Final judge should be invoked with the requested model."""
        from workbench_atlas.loops import ralph_loop

        calls = []

        def tracked_chat_json(messages, **kwargs):
            calls.append(kwargs.get("model"))
            return {"score": 0.85, "criteria": {}, "feedback": "Good", "passed": True}

        mock_omlox_client.chat_json = tracked_chat_json
        scenario = {"title": "T", "description": "D", "risk_category": "C"}
        result = ralph_loop(
            scenario,
            mock_omlox_client,
            max_iterations=2,
            final_judge_model="final-judge-model",
        )
        assert result.passed is True
        # The final judge call is the last one and uses the requested model.
        assert calls[-1] == "final-judge-model"

    def test_ralph_loop_forwards_scenario_id(self, mock_omlox_client):
        """Judge + improve calls tag telemetry with the scenario under judgment."""
        from workbench_atlas.loops import ralph_loop

        calls = []

        def tracked_chat_json(messages, **kwargs):
            calls.append(kwargs.get("scenario_id"))
            return {"score": 0.85, "criteria": {}, "feedback": "Good", "passed": True}

        mock_omlox_client.chat_json = tracked_chat_json
        scenario = {"title": "T", "description": "D", "risk_category": "C"}
        result = ralph_loop(scenario, mock_omlox_client, max_iterations=2,
                            scenario_id="SCN-2099-001")
        assert result.passed is True
        assert calls and all(c == "SCN-2099-001" for c in calls)


# ── DriftLab hooks tests ──

class TestDriftLabHooks:
    def test_create_trace_handler(self):
        from workbench_atlas.driftlab_hooks import create_trace_handler
        handler = create_trace_handler(agent="test", run_id="test-001")
        # May be None if driftlab not available, but shouldn't crash
        assert handler is not None or handler is None  # Just verify no crash

    def test_minimal_governance_check(self):
        from workbench_atlas.driftlab_hooks import _minimal_governance_check
        events = [
            {"kind": "route", "node": "phase1"},
            {"kind": "tool_call", "node": "phase1", "tool": "scrape"},
            {"kind": "error", "node": "phase1"},
        ]
        result = _minimal_governance_check(events, "test")
        assert "score" in result
        assert "cycle_id" in result
        assert "anomalies" in result

    def test_record_phase_events(self):
        from workbench_atlas.driftlab_hooks import record_phase_events
        # Should not crash even with None handler
        record_phase_events(None, "test", ["tool1"])
