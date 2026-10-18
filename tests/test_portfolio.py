"""Read-only portfolio pages over synthetic temporary stores."""

import sqlite3
from urllib.parse import urlencode

import pytest

from contingency_atlas.web import portfolio
from contingency_atlas.web.portfolio import PortfolioError, build_scenario_page


@pytest.fixture
def catalog(settings):
    with sqlite3.connect(settings.scenario_db_path) as connection:
        connection.executemany(
            "INSERT INTO scenarios (scenario_id,title,description,risk_category,origin,status,urgency_score,created_year) VALUES (?,?,?,?,?,?,?,?)",
            [("SYN-A", "Payments outage", "Synthetic description " * 40, "Digital resilience", "synthetic_fixture", "active", 90, 2025),
             ("SYN-B", "Supplier outage", "Synthetic supplier dependency", "Digital resilience", "synthetic_fixture", "active", 90, 2024),
             ("SYN-C", "100% coverage", "Synthetic literal wildcard", "Processing integrity", "synthetic_fixture", "draft", 40, 2023),
             ("SYN-D", "Line_one", "Synthetic fixture", "Processing integrity", "operator_authored", "retired", None, 2022),
             ("SYN-E", "Unscored scenario", "Synthetic fixture", "Digital resilience", "legacy_unknown", None, None, None)],
        )
    return settings


def test_pages_partition_tied_scores_and_put_unscored_last(catalog):
    ids, offset = [], 0
    while offset is not None:
        result = build_scenario_page(catalog, f"limit=2&offset={offset}")
        ids.extend(item["id"] for item in result["scenarios"])
        assert result["page"]["total"] == 5
        offset = result["page"]["next_offset"]
    assert ids == ["SYN-A", "SYN-B", "SYN-C", "SYN-D", "SYN-E"]
    ascending = build_scenario_page(catalog, "sort=urgency&direction=asc")
    assert [item["id"] for item in ascending["scenarios"]] == ["SYN-C", "SYN-A", "SYN-B", "SYN-D", "SYN-E"]


def test_filters_preserve_unknown_provenance_and_implicit_active_status(catalog):
    result = build_scenario_page(catalog, urlencode({"status": "active", "category": "digital resilience"}))
    assert [item["id"] for item in result["scenarios"]] == ["SYN-A", "SYN-B", "SYN-E"]
    assert result["page"]["total"] == 3
    result = build_scenario_page(catalog, "origin=legacy_unknown&status=active")
    assert result["scenarios"][0]["origin"] == "legacy_unknown"
    assert result["scenarios"][0]["status"] == "active"


@pytest.mark.parametrize("query,ids", [
    ("%", ["SYN-C"]), ("_", ["SYN-D"]), ("supplier dependency", ["SYN-B"]),
    ("' OR 1=1 --", []),
])
def test_search_is_bound_literal_text_not_a_wildcard_or_sql(catalog, query, ids):
    assert [item["id"] for item in build_scenario_page(catalog, urlencode({"q": query}))["scenarios"]] == ids


def test_summary_excerpts_are_explicit_and_do_not_write_the_database(catalog):
    before = catalog.scenario_db_path.read_bytes()
    item = build_scenario_page(catalog, "q=Payments")["scenarios"][0]
    assert len(item["description_excerpt"]) == 400 and item["description_truncated"] is True
    assert "description" not in item and "judge_feedback" not in item
    assert catalog.scenario_db_path.read_bytes() == before


@pytest.mark.parametrize("query", [
    "limit=0", "limit=201", "limit=-1", "limit=1.5", "offset=-1", "offset=1000001",
    "limit=1&limit=2", "unknown=yes", "sort=title;DROP+TABLE+scenarios", "direction=sideways",
    "status=approved", "origin=guessed", "category=" + "a" * 161, "q=" + "a" * 201,
    "q=hello%00world", "q=%FF", "q=%0A", "q=%ZZ", "&".join("q=x" for _ in range(17)),
])
def test_invalid_parameters_fail_with_actionable_client_errors(catalog, query):
    with pytest.raises(PortfolioError) as error:
        build_scenario_page(catalog, query)
    assert error.value.status == 400


def test_empty_filtered_page_is_distinguished_from_storage_failure(catalog, tmp_path):
    result = build_scenario_page(catalog, "q=absent")
    assert result["scenarios"] == [] and result["page"]["total"] == 0
    missing = tmp_path / "missing.db"
    configured = catalog.model_copy(update={"scenario_db_path": missing})
    with pytest.raises(PortfolioError) as error:
        build_scenario_page(configured)
    assert error.value.status == 503 and not missing.exists()
    missing.write_text("Synthetic corrupt store")
    with pytest.raises(PortfolioError, match="unavailable"):
        build_scenario_page(configured)
    assert missing.read_text() == "Synthetic corrupt store"


def test_query_budget_fails_closed_without_a_partial_success(catalog, monkeypatch):
    monkeypatch.setattr(portfolio, "QUERY_TIMEOUT_SECONDS", -1)
    with pytest.raises(PortfolioError, match="time budget") as error:
        build_scenario_page(catalog)
    assert error.value.status == 503


def test_out_of_range_offset_returns_explicit_empty_page(catalog):
    result = build_scenario_page(catalog, "offset=50")
    assert result["page"] == {"offset": 50, "limit": 50, "returned": 0, "total": 5,
                               "has_more": False, "next_offset": None, "continuation_limited": False}


def test_offset_cap_never_returns_an_unusable_continuation(catalog, monkeypatch):
    monkeypatch.setattr(portfolio, "MAX_OFFSET", 2)
    page = build_scenario_page(catalog, "limit=2&offset=2")["page"]
    assert page["has_more"] is True and page["continuation_limited"] is True
    assert page["next_offset"] is None


def test_mixin_uses_routing_seam_without_bypassing_parent_authorization(catalog, monkeypatch):
    from contingency_atlas.web import server
    monkeypatch.setattr(server, "get_settings", lambda: catalog)
    class Handler(portfolio.PortfolioHandlerMixin):
        path = "/api/scenarios?limit=1"
        def _send_json(self, payload, status=200):
            self.result = payload, status
    handler = Handler()
    assert handler._portfolio_get("/api/data") is False
    assert handler._portfolio_get("/api/scenarios") is True
    assert handler.result[1] == 200 and handler.result[0]["page"]["returned"] == 1
