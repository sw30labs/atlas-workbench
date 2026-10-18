"""Tests for artifact ingestion: convert, chunk-map extract, merge lanes,
rubric check, and the /api/ingest/convert endpoint."""

import base64
import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from contingency_atlas.ingest_artifacts import (
    chunk_markdown,
    convert_to_markdown,
    decode_artifact,
    extract_evidence,
    harvest_literal_urls,
    merge_extractions,
    rubric_check,
)
from contingency_atlas.web.server import DashboardHandler


class ExtractMockClient:
    """Returns per-chunk extraction JSON and rubric JSON by content."""

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "completeness judge" in messages[0]["content"]:
            return {
                "missed_sources": [],
                "attribution_fidelity": {"verdict": "pass", "issues": []},
                "body_fidelity": {"verdict": "pass", "issues": []},
                "load_bearing_facts": {"who": "present", "what": "present",
                                       "when": "present", "scale": "present", "impact": "present"},
            }
        if "invalid" in user:
            # repair attempt
            return {"records": [{"text": "repaired note", "url": None}]}
        return {
            "records": [
                # paired: this URL is literally in the chunk
                {"text": "A cloud identity advisory was issued.",
                 "url": "https://cisa.example/adv-1",
                 "publisher": "CISA", "confidence": "high"},
                # unattributed: named source, no URL in the text
                {"text": "The CISA advisory was referenced without a link.",
                 "url": None, "publisher": "CISA", "confidence": "high"},
                # must be refused: a private address is not a public source
                {"text": "Internal note.", "url": "http://127.0.0.1/internal",
                 "publisher": "Bad", "confidence": "low"},
                # must be refused: plausible, resolvable, and NOT in the chunk
                {"text": "Borrowed provenance.", "url": "https://blog.example/post-1",
                 "publisher": "Blog", "confidence": "medium"},
            ],
        }


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def test_passthrough_text():
    r = convert_to_markdown("note.txt", b"hello world")
    assert r.markdown == "hello world" and r.converter == "passthrough"


def test_pdf_without_markitdown_is_honest():
    """The degradation path, which only exists when the extra is absent.

    CI installs '.[dev,ontology]', so this is the configuration it runs in;
    with '.[ingest]' present markitdown handles the file and there is no
    degradation to assert. Skipping says "not applicable here" rather than
    quietly passing on a different code path.
    """
    try:
        import markitdown  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("ingest extra installed — the absence path cannot be exercised")

    r = convert_to_markdown("report.pdf", b"%PDF-1.4 fake")
    assert r.markdown == ""
    assert any("markitdown" in w for w in r.warnings)


def test_pdf_with_markitdown_converts():
    """The presence path — the one an operator who installed the extra uses."""
    pytest.importorskip("markitdown", reason="ingest extra not installed")
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    r = convert_to_markdown("report.pdf", buf.getvalue())
    assert r.converter == "markitdown", r.warnings
    assert not r.warnings


def test_image_without_captioning_is_honest():
    r = convert_to_markdown("shot.png", b"\x89PNG")
    assert r.markdown == "" and r.warnings


def test_oversize_rejected():
    r = convert_to_markdown("big.txt", b"x" * (8 * 1024 * 1024 + 1))
    assert r.markdown == "" and r.warnings
    with pytest.raises(ValueError):
        decode_artifact("big.txt", base64.b64encode(b"x" * 100).decode() + "==")


# ---------------------------------------------------------------------------
# Chunking + literal lane
# ---------------------------------------------------------------------------


def test_chunk_small_doc_unsplit():
    chunks, truncated = chunk_markdown("short")
    assert chunks == ["short"] and not truncated


def test_chunk_large_doc_with_overlap_and_cap():
    text = "\n\n".join(f"## Section {i}\n" + "x" * 2000 for i in range(30))
    chunks, truncated = chunk_markdown(text, max_chars=3000, overlap=200, max_chunks=5)
    assert 1 < len(chunks) <= 5
    assert truncated  # capped, honestly reported


def test_harvest_literal_urls():
    md = "see [advisory](https://cisa.example/adv-1) and https://blog.example/post-2, plus (https://cisa.example/adv-1)"
    urls = harvest_literal_urls(md)
    assert "https://cisa.example/adv-1" in urls
    assert "https://blog.example/post-2" in urls
    assert urls.count("https://cisa.example/adv-1") == 1


# ---------------------------------------------------------------------------
# Merge lanes
# ---------------------------------------------------------------------------


def test_merge_splits_paired_unattributed_and_orphans():
    """The three lanes are disjoint, and only the paired one can fill the form."""
    parts = [
        {"records": [
            {"text": "event A", "url": "https://cisa.example/adv-1",
             "publisher": "CISA", "confidence": "high"},
            {"text": "event B", "url": None, "publisher": "", "confidence": "low"},
        ]},
        {"records": [
            # the chunk overlap re-reports the seam item: must not double-count
            {"text": "Event A", "url": "https://cisa.example/adv-1"},
            {"text": "event C", "url": "https://blog.example/post-2", "confidence": "medium"},
        ]},
    ]
    merged = merge_extractions(
        parts, ["https://cisa.example/adv-1", "https://orphan.example/x"]
    )
    assert [r["text"] for r in merged["records"]] == ["event A", "event C"]
    assert [r["text"] for r in merged["unattributed"]] == ["event B"]
    # a link nothing claimed is surfaced, never silently paired to a record
    assert merged["orphan_urls"] == ["https://orphan.example/x"]


def test_blocks_and_urls_come_out_one_to_one():
    """The invariant runner._texts/_urls enforce, asserted at the source.

    Phase 1 rejects a run where the counts disagree. They used to be produced
    from different shapes — text joined on single newlines (one block, always)
    against every URL found (capped at 50) — so a multi-source document could
    not be ingested at all.
    """
    md = "A talk about cloud risk, citing https://cisa.example/adv-1 and the CISA advisory."
    out = extract_evidence(md, ExtractMockClient())
    blocks = [b for b in out["source_text"].split("\n\n") if b.strip()]
    assert len(blocks) == len(out["source_urls"]) == 1
    assert out["source_urls"] == ["https://cisa.example/adv-1"]
    legacy_payload_key = ("ne" + "ws") + "_body"
    assert legacy_payload_key not in out


def test_prefill_satisfies_the_real_phase1_validator():
    """End to end against the code that actually rejected the run.

    Asserting my own split would only prove my split agrees with itself. This
    drives runner._texts and runner._urls — the two functions whose disagreeing
    counts produced "Phase 1 needs one source URL per evidence-text block".
    """
    from contingency_atlas.web.runner import _texts, _urls

    # MULTI-record, and deliberately awkward: a paragraph break inside a
    # record, two records citing the SAME source, and one item with no link.
    # A single-record case would only assert the 1 == 1 accident the old,
    # broken code already satisfied.
    md = (
        "First https://a.example/1 then https://a.example/2 and again "
        "https://a.example/1 plus an unlinked advisory."
    )

    class MultiRecord:
        def chat_json(self, messages, **kwargs):
            if "completeness judge" in messages[0]["content"]:
                return {"missed_sources": []}
            return {"records": [
                {"text": "Headline one.\n\nImpact line.", "url": "https://a.example/1"},
                {"text": "Second event.", "url": "https://a.example/2"},
                {"text": "Third event, same article.", "url": "https://a.example/1"},
                {"text": "Advisory with no link.", "url": None},
            ]}

    out = extract_evidence(md, MultiRecord())

    # exactly what convertArtifact() puts into the two form fields
    params = {
        "evidence_texts": out["source_text"],
        "source_urls": "\n".join(out["source_urls"]),
    }
    texts, urls = _texts(params), _urls(params, "source_urls")
    assert texts, "no evidence blocks — Phase 1 would refuse the run outright"
    assert len(texts) == len(urls) == 3, (
        f"{len(texts)} blocks vs {len(urls)} URLs — this is the exact "
        "inequality runner.py:189 raises on"
    )
    # the record sharing a URL is kept, not silently dropped
    assert urls == ["https://a.example/1", "https://a.example/2", "https://a.example/1"]
    # the unlinked item is held back for review rather than padded in
    assert [r["text"] for r in out["unattributed"]] == ["Advisory with no link."]


def test_a_prefix_of_a_real_url_is_not_accepted_as_provenance():
    """The dangerous direction: a fabricated link certified by a real one.

    A raw `url in chunk` test is unanchored, so a model returning the bare
    domain passes whenever the chunk contains any deeper URL on it — and the
    result resolves, so a reviewer trusts it.
    """
    from contingency_atlas.ingest_artifacts import _verify_pairing

    chunk = "see https://x.example/status/12345 for details"
    out = _verify_pairing(
        [{"text": "claim", "url": "https://x.example"}], chunk
    )
    assert out[0]["url"] is None, "a prefix of a real URL was accepted as its source"
    assert "not present" in out[0]["note"]


def test_a_private_address_is_refused_even_when_present_in_the_text():
    """Covers the is_public_http_url branch specifically.

    Previously the only test touching this was satisfied by the earlier
    'not in the chunk' branch, so the private-address guard was never reached.
    """
    from contingency_atlas.ingest_artifacts import _verify_pairing

    chunk = "internal runbook at http://127.0.0.1/internal explains it"
    out = _verify_pairing(
        [{"text": "claim", "url": "http://127.0.0.1/internal"}], chunk
    )
    assert out[0]["url"] is None
    assert "invalid URL" in out[0]["note"]


def test_a_non_string_url_does_not_discard_the_whole_chunk():
    """A malformed url field must cost one record, not every record.

    _verify_pairing runs inside _extract_chunk's try, so an AttributeError on
    .strip() would be charged to the entire chunk and burn a repair attempt.
    """
    from contingency_atlas.ingest_artifacts import _verify_pairing

    chunk = "text with https://a.example/1 in it"
    out = _verify_pairing([
        {"text": "bad url type", "url": {"href": "https://a.example/1"}},
        {"text": "good one", "url": "https://a.example/1"},
    ], chunk)
    assert len(out) == 2
    assert out[0]["url"] is None
    assert out[1]["url"] == "https://a.example/1"


def test_a_blank_line_inside_a_record_cannot_split_it():
    """A record carrying a paragraph break would become two blocks, one URL.

    The 1:1 invariant is enforced by _texts splitting on "\\n\\n", so a record
    whose own text contains a blank line re-breaks it from the inside — one
    URL, two blocks. Models emit paragraph breaks routinely.
    """
    from contingency_atlas.ingest_artifacts import _verify_pairing
    from contingency_atlas.web.runner import _texts, _urls

    chunk = "context https://a.example/1 more"
    recs = _verify_pairing(
        [{"text": "first paragraph\n\nsecond paragraph", "url": "https://a.example/1"}],
        chunk,
    )
    merged = merge_extractions([{"records": recs}], [])
    params = {
        "evidence_texts": "\n\n".join(r["text"] for r in merged["records"]),
        "source_urls": "\n".join(r["url"] for r in merged["records"]),
    }
    assert len(_texts(params)) == len(_urls(params, "source_urls")) == 1


def test_more_records_than_phase1_accepts_is_reported(monkeypatch):
    """Both caps truncate to the same number, so the run succeeds having
    silently dropped the overflow. A partial ingest must not look complete."""
    from contingency_atlas.web import runner

    class ManyRecords:
        def __init__(self, *a, **k):
            pass

        def chat_json(self, messages, **kwargs):
            if "completeness judge" in messages[0]["content"]:
                return {"missed_sources": []}
            return {"records": [
                {"text": f"event {i}", "url": f"https://a.example/{i}"}
                for i in range(runner.MAX_EVIDENCE_TEXTS + 10)
            ]}

        def close(self):
            pass

    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: True)
    monkeypatch.setattr("contingency_atlas.llm.OMLXClient", ManyRecords)
    text = " ".join(f"https://a.example/{i}" for i in range(runner.MAX_EVIDENCE_TEXTS + 10))

    with _server() as port:
        status, d = _post(port, "/api/ingest/convert", {"text": text})

    assert status == 200
    assert any("only the first" in w for w in d["warnings"]), d["warnings"]


def test_a_url_absent_from_the_chunk_is_never_paired():
    """The anti-fabrication guard, enforced in code rather than by the prompt.

    A borrowed URL is worse than a missing one: it resolves, so a reviewer
    trusts it, while attaching real provenance to a claim it does not support.
    """
    md = "A talk about cloud risk, citing https://cisa.example/adv-1 and the CISA advisory."
    out = extract_evidence(md, ExtractMockClient())
    paired = {r["url"] for r in out["records"]}
    # the mock offered both, and neither appears in the artifact text
    assert "https://blog.example/post-1" not in paired
    assert all("127.0.0.1" not in (u or "") for u in paired)
    # they are not discarded silently — they come back as review items
    unpaired = [r["text"] for r in out["unattributed"]]
    assert "Borrowed provenance." in unpaired and "Internal note." in unpaired


def test_extract_evidence_end_to_end_small_doc():
    md = "A talk about cloud risk, citing https://cisa.example/adv-1 and the CISA advisory."
    out = extract_evidence(md, ExtractMockClient())
    assert out["chunks_processed"] == 1
    assert out["source_text"]
    origins = {s["origin"] for s in out["sources"]}
    assert "llm" in origins          # the paired record
    assert "unresolved" in origins   # the advisory named without a link
    # the bad private-IP URL must not survive anywhere
    assert all("127.0.0.1" not in (s["url"] or "") for s in out["sources"])


def test_extract_mode_intelligence_uses_risk_prompt():
    class PromptSpy:
        def __init__(self):
            self.system_prompts = []

        def chat_json(self, messages, **kwargs):
            self.system_prompts.append(messages[0]["content"])
            return {"records": []}

    spy = PromptSpy()
    extract_evidence("some text", spy, mode="intelligence")
    assert "EMERGING-RISK" in spy.system_prompts[0]
    spy2 = PromptSpy()
    extract_evidence("some text", spy2, mode="evidence")
    assert "loss-event" in spy2.system_prompts[0]
    with pytest.raises(ValueError):
        extract_evidence("some text", spy2, mode="nonsense")


# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------


def test_rubric_check_pass_and_url_gate():
    md = "text citing https://cisa.example/adv-1"
    extraction = extract_evidence(md, ExtractMockClient())
    r = rubric_check(md, extraction, ExtractMockClient())
    assert r["verdict"] == "pass"
    assert r["url_validity"]["checked"] >= 1
    assert r["url_validity"]["invalid"] == []
    assert "missed_sources" in r


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(port, path, payload, headers=None):
    body = json.dumps(payload).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json", **(headers or {})})
    resp = conn.getresponse()
    text = resp.read().decode("utf-8")
    conn.close()
    return resp.status, json.loads(text) if text else {}


def test_convert_endpoint_pasted_text(monkeypatch):
    from contingency_atlas.web import runner

    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: False)
    with _server() as port:
        status, d = _post(port, "/api/ingest/convert",
                          {"text": "talk mentioning https://cisa.example/adv-9"})
    assert status == 200
    assert d["converter"] == "passthrough"
    assert any(s["url"] == "https://cisa.example/adv-9" for s in d["sources"])
    assert d["rubric"] is None  # OMLX offline -> semantic pass skipped, warned
    assert any("OMLX offline" in w for w in d["warnings"])


def test_offline_convert_still_populates_the_url_lane(monkeypatch):
    """The UI reads d.source_urls; the offline branch must set it.

    When extraction is skipped nothing can attribute a link to a claim, so the
    field is deliberately EMPTY rather than filled with every link found — the
    body is one block, so N URLs would guarantee the 1:1 rejection. The links
    are still returned, as orphans to pair by hand.
    """
    from contingency_atlas.web import runner

    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: False)
    with _server() as port:
        status, d = _post(port, "/api/ingest/convert",
                          {"text": "talk citing https://cisa.example/adv-9 today"})
    assert status == 200
    assert d["source_urls"] == []
    assert d["orphan_urls"] == ["https://cisa.example/adv-9"]
    assert any("unpaired" in w for w in d["warnings"]), d["warnings"]


def test_convert_endpoint_file_upload(monkeypatch):
    from contingency_atlas.web import runner

    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: False)
    content = base64.b64encode(b"transcript text with https://blog.example/talk").decode()
    with _server() as port:
        status, d = _post(port, "/api/ingest/convert",
                          {"filename": "transcript.txt", "content_b64": content})
    assert status == 200
    assert d["filename"] == "transcript.txt"
    assert any(s["url"] == "https://blog.example/talk" for s in d["sources"])


def test_convert_endpoint_bad_payload(monkeypatch):
    from contingency_atlas.web import runner

    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: False)
    with _server() as port:
        status, d = _post(port, "/api/ingest/convert", {"filename": "x.bin"})
        assert status == 400


def test_convert_endpoint_rejects_bad_mode(monkeypatch):
    from contingency_atlas.web import runner

    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: True)
    with _server() as port:
        status, d = _post(port, "/api/ingest/convert",
                          {"text": "hello", "mode": "nonsense"})
        assert status == 400
        assert "mode" in d["error"]


def test_failed_chunks_reach_the_operator(monkeypatch):
    """merge_extractions counted failures; the endpoint dropped the count.

    chunks_processed counts sections ATTEMPTED, so a run where every section
    failed rendered as "1 chunks" with an empty evidence box — identical to a
    document that genuinely contained nothing.
    """
    from contingency_atlas.web import runner

    class AlwaysWrongShape:
        """Extraction succeeds as JSON but never has the required keys."""

        def __init__(self, *a, **k):
            pass

        def chat_json(self, messages, **kwargs):
            return ["a note", "another note"]  # a list, not {body_notes, sources}

        def close(self):
            pass

    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: True)
    monkeypatch.setattr("contingency_atlas.llm.OMLXClient", AlwaysWrongShape)

    with _server() as port:
        status, d = _post(port, "/api/ingest/convert", {"text": "some artifact text"})

    assert status == 200
    assert d["chunks_failed"] == 1
    assert d["chunks_processed"] == 1
    assert any("failed to extract" in w for w in d["warnings"]), d["warnings"]


def test_a_failed_rubric_still_returns_the_checks_that_need_no_model():
    """A judge that dies must not take the deterministic checks with it.

    url_validity is pure Python over the sources list. It used to be computed
    AFTER the LLM call, so a truncated judge threw away a check that could not
    itself fail — and the dashboard rendered nothing at all, making a
    four-minute failure look like a clean panel.
    """
    from contingency_atlas.ingest_artifacts import rubric_check
    from contingency_atlas.llm import LLMTruncatedError

    class Dies:
        def chat_json(self, messages, **kwargs):
            raise LLMTruncatedError("hit the 8192-token ceiling")

    extraction = {
        "source_text": "a claim",
        "sources": [{"url": "http://127.0.0.1/internal"}, {"url": "https://ok.example/1"}],
    }
    r = rubric_check("some artifact text", extraction, Dies())
    assert "error" in r and "ceiling" in r["error"]
    # the deterministic lane survives the model's failure
    assert r["url_validity"]["invalid"] == ["http://127.0.0.1/internal"]
    assert r["url_validity"]["verdict"] == "fail"
    assert r["sources_total"] == 2


def test_the_judges_source_window_is_capped_and_reported():
    """The uncapped half of the payload. artifact_text was always capped and
    the omission reported; the sources list beside it was not, and it is the
    half that grows without bound."""
    from contingency_atlas.ingest_artifacts import RUBRIC_MAX_SOURCES, rubric_check

    seen = {}

    class Spy:
        def chat_json(self, messages, **kwargs):
            seen["payload"] = messages[-1]["content"]
            return {"missed_sources": [], "load_bearing_facts": {"scale": "missing"}}

    n = RUBRIC_MAX_SOURCES + 25
    extraction = {"source_text": "b", "sources": [{"url": f"https://a.example/{i}"} for i in range(n)]}
    r = rubric_check("text", extraction, Spy())

    assert r["sources_window_truncated"] is True
    assert r["sources_judged"] == RUBRIC_MAX_SOURCES and r["sources_total"] == n
    assert "a.example/%d" % (n - 1) not in seen["payload"], "uncapped source reached the judge"
    # url_validity still counts every source, not just the judged window
    assert r["url_validity"]["checked"] == n


def test_load_bearing_facts_is_reported_but_does_not_gate():
    """The dropped criterion, restored as advisory rather than as a gate.

    The schema offers only present|missing with no way to say "the source
    never contained this", so a missing fact cannot be attributed to the
    extraction — and a verdict that fires on evidence it cannot attribute
    would fail every qualitative source until reviewers stopped reading it.
    """
    from contingency_atlas.ingest_artifacts import rubric_check

    class Judge:
        def chat_json(self, messages, **kwargs):
            return {
                "missed_sources": [],
                "attribution_fidelity": {"verdict": "pass"},
                "body_fidelity": {"verdict": "pass"},
                "load_bearing_facts": {"who": "present", "what": "present",
                                       "when": "missing", "scale": "missing",
                                       "impact": "present"},
            }

    r = rubric_check("text", {"source_text": "b", "sources": []}, Judge())
    assert r["facts_missing"] == ["scale", "when"]   # surfaced, sorted
    assert r["verdict"] == "pass"                    # and NOT gated on


def test_modal_has_artifact_wiring_for_phases_1_and_2():
    html = open("contingency_atlas/web/static/index.html").read()
    assert "ARTIFACT_TARGETS" in html
    assert 'phase1: { text: "rf-evidence_texts", urls: "rf-source_urls", mode: "evidence" }' in html
    assert 'phase2: { text: "rf-source_text", urls: "rf-risk_source_urls", mode: "intelligence" }' in html


def test_ingest_extra_pins_every_format_the_converter_offers():
    """markitdown gates each format behind its own extra, so a bare
    "markitdown" requirement installs a converter that refuses every type the
    dashboard advertises — and its failure message points back at this extra,
    which is a loop the operator cannot exit. The pin must therefore cover
    MARKITDOWN_EXTENSIONS, not merely name the package.
    """
    import re
    import tomllib
    from pathlib import Path

    from contingency_atlas.ingest_artifacts import MARKITDOWN_EXTENSIONS

    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    ingest = pyproject["project"]["optional-dependencies"]["ingest"]
    spec = next((s for s in ingest if s.startswith("markitdown")), "")
    assert spec, "the ingest extra no longer requires markitdown"

    pinned = set(re.findall(r"[\w-]+", spec.partition("[")[2].partition("]")[0]))
    # html/htm need no sub-extra; everything else markitdown handles must be named.
    needed = {e.lstrip(".") for e in MARKITDOWN_EXTENSIONS} - {"html", "htm"}
    assert "all" in pinned or needed <= pinned, (
        f"ingest extra {spec!r} misses {sorted(needed - pinned)} — the converter "
        "offers those types but the install would refuse them"
    )
