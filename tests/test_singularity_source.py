"""Optional Singularity Atlas pull: relevance, rights, dedup, fail-soft."""

from __future__ import annotations

import json

from workbench_atlas.singularity_source import (
    collect_relevant_stories,
    compose_material,
    probe_singularity_atlas,
    record_source_job,
    relevance_for,
)
from workbench_atlas.web.intake_flow import prepare_research_material
from workbench_atlas.web.server import DashboardHandler


URL = "https://public.example/compute-outage"


def _story(**overrides):
    item = {
        "id": "story-001",
        "title": "Hyperscaler region outage and capacity rationing",
        "url": URL,
        "source": "public-feed",
        "summary": "A public cloud region disruption forced capacity rationing.",
        "origin": "feed",
        "published_at": "2026-08-29T12:00:00+00:00",
        "salience": 0.8,
        "vectors": {"security": 0.1, "compute": 0.7},
        "entities": ["Example Cloud"],
    }
    item.update(overrides)
    return item


def test_probe_does_not_raise_when_neo4j_is_missing(monkeypatch):
    import workbench_atlas.singularity_source as module

    def boom():
        raise RuntimeError("should not connect during import-fail probe")

    monkeypatch.setattr(module, "_fetch_stories_from_neo4j", boom)
    probe = probe_singularity_atlas()
    assert probe.available in {True, False}
    assert probe.reason
    assert "password" not in json.dumps(probe.to_dict()).casefold()


def test_archive_origin_is_excluded_on_rights_grounds(tmp_path):
    pull = collect_relevant_stories(
        include=True,
        workspace=tmp_path,
        fetch_stories=lambda: [_story(origin="archive", id="loop-1")],
    )
    assert pull.selected == []
    assert pull.skipped[0].decision == "skipped_rights"
    assert "all-rights-reserved" in pull.skipped[0].rationale


def test_private_url_is_not_admitted(tmp_path):
    pull = collect_relevant_stories(
        include=True,
        workspace=tmp_path,
        fetch_stories=lambda: [_story(url="http://127.0.0.1/internal")],
    )
    assert pull.skipped[0].decision == "skipped_provenance"


def test_compute_outage_is_selected_and_culture_is_not(tmp_path):
    pull = collect_relevant_stories(
        include=True,
        workspace=tmp_path,
        fetch_stories=lambda: [
            _story(),
            _story(
                id="story-culture",
                title="A concert in the park",
                url="https://public.example/concert",
                summary="People enjoyed music.",
                vectors={"culture": 0.9},
                entities=["Band"],
            ),
        ],
    )
    assert [item.story_id for item in pull.selected] == ["story-001"]
    assert "operational-risk overlap" in pull.selected[0].rationale
    culture = next(item for item in pull.skipped if item.story_id == "story-culture")
    assert culture.decision == "skipped_irrelevant"


def test_prepare_does_not_burn_unsubmitted_or_capped_stories(tmp_path):
    extra = _story(
        id="story-cap",
        title="Another hyperscaler outage and capacity rationing",
        url="https://public.example/compute-outage-2",
        summary="A second public cloud region disruption forced capacity rationing.",
    )
    first = collect_relevant_stories(
        include=True,
        workspace=tmp_path,
        fetch_stories=lambda: [_story(), extra],
        limit=1,
    )
    assert [item.story_id for item in first.selected] == ["story-001"]
    assert any(item.decision == "skipped_cap" for item in first.skipped)
    record_source_job(first, workspace=tmp_path, draft_id="draft-1")

    again = collect_relevant_stories(
        include=True,
        workspace=tmp_path,
        fetch_stories=lambda: [_story(), extra],
    )
    assert {item.story_id for item in again.selected} == {"story-001", "story-cap"}


def test_seen_and_corpus_urls_are_not_processed_twice(tmp_path):
    from workbench_atlas.singularity_source import record_submitted_stories

    first = collect_relevant_stories(
        include=True,
        workspace=tmp_path,
        fetch_stories=lambda: [_story()],
    )
    record_source_job(first, workspace=tmp_path, draft_id="draft-1")
    record_submitted_stories(
        [item.to_dict() for item in first.selected],
        workspace=tmp_path,
        draft_id="draft-1",
    )

    again = collect_relevant_stories(
        include=True,
        workspace=tmp_path,
        fetch_stories=lambda: [_story()],
    )
    assert again.selected == []
    assert again.skipped[0].decision == "skipped_seen"

    other = collect_relevant_stories(
        include=True,
        workspace=tmp_path / "other",
        corpus_urls=[URL],
        fetch_stories=lambda: [_story(id="story-dup")],
    )
    assert other.skipped[0].decision == "skipped_duplicate"


def test_fetch_failure_is_unavailable_not_an_exception(tmp_path):
    def boom():
        raise ConnectionError("neo4j down")

    pull = collect_relevant_stories(
        include=True,
        workspace=tmp_path,
        fetch_stories=boom,
    )
    assert pull.status == "unavailable"
    assert pull.selected == []
    assert "read failed" in pull.probe.reason


def test_opt_out_does_not_touch_the_graph(tmp_path):
    def boom():
        raise AssertionError("fetch must not run when the operator opts out")

    pull = collect_relevant_stories(
        include=False,
        workspace=tmp_path,
        fetch_stories=boom,
    )
    assert pull.status == "skipped"
    assert compose_material("pasted notes", pull) == "pasted notes"


def test_keyword_only_story_needs_more_than_one_weak_term():
    weak = relevance_for({
        "title": "A model card",
        "summary": "The authors published a model.",
        "vectors": {},
        "entities": [],
    })
    strong = relevance_for({
        "title": "Cloud vendor outage",
        "summary": "Third-party concentration disrupted settlement processing.",
        "vectors": {},
        "entities": [],
    })
    assert weak[0] is False
    assert strong[0] is True


def test_prepare_injects_selected_stories_and_writes_the_job_log(settings, tmp_workspace):
    settings = settings.model_copy(update={"workspace": tmp_workspace})
    class _Client:
        def chat_json(self, messages, **_kwargs):
            system = str(messages[0].get("content") or "")
            if "prepare DRAFT research intake" in system:
                return {
                    "candidates": [{
                        "kind": "evidence",
                        "title": "Hyperscaler region outage",
                        "text": "A public notice described a region outage and capacity rationing.",
                        "source_urls": [URL],
                        "input_unit_ids": ["unit-002"],
                        "classification_reason": "Observed infrastructure disruption.",
                        "record": {
                            "source_publisher": "Public Register",
                            "source_title": "Region outage",
                            "source_date": "2026-08-29",
                            "source_type": "public_reporting",
                        },
                    }],
                    "ignored": [{"unit_id": "unit-001", "reason": "source preamble"}],
                }
            if "semantic QA judge" in system:
                return {"verdict": "pass", "issues": []}
            raise AssertionError(system[:80])

    payload = prepare_research_material(
        "",
        settings=settings,
        client=_Client(),
        include_singularity_atlas=True,
        singularity_fetch=lambda: [_story()],
    )
    assert payload["singularity_atlas"]["summary"]["selected"] == 1
    assert payload["singularity_atlas"]["selected"][0]["rationale"]
    jobs = (tmp_workspace / "singularity_intake_jobs.jsonl").read_text(encoding="utf-8")
    assert "story-001" in jobs
    seen_path = tmp_workspace / "singularity_intake_seen.jsonl"
    if seen_path.is_file():
        assert "story-001" not in seen_path.read_text(encoding="utf-8")
    assert any(item["kind"] == "evidence" for item in payload["items"])


def test_prepare_without_paste_fails_soft_when_source_is_empty(settings):
    try:
        prepare_research_material(
            "  ",
            settings=settings,
            client=object(),
            include_singularity_atlas=True,
            singularity_fetch=lambda: [],
        )
    except ValueError as exc:
        assert "no eligible" in str(exc).casefold()
    else:
        raise AssertionError("expected a ValueError when the optional source is empty")


def test_sources_route_never_500s():
    payload = {}

    class _Handler(DashboardHandler):
        def __init__(self):
            self.path = "/api/intake/sources"
            self.headers = {}

        def _send_json(self, data, status=200):
            payload["status"] = status
            payload["data"] = data

        def log_message(self, *_args):
            return

    _Handler()._handle_intake_sources()
    assert payload["status"] == 200
    assert payload["data"]["singularity_atlas"]["available"] in {True, False}
    assert "reason" in payload["data"]["singularity_atlas"]
