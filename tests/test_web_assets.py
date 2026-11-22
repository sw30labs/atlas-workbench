"""Static guards on the dashboard bundle.

The frontend has no execution coverage, so a runtime ReferenceError ships
silently: `node --check` passes on `StringfmtTs(x)` because it is perfectly
valid JavaScript — an undefined identifier called as a function. That exact
mangling reached main. It threw inside renderFindings(), which runs two lines
before the Evidence Events chart registration, so both charts were never drawn and
their canvases stretched to ~960px of empty panel.

These tests do not execute the page. They catch the shapes that a bulk edit
produces, which is the failure mode that actually happened.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INDEX = (
    Path(__file__).resolve().parents[1]
    / "workbench_atlas" / "web" / "static" / "index.html"
)


@pytest.fixture(scope="module")
def source() -> str:
    return INDEX.read_text(encoding="utf-8")


#: Helpers a bulk rename could glue an identifier onto.
HELPERS = ("fmtTs", "esc", "fmtInt")


@pytest.mark.parametrize("helper", HELPERS)
def test_helper_calls_are_not_glued_to_an_identifier(source, helper):
    """`String(fmtTs(x))` collapsing to `StringfmtTs(x)` is syntactically
    valid and fails only when the line runs."""
    glued = re.findall(rf"[A-Za-z0-9_$]{helper}\(", source)
    assert not glued, (
        f"{helper} is prefixed by an identifier in {len(glued)} place(s) — "
        "a call was glued onto the name by an edit"
    )


def test_every_called_helper_is_defined(source):
    """Cheap undefined-function check for the helpers this file relies on."""
    for helper in HELPERS:
        assert re.search(rf"(const|function)\s+{helper}\b", source), (
            f"{helper} is called but never defined"
        )


def test_chart_canvases_declare_a_height(source):
    """A canvas with only `width: 100%` in CSS scales its height by the
    intrinsic ratio, so an undrawn chart becomes a very tall empty panel
    rather than a small one. The height attribute is what registerChart
    reads as the logical height."""
    canvases = re.findall(r"<canvas[^>]*class=\"chart\"[^>]*>", source)
    assert canvases, "no chart canvases found — selector drifted"
    missing = [c for c in canvases if "height=" not in c]
    assert not missing, f"chart canvas without a height attribute: {missing}"


def test_every_playground_card_belongs_to_a_group(source):
    """A group listing an id that no card has would silently shunt real cards
    into the catch-all — the grouping must stay in step with PHASES."""
    real = set(re.findall(r'id: "([a-z0-9_]+)", tag:', source))
    block = re.search(r"const PLAYGROUND_GROUPS = \[(.*?)\n\];", source, re.S)
    assert block, "PLAYGROUND_GROUPS not found — the renderer changed shape"
    grouped = {
        card
        for ids in re.findall(r"ids: \[([^\]]*)\]", block.group(1))
        for card in re.findall(r'"([a-z0-9_]+)"', ids)
    }
    assert real, "no tagged playground cards found — selector drifted"
    assert not (grouped - real), f"group lists unknown card ids: {grouped - real}"
    assert not (real - grouped), f"card in no group: {real - grouped}"


def test_scenario_origin_controls_use_persisted_provenance(source):
    """The dashboard must not infer provenance from a scenario-id prefix."""
    for origin in (
        "operator_authored",
        "pipeline_generated",
        "compound_graduation",
        "synthetic_fixture",
        "legacy_unknown",
    ):
        assert f'value="{origin}"' in source
    assert 'startsWith("ATLAS-' not in source
    assert "scenarioOrigin(s) === scnState.origin" in source


def _nav_views(chunk):
    return re.findall(r'class="nav-btn[^"]*" data-view="([^"]+)"', chunk)


def test_primary_navigation_expresses_the_operator_loop(source):
    """The rail leads with the workbench path. Specialist screens stay collapsed."""
    nav = re.search(r'<nav class="side".*?</nav>', source, re.S)
    assert nav, "primary navigation not found"
    html = nav.group()
    primary, advanced_and_rest = html.split('<details class="nav-advanced">', 1)
    advanced, utilities = advanced_and_rest.split('<div class="nav-utilities">', 1)
    assert _nav_views(primary) == [
        "overview",
        "evidence",
        "review",
        "pipeline",
        "scenarios",
    ]
    assert _nav_views(advanced) == [
        "organization",
        "bulk",
        "compounds",
        "playground",
        "costs",
    ]
    assert _nav_views(utilities) == ["settings", "about"]
    assert "Discovery workflow" in primary
    for step in ("1", "2", "3", "4"):
        assert f'<span class="step">{step}</span>' in primary


def test_specialist_screens_are_contextual_workspace_tabs(source):
    tabs = set(re.findall(r'class="workspace-tab[^"]*"[^>]*data-view="([^"]+)"', source))
    assert tabs == {"evidence", "risks", "findings"}
    assert 'risks: "evidence", findings: "evidence"' in source
    assert 'playground: "pipeline", costs: "pipeline"' in source
    assert 'scenarios: "scenarios", compounds: "scenarios"' in source


def test_analyze_is_one_run_and_the_demo_library_is_labeled(source):
    pipeline = source.split('id="view-pipeline"', 1)[1].split('id="view-playground"', 1)[0]
    assert "RUN ANALYSIS" in pipeline
    assert 'id="run-all-btn"' in pipeline
    assert 'data-view="playground"' not in pipeline
    assert 'data-view="costs"' not in pipeline
    assert 'id="demo-banner"' in source
    assert "function corpusIsFictionalDemo" in source
    assert "function renderDemoBanner" in source


def test_today_prioritizes_next_action_over_duplicate_analytics(source):
    overview = source.split('id="view-overview"', 1)[1].split(
        'id="view-scenarios"', 1
    )[0]
    for marker in (
        'id="next-action"',
        'id="workflow-strip"',
        'id="recent-activity"',
        'id="ch-matrix"',
        'id="map-pause"',
        'id="map-reset"',
        'id="map-hot"',
        'data-map-mode="constellation"',
        'data-map-mode="radar"',
        'data-map-mode="cascade"',
        'id="map-shift-play"',
        'id="map-shift-range"',
        'id="portfolio-caption"',
    ):
        assert marker in overview
    for duplicate in ("ch-donut", "ch-hist", "ch-timeline", "ch-tokens"):
        assert f'id="{duplicate}"' not in overview
    telemetry = source.split('id="view-costs"', 1)[1].split(
        'id="view-settings"', 1
    )[0]
    assert 'id="ch-tokens"' in telemetry


def test_advanced_surfaces_use_progressive_disclosure(source):
    assert re.search(
        r'<details class="disclosure" id="phase-controls">.*?id="phase-cards"',
        source,
        re.S,
    )
    assert re.search(
        r'<details class="review-tools">.*?id="review-analyze-all".*?'
        r'id="review-research-all".*?id="review-scan-dupes"',
        source,
        re.S,
    )
    assert '<details class="disclosure" id="evidence-snapshot">' in source
    assert "Evidence &amp; scoring" in source
    assert "Full scenario record" in source


def test_view_history_supports_back_and_forward(source):
    assert 'history.pushState(null, "", viewHash)' in source
    assert 'addEventListener("popstate", scheduleRoute)' in source
    assert 'addEventListener("hashchange", scheduleRoute)' in source
    assert "function routeFromLocation()" in source


def test_routed_modals_close_back_to_their_origin(source):
    assert 'history.pushState({atlasModal: "scenario", returnView: currentView}' in source
    assert 'history.pushState({atlasModal: "run", returnView: currentView}' in source
    assert 'history.state.atlasModal === "scenario") history.back()' in source
    assert 'history.state.atlasModal === "run") history.back()' in source


def test_primary_navigation_and_run_modal_expose_accessibility_state(source):
    assert 'aria-current="page"' in source
    assert 'role="tablist"' in source
    assert 'role="tab" aria-selected=' in source
    assert 'id="run-modal-box" role="dialog" aria-modal="true"' in source
    assert 'id="run-x" aria-label="Close manual execution"' in source
    assert 'x.setAttribute("aria-selected", String(active))' in source


def test_queue_wide_research_is_not_limited_by_visible_review_segment(source):
    assert 'const eligible = allItems.filter(it =>' in source
    assert "HIDDEN BY FILTER" in source
    assert "DEEP RESEARCH QUEUE" in source


def test_static_markup_ids_are_unique(source):
    markup = source.split("<script>", 1)[0]
    ids = re.findall(r'\bid="([^"]+)"', markup)
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    assert not duplicates, f"duplicate static ids: {duplicates}"


def test_briefing_radar_and_timeshift_are_wired(source):
    script = source.split("<script>", 1)[1]
    for marker in (
        "function drawRadar",
        "function drawCascade",
        "function drawConstellation",
        "function livePairCompounds",
        "function scenarioEvidenceFill",
        "function citedEvidenceDays",
        "portfolioState.mode === \"radar\"",
        "cited public evidence lands",
        'data-map-mode="radar"',
        "3D PPI",
        "driver × horizon × urgency",
        "3D ORRERY",
        "shared phrases, not a joint score",
    ):
        assert marker in source, marker
    assert "Cross-driver convergence" in script
    assert "horizonBand" in script
    assert "one hop only" in source or "first hop only" in source
    assert "No k=3 score" in source or "no k=3 score" in source or "no triad score" in source
    assert "3D AND NET" in source
    assert "instrument_status" in source
    assert 'data-k="instrument_status"' in source
    assert 'portfolioState.mode === "constellation"' in script
    assert "Risk constellation · 3D orrery" in source
