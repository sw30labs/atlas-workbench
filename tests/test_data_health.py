import json

from workbench_atlas.web.health import data_health


def test_malformed_register_is_unavailable_not_empty(settings):
    settings.emerging_risks_path.write_text('{"unexpected":"object"}')
    result = data_health(settings)
    assert result["status"] == "unavailable"
    assert next(s for s in result["stores"] if s["store"] == "risks")["status"] == "unavailable"


def test_corrupt_evidence_database_is_reported(settings):
    settings.evidence_db_path.write_bytes(b"invalid database")
    assert data_health(settings)["status"] == "unavailable"


def test_empty_initialized_register_is_available(settings):
    settings.emerging_risks_path.write_text(json.dumps([]))
    assert data_health(settings)["status"] == "ok"
