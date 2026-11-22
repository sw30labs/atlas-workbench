"""Private organization imports use previews and atomic typed confirmation."""

import json
import sqlite3
import threading

import pytest

from workbench_atlas.context import context_payload
from workbench_atlas.imports import ImportError, preview_import, submit_import


@pytest.fixture(autouse=True)
def clear_previews():
    from workbench_atlas import imports
    imports._PREVIEWS.clear()
    yield
    imports._PREVIEWS.clear()


@pytest.fixture
def private_settings(settings, tmp_path):
    return settings.model_copy(update={"context_enabled": True, "context_db_path": tmp_path / "private-context.db"})


def service(record_id="service-a", name="Fictional payment processing"):
    return {"id": record_id, "name": name, "owner": "Synthetic service owner",
            "impact_tolerance": "Fictional exercise tolerance: four hours"}


def preview(settings, records, **kwargs):
    return preview_import(settings, content=json.dumps(records), format="json", actor="Synthetic operator",
                          source="Fictional organization inventory for tests", kind="services", **kwargs)


def submit(settings, result, selected=None, actor="Synthetic operator"):
    return submit_import(settings, preview_id=result["preview_id"], actor=actor,
                         selected_ids=selected or [row["candidate_id"] for row in result["candidates"]])


def test_preview_creates_no_store_and_confirmation_preserves_public_files(private_settings):
    public = [private_settings.scenario_db_path, private_settings.evidence_db_path, private_settings.emerging_risks_path]
    before = {path: path.read_bytes() for path in public}
    result = preview(private_settings, [service()])
    assert result["counts"] == {"total": 1, "valid": 1, "invalid": 0}
    assert not private_settings.context_db_path.exists()
    done = submit(private_settings, result)
    assert done["imported_count"] == 1
    stored = context_payload(private_settings)["records"]["services"][0]
    assert stored["classification"] == "organization"
    assert stored["source"] == "Fictional organization inventory for tests"
    assert {path: path.read_bytes() for path in public} == before
    with sqlite3.connect(private_settings.context_db_path) as conn:
        assert conn.execute("SELECT actor FROM context_decisions").fetchone()[0] == "Synthetic operator"


def test_csv_mapping_and_array_conversion(private_settings):
    content = 'Name,Person,Tolerance,Refs,Ignore\nFictional service,Synthetic owner,Four hours,Doc A;Doc B,irrelevant\n'
    result = preview_import(private_settings, content=content, format="csv", kind="services", actor="Synthetic operator",
                            source="Fictional mapping source", field_mapping={"Name": "name", "Person": "owner",
                            "Tolerance": "impact_tolerance", "Refs": "source_refs"})
    candidate = result["candidates"][0]
    assert candidate["row_index"] == 0 and candidate["row_number"] == 2
    assert candidate["record"]["data"]["source_refs"] == ["Doc A", "Doc B"]
    assert "Ignore" not in candidate["record"]["data"]
    assert not candidate["errors"]
    submit(private_settings, result)


def test_forward_references_are_resolved_atomically_at_confirmation(private_settings):
    records = [
        {"kind": "controls", "data": {"id": "control-a", "name": "Fictional fallback exercise", "owner": "Synthetic owner",
                                      "service_ids": "service-a", "source_refs": '["Document A"]'}},
        {"kind": "services", "data": service()},
    ]
    result = preview_import(private_settings, content=json.dumps(records), format="json", actor="Synthetic operator",
                            source="Fictional controls inventory")
    assert result["counts"]["valid"] == 2
    done = submit(private_settings, result)
    assert done["imported_count"] == 2
    assert len(context_payload(private_settings)["records"]["controls"]) == 1


def test_failed_reference_check_does_not_partially_import(private_settings):
    records = [
        {"kind": "services", "data": service()},
        {"kind": "controls", "data": {"id": "control-a", "name": "Fictional control", "owner": "Synthetic owner",
                                      "service_ids": ["missing-service"]}},
    ]
    result = preview_import(private_settings, content=json.dumps(records), format="json", actor="Synthetic operator",
                            source="Fictional controls inventory")
    assert result["counts"]["valid"] == 2
    with pytest.raises(ImportError, match="No records were imported"):
        submit(private_settings, result)
    assert context_payload(private_settings)["records"]["services"] == []
    assert context_payload(private_settings)["changes"] == []
    # The valid row can still be selected from the frozen preview after failure.
    assert submit(private_settings, result, ["row-1"])["imported_count"] == 1


def test_validation_errors_are_per_row_and_valid_subset_can_be_submitted(private_settings):
    result = preview(private_settings, [service(), {"name": "Missing mandatory owner and tolerance"}])
    assert result["counts"] == {"total": 2, "valid": 1, "invalid": 1}
    assert "owner" in " ".join(result["candidates"][1]["errors"])
    with pytest.raises(ImportError, match="validation errors"):
        submit(private_settings, result)
    assert submit(private_settings, result, ["row-1"])["imported_count"] == 1


def test_returned_preview_cannot_change_frozen_candidate(private_settings):
    result = preview(private_settings, [service()])
    result["candidates"][0]["record"]["data"]["name"] = "Tampered after preview"
    submit(private_settings, result)
    assert context_payload(private_settings)["records"]["services"][0]["name"] == service()["name"]


def test_tokens_are_bound_to_actor_workspace_and_expiry(private_settings, tmp_path, monkeypatch):
    from workbench_atlas import imports

    result = preview(private_settings, [service()])
    with pytest.raises(ImportError, match="unavailable"):
        submit(private_settings, result, actor="Another operator")
    other = private_settings.model_copy(update={"context_db_path": tmp_path / "another.db"})
    with pytest.raises(ImportError, match="unavailable"):
        submit(other, result)
    now = imports.time.monotonic()
    monkeypatch.setattr(imports.time, "monotonic", lambda: now + imports.PREVIEW_TTL_SECONDS + 1)
    with pytest.raises(ImportError, match="unavailable"):
        submit(private_settings, result)


def test_duplicate_selection_replay_and_simultaneous_submit_are_rejected(private_settings, monkeypatch):
    result = preview(private_settings, [service()])
    entered, release = threading.Event(), threading.Event()
    from workbench_atlas.context import import_records

    def blocked_import(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return import_records(*args, **kwargs)

    monkeypatch.setattr("workbench_atlas.context.import_records", blocked_import)
    results = []
    worker = threading.Thread(target=lambda: results.append(submit(private_settings, result)))
    worker.start()
    assert entered.wait(5)
    try:
        with pytest.raises(ImportError, match="already being submitted"):
            submit(private_settings, result)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and results[0]["imported_count"] == 1
    with pytest.raises(ImportError, match="already submitted"):
        submit(private_settings, result)


def test_existing_records_require_explicit_current_revision(private_settings):
    submit(private_settings, preview(private_settings, [service()]))
    incoming = preview(private_settings, [service(name="An explicit proposed replacement")])
    with pytest.raises(ImportError, match="revision omitted"):
        submit(private_settings, incoming)
    row = service(name="An explicit revision-one replacement")
    row["expected_revision"] = 1
    assert submit(private_settings, preview(private_settings, [row]))["imported"][0]["revision"] == 2
    stale = preview(private_settings, [row])
    with pytest.raises(ImportError, match="record changed"):
        submit(private_settings, stale)


@pytest.mark.parametrize("content,format", [
    ('[{"name":"first","name":"second"}]', "json"),
    ('[{"value":NaN}]', "json"),
    ('{"tables":{"scenarios":[]}}', "json"),
    ('name,name\na,b\n', "csv"),
    ('name,owner\na,b,c\n', "csv"),
    ('name,owner\na\n', "csv"),
    ('[]', "json"),
    ('not supported', "xlsx"),
])
def test_ambiguous_or_unsupported_files_fail(private_settings, content, format):
    with pytest.raises(ImportError):
        preview_import(private_settings, content=content, format=format, actor="Synthetic operator",
                       source="Fictional source", kind="services")
    assert not private_settings.context_db_path.exists()


def test_duplicate_ids_in_preview_and_public_approval_fields_are_blocked(private_settings):
    result = preview(private_settings, [service(), service()])
    assert result["counts"]["invalid"] == 2
    invalid = service()
    invalid["review_status"] = "approved"
    result = preview(private_settings, [invalid])
    assert "review_status" in " ".join(result["candidates"][0]["errors"])
    with pytest.raises(ImportError, match="validation errors"):
        submit(private_settings, result)


def test_wrong_workspace_source_or_empty_selection_is_rejected(private_settings):
    with pytest.raises(ImportError, match="Enable"):
        preview(private_settings.model_copy(update={"context_enabled": False}), [service()])
    with pytest.raises(ImportError, match="separate private"):
        preview(private_settings.model_copy(update={"context_db_path": private_settings.scenario_db_path}), [service()])
    with pytest.raises(ImportError, match="Declare"):
        preview_import(private_settings, content="[]", format="json", actor="Synthetic operator", source="")
    result = preview(private_settings, [service()])
    with pytest.raises(ImportError, match="Choose"):
        submit_import(private_settings, preview_id=result["preview_id"], actor="Synthetic operator", selected_ids=[])


def test_limits_and_untrusted_text_never_execute(private_settings):
    with pytest.raises(ImportError, match="2 MB"):
        preview_import(private_settings, content="x" * (2 * 1024 * 1024 + 1), format="csv",
                       actor="Synthetic operator", source="Fictional source")
    with pytest.raises(ImportError, match="500"):
        preview(private_settings, [service(str(i)) for i in range(501)])
    result = preview(private_settings, [service(name='=HYPERLINK("https://example.com")')])
    submit(private_settings, result)
    assert context_payload(private_settings)["records"]["services"][0]["name"].startswith("=HYPERLINK")
