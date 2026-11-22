"""Deep research: DDG parsing, capped fetching, the bounded loop, adopt-refs.

The loop is exercised offline with injected search/fetch/LLM fakes. The
invariants under test: proposals only (candidates always land pending),
the model can never steer fetches to URLs the search step did not return,
"no evidence found" is a first-class verdict, and refs can be adopted
only from fully approved evidence onto a still-pending risk.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from workbench_atlas.config import Settings
from workbench_atlas.research import (
    ddg_search,
    fetch_page,
    load_research_memos,
    run_deep_research,
    run_deep_research_all,
    store_research_memo,
)
from workbench_atlas.schema import migrate, migrate_evidence_events
from workbench_atlas.web import runner, server
from workbench_atlas.web.server import DashboardHandler

ROOT = Path(__file__).resolve().parents[1]

DDG_FIXTURE = """
<html><body>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnews.stanford.edu%2Ftalk&amp;rut=abc">Stanford <b>talk</b></a>
<a class="result__snippet" href="#">The economy of the AI super cycle</a>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=http%3A%2F%2F192.0.2.10%2Fnot-public&amp;rut=def">non-public thing (RFC 5737 TEST-NET)</a>
<a class="result__snippet" href="#">should be filtered</a>
<a rel="nofollow" class="result__a" href="https://example.org/direct">Direct link</a>
<a class="result__snippet" href="#">no redirect wrapper</a>
</body></html>
"""


def _transport(pages: dict[str, httpx.Response]):
    def handle(request: httpx.Request) -> httpx.Response:
        key = str(request.url).split("?")[0]
        if key in pages:
            return pages[key]
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handle)


def _excluded_host() -> str:
    return "".join(("o", "rx", ".org"))


def test_ddg_search_parses_unwraps_and_filters():
    transport = _transport({
        "https://html.duckduckgo.com/html/": httpx.Response(
            200, text=DDG_FIXTURE, headers={"content-type": "text/html"}),
    })
    results = ddg_search("anything", transport=transport)
    urls = [r["url"] for r in results]
    assert urls == ["https://news.stanford.edu/talk", "https://example.org/direct"]
    assert results[0]["title"] == "Stanford talk"
    assert "super cycle" in results[0]["snippet"]


def test_ddg_search_drops_excluded_source_results():
    excluded = f'<a class="result__a" href="https://{_excluded_host()}/item">x</a>'
    transport = _transport({
        "https://html.duckduckgo.com/html/": httpx.Response(
            200,
            text=f"<html><body>{excluded}</body></html>",
            headers={"content-type": "text/html"},
        ),
    })
    assert ddg_search("anything", transport=transport) == []


def test_fetch_page_blocks_private_redirects():
    transport = _transport({
        "https://ok.example.com/a": httpx.Response(
            302, headers={"location": "http://127.0.0.1/internal"}),
    })
    with pytest.raises(ValueError, match="non-public"):
        fetch_page("https://ok.example.com/a", transport=transport)


def test_fetch_page_blocks_excluded_initial_and_redirect_hosts():
    excluded_url = f"https://{_excluded_host()}/source"
    with pytest.raises(ValueError, match="excluded"):
        fetch_page(excluded_url)

    transport = _transport({
        "https://ok.example.com/a": httpx.Response(
            302,
            headers={"location": excluded_url},
        ),
    })
    with pytest.raises(ValueError, match="excluded"):
        fetch_page("https://ok.example.com/a", transport=transport)


def test_fetch_page_extracts_text_and_title():
    html = "<html><head><title>My Title</title><script>evil()</script></head><body><p>Real   text</p></body></html>"
    transport = _transport({
        "https://ok.example.com/page": httpx.Response(
            200, text=html, headers={"content-type": "text/html"}),
    })
    page = fetch_page("https://ok.example.com/page", transport=transport)
    assert page["title"] == "My Title"
    assert "Real text" in page["text"] and "evil" not in page["text"]


def _minimal_pdf() -> bytes:
    from pypdf import PdfWriter
    import io
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_fetch_page_accepts_pdf():
    transport = _transport({
        "https://bis.example.com/publ/report.pdf": httpx.Response(
            200, content=_minimal_pdf(),
            headers={"content-type": "application/pdf"}),
    })
    page = fetch_page("https://bis.example.com/publ/report.pdf",
                      transport=transport)
    assert page["url"] == "https://bis.example.com/publ/report.pdf"
    assert isinstance(page["text"], str) and page["bytes"] > 0


def test_fetch_page_bad_pdf_is_a_value_error():
    transport = _transport({
        "https://bis.example.com/publ/broken.pdf": httpx.Response(
            200, content=b"%PDF-1.7 garbage bytes",
            headers={"content-type": "application/pdf"}),
    })
    with pytest.raises(ValueError, match="PDF"):
        fetch_page("https://bis.example.com/publ/broken.pdf", transport=transport)


# ── loop fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def research_settings(tmp_path, monkeypatch):
    risks = [{
        "issue": "Stranded-energy dependency for AI infrastructure",
        "description": "needs evidence", "source_type": "public_research",
        "provenance_note": "fixture", "review_status": "pending",
        "evidence_refs": ["https://epoch.ai/unrelated"],
    }]
    (tmp_path / "emerging_risks.json").write_text(json.dumps(risks))
    migrate_evidence_events(str(tmp_path / "ev.db"))
    migrate(str(tmp_path / "atlas.db"))
    settings = Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=tmp_path / "ev.db",
        emerging_risks_path=tmp_path / "emerging_risks.json",
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


RISK_ID = "Stranded-energy dependency for AI infrastructure"


class _FS:
    llm_model_judge = "fake-judge"


class ScriptedClient:
    """LLM fake driving the loop; set find_evidence=False for the dry path."""

    settings = _FS()

    def __init__(self, find_evidence: bool = True):
        self.find_evidence = find_evidence

    def chat_json(self, messages, model=None, temperature=0.0):
        text = messages[-1]["content"]
        if "Step: PLAN" in text:
            return {"queries": ["stanford stranded energy"]}
        if "Step: TRIAGE" in text:
            return {"fetch": ["https://news.stanford.edu/talk",
                              "https://model-invented.example.com/x"]}
        if "Step: ASSESS" in text:
            if not self.find_evidence:
                return {"relevant": False, "supports": [], "refutes": [],
                        "candidate_evidence": None, "note": "off-topic"}
            return {"relevant": True, "supports": ["refs resolve"], "refutes": [],
                    "candidate_evidence": {
                        "headline": "Stanford talk on AI supercycle energy",
                        "source_publisher": "Stanford University",
                        "source_title": "The Economy of the AI Super Cycle",
                        "source_date": "2026-04-12",
                        "independently_authored_summary":
                            "A Stanford lecture describes stranded-energy siting "
                            "for AI datacenters and its reliability trade-offs.",
                        "risk_category": "Digital resilience"},
                    "note": "on-topic"}
        if "Step: JUDGE" in text:
            verdict = "enough" if self.find_evidence else "no_evidence_found"
            return {"coverage": [{"flag": "refs", "status":
                                  "covered" if self.find_evidence else "uncovered"}],
                    "verdict": verdict, "note": "scripted"}
        if "Step: DRAFT_REASON" in text:
            return {"draft_reason":
                    "Research corroborates the proposed mechanism; "
                    "see https://news.stanford.edu/talk."}
        return {}


def _fake_search(query, **kwargs):
    return [{"title": "Stanford talk", "url": "https://news.stanford.edu/talk",
             "snippet": "AI supercycle energy"}]


def _fake_fetch(url, **kwargs):
    assert url == "https://news.stanford.edu/talk", \
        f"model-invented URL reached the fetcher: {url}"
    return {"url": url, "title": "The Economy of the AI Super Cycle",
            "text": "stranded energy for ai datacenters " * 40, "bytes": 999}


def test_loop_creates_pending_candidate_and_memo(research_settings):
    result = run_deep_research(
        research_settings, ScriptedClient(), kind="risk", record_id=RISK_ID,
        seed_domains=["stanford.edu"], search=_fake_search, page_fetch=_fake_fetch,
    )
    assert result["verdict"] == "enough"
    assert result["candidates_created"] == 1 and not result["errors"]

    conn = sqlite3.connect(research_settings.evidence_db_path)
    event_id, status, note, fingerprint = conn.execute(
        "SELECT event_id, review_status, provenance_note, content_fingerprint FROM evidence_events"
    ).fetchone()
    conn.close()
    assert status == "pending"                      # proposals only — never approved
    assert "deep_research" in note
    assert event_id == "EVT-DR-" + fingerprint[:12]

    memo = load_research_memos(research_settings.scenario_db_path)[("risk", RISK_ID)]
    assert memo["verdict"] == "enough"
    assert memo["candidates"] == [event_id]
    assert memo["coverage"][0]["status"] == "covered"


def test_research_candidates_carry_classification_columns(research_settings):
    """Candidate classes are closed-vocabulary values or explicitly absent."""
    from workbench_atlas.categories import (
        EVENT_DRIVER_CLASSES,
        PRIMARY_CAUSE_CLASSES,
        REGIONS,
    )

    run_deep_research(
        research_settings, ScriptedClient(), kind="risk", record_id=RISK_ID,
        seed_domains=["stanford.edu"], search=_fake_search, page_fetch=_fake_fetch,
    )
    conn = sqlite3.connect(research_settings.evidence_db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM evidence_events").fetchone()
    conn.close()
    assert set(row.keys()) >= {"event_driver_class", "primary_cause_class", "region", "country"}
    assert row["event_driver_class"] is None or row["event_driver_class"] in EVENT_DRIVER_CLASSES
    assert row["primary_cause_class"] is None or row["primary_cause_class"] in PRIMARY_CAUSE_CLASSES
    assert row["region"] is None or row["region"] in REGIONS


def test_research_candidate_rejects_out_of_vocabulary_classification():
    """A value the assessor invents must not reach the store as if it were
    part of the taxonomy."""
    from workbench_atlas.research import _candidate_event

    page = {"url": "https://example.gov/a", "title": "T", "text": "x"}
    event = _candidate_event(
        page,
        {
            "headline": "Cloud identity outage",
            "independently_authored_summary": "A cloud identity system failed.",
            "risk_category": "Digital resilience",
            "event_driver_class": "Something the model invented",
            "primary_cause_class": "Also invented",
        },
        "risk", "r1", "note",
    )
    assert event["event_driver_class"] is None
    assert event["primary_cause_class"] is None

    valid = _candidate_event(
        page,
        {
            "headline": "Cloud identity outage",
            "independently_authored_summary": "A cloud identity system failed.",
            "event_driver_class": "Technology disruption",
            "primary_cause_class": "Access control",
        },
        "risk", "r1", "note",
    )
    assert valid["event_driver_class"] == "Technology disruption"
    assert valid["primary_cause_class"] == "Access control"


def test_research_candidate_region_is_normalized_or_dropped():
    """Geography cannot be derived from risk language, so an unrecognized
    region becomes None rather than a confident wrong answer."""
    from workbench_atlas.research import _candidate_event

    page = {"url": "https://example.gov/a", "title": "T", "text": "x"}
    base = {
        "headline": "Cloud identity outage",
        "independently_authored_summary": "A cloud identity system failed.",
        "risk_category": "Digital resilience",
    }
    casing = _candidate_event(page, {**base, "region": "north america"},
                              "risk", "r1", "note")
    assert casing["region"] == "North America"
    junk = _candidate_event(page, {**base, "region": "Atlantis"},
                            "risk", "r1", "note")
    assert junk["region"] is None


def test_candidate_and_memo_storage_reject_excluded_sources(research_settings):
    from workbench_atlas.research import _candidate_event

    excluded_url = f"https://{_excluded_host()}/source"
    page = {"url": excluded_url, "title": "Excluded", "text": "x"}
    with pytest.raises(ValueError, match="excluded"):
        _candidate_event(
            page,
            {"independently_authored_summary": "Synthetic summary."},
            "risk",
            "fixture-risk",
            "fixture",
        )
    with pytest.raises(ValueError, match="excluded"):
        store_research_memo(
            research_settings.scenario_db_path,
            "risk",
            "fixture-risk",
            "fixture-model",
            {"findings": [{"url": excluded_url}]},
        )


@pytest.mark.parametrize(
    "field",
    (
        "source_publisher",
        "source_title",
        "headline",
        "independently_authored_summary",
        "risk_category",
        "country",
    ),
)
def test_candidate_rejects_excluded_host_in_model_authored_fields(field):
    from workbench_atlas.research import _candidate_event

    draft = {"independently_authored_summary": "Synthetic safe summary."}
    draft[field] = f"Reported by {_excluded_host()}"
    with pytest.raises(ValueError, match="excluded source host"):
        _candidate_event(
            {
                "url": "https://example.org/public-source",
                "title": "Synthetic source",
                "text": "Synthetic page text.",
            },
            draft,
            "risk",
            "fixture-risk",
            "fixture",
        )


def test_loop_reruns_deduplicate_candidates(research_settings):
    for _ in range(2):
        result = run_deep_research(
            research_settings, ScriptedClient(), kind="risk", record_id=RISK_ID,
            search=_fake_search, page_fetch=_fake_fetch,
        )
    assert result["candidates_created"] == 0 and result["candidates_duplicate"] == 1
    conn = sqlite3.connect(research_settings.evidence_db_path)
    assert conn.execute("SELECT COUNT(*) FROM evidence_events").fetchone()[0] == 1
    conn.close()


def test_no_evidence_found_is_first_class(research_settings):
    result = run_deep_research(
        research_settings, ScriptedClient(find_evidence=False),
        kind="risk", record_id=RISK_ID,
        search=_fake_search, page_fetch=_fake_fetch,
    )
    assert result["verdict"] == "no_evidence_found"
    assert result["candidates_created"] == 0
    memo = load_research_memos(research_settings.scenario_db_path)[("risk", RISK_ID)]
    assert memo["verdict"] == "no_evidence_found"
    assert memo["findings"][0]["relevant"] is False


def test_excluded_search_results_never_reach_fetch_candidate_or_memo(
    research_settings,
):
    excluded_url = f"https://{_excluded_host()}/source"
    fetched: list[str] = []

    def search(_query):
        return [{"title": "Excluded", "url": excluded_url, "snippet": "x"}]

    def page_fetch(url):
        fetched.append(url)
        raise AssertionError("excluded source reached fetch")

    result = run_deep_research(
        research_settings,
        ScriptedClient(),
        kind="risk",
        record_id=RISK_ID,
        search=search,
        page_fetch=page_fetch,
    )
    assert result["verdict"] == "no_evidence_found"
    assert result["pages_fetched"] == 0
    assert fetched == []
    memo = load_research_memos(research_settings.scenario_db_path)[
        ("risk", RISK_ID)
    ]
    assert excluded_url not in json.dumps(memo)


def test_pending_endpoint_attaches_memo_with_candidate_status(research_settings):
    run_deep_research(research_settings, ScriptedClient(), kind="risk",
                      record_id=RISK_ID, search=_fake_search, page_fetch=_fake_fetch)
    queue = server.build_review_queue()
    entry = next(e for e in queue["risks"] if e["record_id"] == RISK_ID)
    memo = entry["research"]
    assert memo["verdict"] == "enough"
    assert memo["candidate_status"][0]["review_status"] == "pending"


# ── channel proposals: gated on pre-analysis, memo-only, draft reason ────


def test_clip_reason_never_cuts_mid_sentence():
    from workbench_atlas.research import _clip_reason
    short = "Two sentences. All here."
    assert _clip_reason(short) == short
    long = ("A complete sentence. " * 90) + "an unfinished tail"
    clipped = _clip_reason(long)
    assert len(clipped) <= 1500
    assert clipped.endswith(".")


def _channel_proposal(settings, **overrides) -> str:
    from workbench_atlas.proposals import create_channel_proposals
    cand = {
        "kind": "edge_add", "risk_ids": ["atom-a", "atom-b"], "channel": "cloud",
        "mechanism": "fixture transmission mechanism",
        "citation_url": "https://example.com/r.pdf", "citation_status": "resolved",
    }
    cand.update(overrides)
    result = create_channel_proposals(settings.scenario_db_path, [cand])
    assert result["created"] == 1
    return result["proposal_ids"][0]


def _store_channel_explanation(settings, pid) -> None:
    from workbench_atlas.proposals import get_channel_proposal
    from workbench_atlas.review_assist import content_hash, store_suggestion
    record = get_channel_proposal(settings.scenario_db_path, pid)
    store_suggestion(
        settings.scenario_db_path, "channel", pid,
        content_hash("channel", record),
        {"recommendation": None, "explanation": "fixture explanation",
         "weaknesses": ["mechanism under-specified"],
         "model_id": "fake-judge", "error": None},
    )


def test_channel_research_requires_explanation_first(research_settings):
    pid = _channel_proposal(research_settings)
    with pytest.raises(ValueError, match="pre-analysis"):
        run_deep_research(
            research_settings, ScriptedClient(), kind="channel", record_id=pid,
            search=_fake_search, page_fetch=_fake_fetch,
        )


def test_channel_research_is_memo_only_with_draft_reason(research_settings):
    pid = _channel_proposal(research_settings)
    _store_channel_explanation(research_settings, pid)
    result = run_deep_research(
        research_settings, ScriptedClient(), kind="channel", record_id=pid,
        search=_fake_search, page_fetch=_fake_fetch,
    )
    assert result["verdict"] == "enough"
    assert result["candidates_created"] == 0
    # channels have no refs to adopt — nothing is filed into the evidence
    # store even though the fake ASSESS step drafted a candidate
    conn = sqlite3.connect(research_settings.evidence_db_path)
    assert conn.execute("SELECT COUNT(*) FROM evidence_events").fetchone()[0] == 0
    conn.close()
    memo = load_research_memos(research_settings.scenario_db_path)[("channel", pid)]
    assert memo["verdict"] == "enough" and memo["candidates"] == []
    # the explanation's weaknesses steer the research as red flags
    assert any("mechanism under-specified" in f for f in memo["red_flags"])
    assert "news.stanford.edu" in memo["draft_reason"]

    queue = server.build_review_queue()
    entry = next(e for e in queue["channel_proposals"]["pending"]
                 if e["record_id"] == pid)
    assert entry["research"]["draft_reason"] == memo["draft_reason"]


# ── adopt-refs endpoint ──────────────────────────────────────────────────


@contextmanager
def _dashboard():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _post(port, path, body, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "POST", path, body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def _approve_candidate(settings, event_id):
    from workbench_atlas.review import approve_evidence

    conn = sqlite3.connect(settings.evidence_db_path)
    conn.execute(
        "UPDATE evidence_events SET source_locator = '', taxonomy_version = 'atlas-1.0'"
        " WHERE event_id = ?", (event_id,),
    )
    conn.commit()
    conn.close()
    approve_evidence(settings.evidence_db_path, event_id,
                     public_source_attested=True, rights_attested=True)


def test_adopt_refs_requires_approved_evidence(research_settings):
    run_deep_research(research_settings, ScriptedClient(), kind="risk",
                      record_id=RISK_ID, search=_fake_search, page_fetch=_fake_fetch)
    memo = load_research_memos(research_settings.scenario_db_path)[("risk", RISK_ID)]
    event_id = memo["candidates"][0]

    with _dashboard() as port:
        status, payload = _post(port, "/api/review/adopt-refs",
                                {"record_id": RISK_ID, "event_ids": [event_id]})
        assert status == 400 and "only approved evidence" in payload["error"]

        _approve_candidate(research_settings, event_id)
        status, payload = _post(port, "/api/review/adopt-refs",
                                {"record_id": RISK_ID, "event_ids": [event_id]})
        assert status == 200 and payload["added"] == [event_id]
        # idempotent: adopting again adds nothing
        status, payload = _post(port, "/api/review/adopt-refs",
                                {"record_id": RISK_ID, "event_ids": [event_id]})
        assert status == 200 and payload["added"] == []

    saved = json.loads(research_settings.emerging_risks_path.read_text())
    record = next(r for r in saved if r["issue"] == RISK_ID)
    assert event_id in record["evidence_refs"]
    assert "adopted after human approval" in record["provenance_note"]


def test_adopt_refs_rejects_unknown_and_cross_origin(research_settings):
    with _dashboard() as port:
        status, payload = _post(port, "/api/review/adopt-refs",
                                {"record_id": RISK_ID, "event_ids": ["EVT-NOPE"]})
        assert status == 400 and "no such evidence" in payload["error"]
        status, _ = _post(port, "/api/review/adopt-refs",
                          {"record_id": RISK_ID, "event_ids": ["EVT-NOPE"]},
                          headers={"Origin": "http://evil.example"})
        assert status == 403


# ── detach-refs endpoint ─────────────────────────────────────────────────


@pytest.fixture
def detach_settings(tmp_path, monkeypatch):
    risks = [{
        "issue": RISK_ID,
        "description": "needs evidence", "source_type": "public_research",
        "provenance_note": "fixture", "review_status": "pending",
        "evidence_refs": ["https://a.example/1", "https://b.example/2", "EVT-X"],
        "evidence_strength": "strong",
    }]
    (tmp_path / "emerging_risks.json").write_text(json.dumps(risks))
    migrate_evidence_events(str(tmp_path / "ev.db"))
    migrate(str(tmp_path / "atlas.db"))
    settings = Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=tmp_path / "ev.db",
        emerging_risks_path=tmp_path / "emerging_risks.json",
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


def _saved_risk(settings, issue=RISK_ID):
    saved = json.loads(settings.emerging_risks_path.read_text())
    return next(r for r in saved if r["issue"] == issue)


def test_detach_refs_removes_stamps_and_is_idempotent(detach_settings):
    from workbench_atlas.categories import derive_evidence_strength
    with _dashboard() as port:
        status, payload = _post(port, "/api/review/detach-refs", {
            "record_id": RISK_ID,
            "refs": ["https://a.example/1", "EVT-X"],
        })
        assert status == 200
        assert sorted(payload["removed"]) == ["EVT-X", "https://a.example/1"]

        saved = _saved_risk(detach_settings)
        assert saved["evidence_refs"] == ["https://b.example/2"]
        assert saved["review_status"] == "pending"
        assert "Evidence refs detached by human review" in saved["provenance_note"]
        # strength is derived from the refs — recomputed, not left stale
        assert saved["evidence_strength"] == derive_evidence_strength(
            ["https://b.example/2"])

        # idempotent: detaching what is already gone removes nothing
        status, payload = _post(port, "/api/review/detach-refs", {
            "record_id": RISK_ID, "refs": ["EVT-X"],
        })
        assert status == 200 and payload["removed"] == []
        assert _saved_risk(detach_settings)["evidence_refs"] == ["https://b.example/2"]


def test_detach_refs_guards(detach_settings):
    with _dashboard() as port:
        status, _ = _post(port, "/api/review/detach-refs",
                          {"record_id": RISK_ID, "refs": []})
        assert status == 400
        status, payload = _post(port, "/api/review/detach-refs",
                                {"record_id": "no such risk", "refs": ["x"]})
        assert status == 404 and "no emerging risk" in payload["error"]
        status, _ = _post(port, "/api/review/detach-refs",
                          {"record_id": RISK_ID, "refs": ["x"]},
                          headers={"Origin": "http://evil.example"})
        assert status == 403

    # approved risks are frozen — detach is a pending-review action
    saved = _saved_risk(detach_settings)
    saved["review_status"] = "approved"
    detach_settings.emerging_risks_path.write_text(json.dumps([saved]))
    with _dashboard() as port:
        status, payload = _post(port, "/api/review/detach-refs",
                                {"record_id": RISK_ID, "refs": ["EVT-X"]})
        assert status == 400 and "pending" in payload["error"]


def test_review_research_is_a_valid_runner_phase():
    assert "review_research" in runner.VALID_PHASES


def test_index_has_research_ui():
    html = (ROOT / "workbench_atlas/web/static/index.html").read_text()
    assert "data-rv-research" in html
    assert "data-rv-adopt" in html
    assert "/api/review/adopt-refs" in html
    assert 'id="review-research-all"' in html
    assert "review_research_all" in html
    assert "NO CREDIBLE EVIDENCE FOUND" in html


def test_index_adopt_offer_excludes_attached_refs():
    """Already-adopted candidates must not be re-offered: the endpoint is
    idempotent, so re-offering them makes ADOPT a silent no-op."""
    html = (ROOT / "workbench_atlas/web/static/index.html").read_text()
    assert "!attached.has(String(c.event_id))" in html
    assert 'st-ok">adopted' in html


def test_index_has_detach_refs_ui():
    """Ref removal is a first-class review action: per-ref button plus a
    bulk detach of refs that resolve to no evidence record."""
    html = (ROOT / "workbench_atlas/web/static/index.html").read_text()
    assert "data-rv-detach" in html
    assert "/api/review/detach-refs" in html
    assert "DETACH" in html


# ── batch deep research: only records with a fresh pre-analysis ──────────

OTHER_RISK_ID = "Undersea cable chokepoint concentration"


@pytest.fixture
def batch_settings(tmp_path, monkeypatch):
    risks = [
        {"issue": RISK_ID,
         "description": "needs evidence", "source_type": "public_research",
         "provenance_note": "fixture", "review_status": "pending",
         "evidence_refs": ["https://epoch.ai/unrelated"]},
        {"issue": OTHER_RISK_ID,
         "description": "thin base", "source_type": "public_research",
         "provenance_note": "fixture", "review_status": "pending",
         "evidence_refs": ["https://example.org/cables"]},
    ]
    (tmp_path / "emerging_risks.json").write_text(json.dumps(risks))
    migrate_evidence_events(str(tmp_path / "ev.db"))
    migrate(str(tmp_path / "atlas.db"))
    settings = Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=tmp_path / "ev.db",
        emerging_risks_path=tmp_path / "emerging_risks.json",
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


def _store_risk_suggestion(settings, issue, chash=None, **overrides):
    """A pre-analysis row as run_review_analysis would have stored it."""
    from workbench_atlas.review import list_risks_for_review
    from workbench_atlas.review_assist import content_hash, store_suggestion
    record = next(r for r in list_risks_for_review(settings.emerging_risks_path)
                  if r["issue"] == issue)
    analysis = {"recommendation": "approve", "confidence": 0.7,
                "reasons": ["fixture"], "concerns": [],
                "model_id": "fake-judge", "error": None}
    analysis.update(overrides)
    store_suggestion(settings.scenario_db_path, "risk", issue,
                     chash or content_hash("risk", record), analysis)


def _risk_content_hash(settings, issue):
    from workbench_atlas.review import list_risks_for_review
    from workbench_atlas.review_assist import content_hash
    record = next(r for r in list_risks_for_review(settings.emerging_risks_path)
                  if r["issue"] == issue)
    return content_hash("risk", record)


def test_batch_researches_only_records_with_fresh_preanalysis(batch_settings):
    _store_risk_suggestion(batch_settings, RISK_ID)
    result = run_deep_research_all(
        batch_settings, ScriptedClient(),
        search=_fake_search, page_fetch=_fake_fetch,
    )
    assert result["eligible"] == 1
    assert result["researched"] == 1
    assert result["skipped_no_preanalysis"] == 1
    assert result["failed"] == 0 and result["deferred"] == 0
    assert result["verdicts"] == {f"risk:{RISK_ID}": "enough"}

    memos = load_research_memos(batch_settings.scenario_db_path)
    assert ("risk", RISK_ID) in memos
    assert ("risk", OTHER_RISK_ID) not in memos
    # the memo records which content it covers, so reruns can skip it
    assert memos[("risk", RISK_ID)]["record_content_hash"] == \
        _risk_content_hash(batch_settings, RISK_ID)


def test_batch_skips_fresh_memo_unless_forced(batch_settings):
    _store_risk_suggestion(batch_settings, RISK_ID)
    run_deep_research_all(batch_settings, ScriptedClient(),
                          search=_fake_search, page_fetch=_fake_fetch)
    again = run_deep_research_all(batch_settings, ScriptedClient(),
                                  search=_fake_search, page_fetch=_fake_fetch)
    assert again["researched"] == 0
    assert again["skipped_has_memo"] == 1
    forced = run_deep_research_all(batch_settings, ScriptedClient(), force=True,
                                   search=_fake_search, page_fetch=_fake_fetch)
    assert forced["researched"] == 1


def test_batch_ignores_stale_or_failed_suggestions(batch_settings):
    _store_risk_suggestion(batch_settings, RISK_ID, chash="stale-hash")
    _store_risk_suggestion(batch_settings, OTHER_RISK_ID, error="model blew up")
    result = run_deep_research_all(batch_settings, ScriptedClient(),
                                   search=_fake_search, page_fetch=_fake_fetch)
    assert result["researched"] == 0
    assert result["skipped_no_preanalysis"] == 2


def test_batch_collects_per_record_failures(batch_settings, monkeypatch):
    _store_risk_suggestion(batch_settings, RISK_ID)
    _store_risk_suggestion(batch_settings, OTHER_RISK_ID)

    from workbench_atlas import research

    def flaky(settings, client, *, kind, record_id, **kwargs):
        if record_id == RISK_ID:
            raise RuntimeError("search backend down")
        return {"verdict": "enough"}

    monkeypatch.setattr(research, "run_deep_research", flaky)
    result = research.run_deep_research_all(batch_settings, ScriptedClient())
    assert result["researched"] == 1
    assert result["failed"] == 1
    assert RISK_ID in result["errors"][0]
    assert "search backend down" in result["errors"][0]


def test_batch_defers_records_beyond_the_cap(batch_settings):
    _store_risk_suggestion(batch_settings, RISK_ID)
    _store_risk_suggestion(batch_settings, OTHER_RISK_ID)
    result = run_deep_research_all(
        batch_settings, ScriptedClient(), max_records=1,
        search=_fake_search, page_fetch=_fake_fetch,
    )
    assert result["eligible"] == 2
    assert result["researched"] == 1
    assert result["deferred"] == 1


def test_review_research_all_is_a_valid_runner_phase():
    assert "review_research_all" in runner.VALID_PHASES
