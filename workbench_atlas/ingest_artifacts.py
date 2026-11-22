"""Artifact ingestion — files/screenshots/transcripts to reviewed evidence.

Pipeline (designed with the user, 2026-08-01):

1. **Convert** artifacts to markdown. Microsoft MarkItDown when installed
   (optional extra ``[ingest]``); plain text/markdown passthrough always;
   everything else gets an honest "unsupported" warning — never a silent
   guess.
2. **Extract** source text + source candidates with a *bounded* chunk map:
   split on headings/size with overlap, per-chunk strict-JSON extraction
   (tenacity-bounded repair, one attempt), deterministic merge. Literal
   URLs are harvested from markdown structure first; LLM-resolved URLs
   are a separate, lower-confidence lane; references without URLs stay
   explicitly unresolved. No lane may fabricate its way to green.
3. **Rubric check** (not a confidence number): missed-sources
   enumeration, attribution fidelity with quoted spans, body fidelity,
   load-bearing facts checklist, and deterministic URL validity via
   ``provenance.is_public_http_url``.

Output is a DRAFT for the review screen: nothing here writes to any
store. The human confirms or edits, then the existing Phase 1 flow runs.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMTruncatedError
from .provenance import is_public_http_url

logger = logging.getLogger(__name__)

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
CHUNK_CHARS = 6000
CHUNK_OVERLAP = 400
MAX_CHUNKS = 20
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".rst", ".log", ".vtt", ".srt", ".csv"}
MARKITDOWN_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm"}

SOURCE_ORIGINS = ("literal", "llm", "unresolved")


# ---------------------------------------------------------------------------
# 1. Conversion
# ---------------------------------------------------------------------------


@dataclass
class ConvertResult:
    markdown: str
    warnings: list[str] = field(default_factory=list)
    converter: str = "passthrough"


def _markitdown_available() -> bool:
    try:
        import markitdown  # noqa: F401

        return True
    except ImportError:
        return False


def convert_to_markdown(filename: str, content: bytes) -> ConvertResult:
    """Convert an artifact to markdown, honestly degrading per type."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if len(content) > MAX_ARTIFACT_BYTES:
        return ConvertResult("", [f"artifact exceeds {MAX_ARTIFACT_BYTES // (1024 * 1024)}MB limit"])
    if ext in TEXT_EXTENSIONS or not ext:
        try:
            return ConvertResult(content.decode("utf-8", errors="replace"), converter="passthrough")
        except Exception as exc:  # pragma: no cover - decode with replace rarely raises
            return ConvertResult("", [f"could not decode text: {exc}"])
    if ext in MARKITDOWN_EXTENSIONS:
        if not _markitdown_available():
            return ConvertResult(
                "",
                [
                    f"'{ext}' needs the optional markitdown dependency "
                    "(pip install 'workbench-atlas[ingest]') — or paste the text directly"
                ],
            )
        import io

        from markitdown import MarkItDown

        try:
            result = MarkItDown().convert_stream(io.BytesIO(content), file_extension=ext)
            return ConvertResult(result.text_content or "", converter="markitdown")
        except Exception as exc:
            return ConvertResult("", [f"markitdown failed on {filename}: {exc}"])
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return ConvertResult(
            "",
            [
                "image conversion needs a multimodal model caption pass "
                "(not wired in this environment) — describe the screenshot "
                "in text for now"
            ],
        )
    return ConvertResult("", [f"unsupported file type '{ext or filename}'"])


# ---------------------------------------------------------------------------
# 2. Chunking + extraction
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,3}\s", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\w(/@])(https?://[^\s<>)\]]+)")


def chunk_markdown(
    text: str, *, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP, max_chunks: int = MAX_CHUNKS
) -> tuple[list[str], bool]:
    """Split on headings when possible, else on size with overlap.
    Returns (chunks, truncated)."""
    if len(text) <= max_chars:
        return [text], False
    boundaries = [m.start() for m in _HEADING_RE.finditer(text)]
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = min(start + max_chars, len(text))
        if end < len(text):
            # prefer to break at a heading boundary before end
            candidates = [b for b in boundaries if start + overlap < b <= end]
            if candidates:
                end = candidates[-1]
        chunks.append(text[start:end])
        start = end - overlap if end < len(text) else end
    truncated = start < len(text)
    return chunks, truncated


def harvest_literal_urls(markdown: str) -> list[str]:
    """Deterministic lane: URLs from markdown structure (links + bare)."""
    urls = list(dict.fromkeys(_MD_LINK_RE.findall(markdown)))
    urls += [u for u in _BARE_URL_RE.findall(markdown) if u not in urls]
    return [u.rstrip(".,;:'\"") for u in urls]


PROMPT_VERSION = "artifact-ingestion-v1"

_RECORD_RULES = """Rules:
- One record per distinct item. `text` must stand on its own without the
  surrounding document — a reviewer reads it alone.
- `url` must be copied CHARACTER FOR CHARACTER from this chunk and must be the
  source OF THAT ITEM. If the item has no URL in this chunk, url MUST be null.
  Never reuse a nearby URL to fill the field, and never invent one: a wrong
  pairing is worse than a null, because it attaches real provenance to the
  wrong claim.
- No invented facts or publishers. If the chunk contains nothing extractable,
  records is [].
JSON only, no markdown fences."""

_EXTRACT_SYSTEMS = {
    "evidence": """You extract loss-event evidence from ONE chunk of a larger artifact.
Each record is ONE self-contained evidence statement together with the source
it came from. Return JSON only:
{
  "records": [
    {"text": "one faithful, self-contained statement of what happened",
     "url": "the URL in THIS chunk that this statement came from, else null",
     "publisher": "who published it, else \\"\\"",
     "confidence": "high|medium|low"}
  ]
}
"""
    + _RECORD_RULES,
    "intelligence": """You extract EMERGING-RISK intelligence from ONE chunk of a larger artifact.
Focus on what is NEW or CHANGING: novel dependencies, concentration, control
weaknesses, technology/regulatory shifts, joint-failure potential. Each record
is ONE self-contained signal together with the source it came from.
Return JSON only:
{
  "records": [
    {"text": "one faithful, self-contained statement of what is new or changing and why it matters",
     "url": "the URL in THIS chunk that this statement came from, else null",
     "publisher": "who published it, else \\"\\"",
     "confidence": "high|medium|low"}
  ]
}
"""
    + _RECORD_RULES,
}

EXTRACT_MODES = tuple(_EXTRACT_SYSTEMS)

#: Token ceiling for ONE chunk extraction.
#:
#: A 6000-char chunk was measured needing ~2540 tokens to close the full
#: records object; the previous 2048 cut it off mid-structure on real
#: documents, which is the bug this constant exists to fix. 4096 keeps ~1.6x
#: headroom over that measurement, and the extractor's output is bounded by
#: its input — one chunk in, one records list out — so the ceiling does not
#: grow with document size.
#:
#: An earlier revision justified the upper bound as "a budget the model cannot
#: reach inside the request timeout becomes a retried read timeout". That is
#: FALSE on this stack and the claim is retracted: httpx applies its timeout
#: per socket read, not to total generation, and the OMLX backend keeps the
#: connection fed during generation — so two calls were observed completing at
#: 233s and 246s against a 180s setting without raising. There is no
#: wall-clock ceiling on a generation anywhere in the client; if one is ever
#: wanted it has to be imposed explicitly, not inferred from `timeout=`.
EXTRACT_MAX_TOKENS = 4096

#: Token ceiling for the rubric judge — deliberately NOT EXTRACT_MAX_TOKENS.
#:
#: The two share nothing but a shape. The extractor sees one 6000-char chunk
#: (~1.8k tokens observed) and answers about that chunk. The judge sees the
#: artifact window PLUS the whole extraction (~20k-27k tokens observed) and
#: must enumerate every source the extraction missed — an output that grows
#: with the source list, not with a chunk. Sharing the extractor's 4096 made
#: this the only call site in the codebase with negative headroom: it
#: truncated in both real sessions on record, at exactly 4096 tokens.
RUBRIC_MAX_TOKENS = 8192

#: How many sources the judge is shown. rubric_check capped `artifact_text`
#: from the start and reported the omission via judged_window_truncated, but
#: passed the sources list uncapped beside it — and that list is the half that
#: grows without bound, since orphan URLs are harvested from the whole
#: document while extraction stops at MAX_CHUNKS. A 10k-link artifact built a
#: ~968k-token prompt no backend can serve. Capped and reported the same way
#: the text window already was.
RUBRIC_MAX_SOURCES = 60


def _verify_pairing(records: list[Any], chunk: str) -> list[dict[str, Any]]:
    """Keep a record's URL only if it is literally in the chunk it came from.

    The prompt asks the model not to invent or borrow a URL, but provenance is
    not something to leave to a model's compliance: a plausible-looking
    pairing attaches a real, checkable source to a claim it does not support,
    which is worse than no URL at all — a reviewer trusts it precisely because
    the link resolves. So the pairing is re-derived here from the chunk text.
    Anything unconfirmed drops to url=None and becomes a human-review item
    rather than a silently mis-attributed record.
    """
    # Match against the URLs actually harvested from the chunk, not a raw
    # substring test. `url in chunk` is unanchored, so "https://x.com" passes
    # whenever the chunk contains "https://x.com/status/123" — a fabricated
    # link certified by a real one, which is the exact direction this guard
    # exists to stop. Using the harvester also settles trailing punctuation
    # the same way the orphan lane does, so a URL ending a sentence cannot be
    # stored with the period attached AND reported as an unclaimed orphan.
    present = set(harvest_literal_urls(chunk))
    verified: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        text = str(rec.get("text", "")).strip()
        if not text:
            continue
        # A blank line is the block separator runner._texts splits on, so a
        # record containing one would become TWO evidence blocks while still
        # contributing a single URL — silently re-breaking the 1:1 invariant
        # from inside a record. Models emit paragraph breaks routinely, so
        # collapse them here rather than trusting the text not to contain one.
        text = re.sub(r"\n\s*\n+", "\n", text)
        # str(): a non-string url (the model occasionally returns a list or an
        # object) would raise AttributeError on .strip(), and _extract_chunk's
        # broad `except Exception` would charge that to the whole chunk —
        # discarding every record in it and burning a repair generation that
        # could never help.
        url = str(rec.get("url") or "").strip() or None
        dropped = ""
        if url and url not in present:
            dropped, url = f"url not present in the chunk, unpaired: {url}", None
        elif url and not is_public_http_url(url):
            dropped, url = f"invalid URL dropped: {url}", None
        verified.append({
            "text": text[:4000],
            "url": url,
            "publisher": str(rec.get("publisher", ""))[:200],
            "confidence": rec.get("confidence", "low"),
            "note": dropped,
        })
    return verified


def _extract_chunk(chunk: str, client: Any, mode: str = "evidence") -> dict[str, Any]:
    """One strict-JSON extraction call + one bounded repair attempt."""
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEMS[mode]},
        {"role": "user", "content": chunk},
    ]
    for attempt in range(2):
        try:
            data = client.chat_json(
                messages, temperature=0.0, max_tokens=EXTRACT_MAX_TOKENS,
            )
            if isinstance(data, dict) and isinstance(data.get("records"), list):
                return {"records": _verify_pairing(data["records"], chunk)}
            logger.info(
                "chunk extraction attempt %d returned %s without a records list "
                "— treating as a failed extraction",
                attempt + 1, type(data).__name__,
            )
        except LLMTruncatedError as exc:
            # The repair turn asks for "the same answer as valid JSON only",
            # which cannot help: the answer was well-formed and simply did not
            # fit. A second attempt truncates at the identical ceiling, so stop
            # here rather than spend another full generation proving it.
            logger.info("chunk extraction hit the token ceiling: %s", exc)
            break
        except Exception as exc:
            logger.info("chunk extraction attempt %d failed: %s", attempt + 1, exc)
            messages = messages + [
                {"role": "assistant", "content": "(invalid)"},
                {"role": "user", "content": "Return the same answer as valid JSON only."},
            ]
    return {"records": [], "chunk_failed": True}


def merge_extractions(
    parts: list[dict[str, Any]], literal_urls: list[str]
) -> dict[str, Any]:
    """Deterministic merge into three disjoint lanes.

    Phase 1 requires one canonical URL per evidence-text block, so the only
    lane that may pre-fill the form is the paired one — everything else is
    routed to human review rather than padded to make the counts line up.

    * ``records``      — text WITH a verified URL, in order. Two records MAY
                         share a URL: one article routinely reports several
                         distinct events, and phase1_ingest pairs positionally
                         without requiring the URLs to be unique.
    * ``unattributed`` — text the chunk gave no URL for. Real content, but it
                         cannot be ingested until a reviewer supplies a source.
    * ``orphan_urls``  — links present in the artifact that no record claimed.
    """
    records: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_text: set[str] = set()

    for part in parts:
        for rec in part.get("records", []):
            if not isinstance(rec, dict):
                continue
            text = str(rec.get("text", "")).strip()
            if not text:
                continue
            key = re.sub(r"\s+", " ", text.lower())
            if key in seen_text:
                continue  # the chunk overlap re-reports items at the seams
            seen_text.add(key)
            url = rec.get("url") or None
            if url:
                # No dedup by URL. Dropping the second record citing a source
                # discarded its text into no lane at all — not records, not
                # unattributed, not orphan_urls, and with no counter — so a
                # digest where one article yields three events silently lost
                # two of them. Seam duplicates are already handled by the text
                # key above, which is the only duplication that is an artefact
                # rather than real content.
                seen_urls.add(url)
                records.append({**rec, "origin": "paired"})
            else:
                unattributed.append({**rec, "origin": "unattributed"})

    orphans = [u for u in literal_urls if u not in seen_urls]

    # Compat lane: the rubric judge and the review panel still read `sources`.
    sources = [
        {"publisher": r.get("publisher", ""), "reference": r.get("text", "")[:300],
         "url": r.get("url"), "origin": "llm",
         "confidence": r.get("confidence", "low"), "note": r.get("note", "")}
        for r in records
    ] + [
        {"publisher": "", "reference": "", "url": u, "origin": "literal",
         "confidence": "high", "note": "link in artifact, no record claimed it"}
        for u in orphans
    ] + [
        {"publisher": r.get("publisher", ""), "reference": r.get("text", "")[:300],
         "url": None, "origin": "unresolved",
         "confidence": r.get("confidence", "low"), "note": r.get("note", "")}
        for r in unattributed
    ]

    failed = sum(1 for p in parts if p.get("chunk_failed"))
    return {
        "records": records,
        "unattributed": unattributed,
        "orphan_urls": orphans,
        "sources": sources,
        "chunks_failed": failed,
    }


def extract_evidence(markdown: str, client: Any, mode: str = "evidence") -> dict[str, Any]:
    """Bounded chunk map + deterministic merge over one artifact.

    ``mode``: "evidence" (Phase 1 evidence events) or "intelligence" (Phase 2
    emerging-risk signals) — same lanes and merge, different focus.
    """
    if mode not in EXTRACT_MODES:
        raise ValueError(f"unknown extraction mode {mode!r} (use one of {EXTRACT_MODES})")
    chunks, truncated = chunk_markdown(markdown)
    parts = [_extract_chunk(c, client, mode=mode) for c in chunks]
    # Harvest from the chunks actually processed, not the whole document.
    # chunk_markdown stops at MAX_CHUNKS, so harvesting `markdown` reported
    # orphan links out of text the extractor never read — links no record
    # COULD have claimed — and grew the downstream judge prompt without bound
    # on a large artifact. `truncated` already tells the operator the tail was
    # not read; the orphan lane must not contradict it.
    merged = merge_extractions(parts, harvest_literal_urls("\n".join(chunks)))
    # BLANK line between blocks: runner._texts splits on "\n\n", so joining
    # with a single newline collapsed every record into one block and made the
    # count disagree with the URL list no matter how many records there were.
    merged["source_text"] = "\n\n".join(r["text"] for r in merged["records"])
    merged["source_urls"] = [r["url"] for r in merged["records"]]
    merged["chunks_processed"] = len(chunks)
    merged["truncated"] = truncated
    merged["mode"] = mode
    return merged


# ---------------------------------------------------------------------------
# 3. Rubric completeness check
# ---------------------------------------------------------------------------

_RUBRIC_SYSTEM = """You are the completeness judge for an evidence extraction.
Given (a) the artifact text and (b) the extraction, score these rubric
criteria. Each needs EVIDENCE, not confidence.

1. missed_sources: list EVERY named source/report/advisory/document in the
   text that is ABSENT from the extraction (verbatim names; [] if none).
2. attribution_fidelity: for each extracted source, is it genuinely in the
   text? verdict pass/fail + offending items.
3. body_fidelity: does the source text avoid facts not present in the text?
   verdict pass/fail + offending sentences.
4. load_bearing_facts: does the body preserve who/what/when/scale/impact?
   verdict per item (present/missing).

Return JSON only:
{"missed_sources": [...], "attribution_fidelity": {"verdict": "pass|fail", "issues": [...]},
 "body_fidelity": {"verdict": "pass|fail", "issues": [...]},
 "load_bearing_facts": {"who": "present|missing", "what": "present|missing",
   "when": "present|missing", "scale": "present|missing", "impact": "present|missing"}}"""


def rubric_check(markdown: str, extraction: dict[str, Any], client: Any) -> dict[str, Any]:
    """Rubric-based completeness check (evidence-forced, not confidence)."""
    text = markdown[:24_000]  # cap the judged window; coverage note if truncated
    all_sources = extraction.get("sources", [])
    shown = all_sources[:RUBRIC_MAX_SOURCES]
    payload = json.dumps(
        {
            "artifact_text": text,
            "extraction": {
                "source_text": extraction.get("source_text", ""),
                "sources": shown,
            },
        },
        ensure_ascii=False,
    )

    # url_validity is pure Python over the sources and owes the model nothing,
    # so it is computed BEFORE the call. It used to sit after, which meant a
    # failed judge threw away a deterministic check that had already been paid
    # for — the one part of the rubric that cannot fail.
    urls = [s["url"] for s in all_sources if s.get("url")]
    invalid = [u for u in urls if not is_public_http_url(u)]
    deterministic = {
        "url_validity": {
            "checked": len(urls),
            "invalid": invalid,
            "verdict": "pass" if not invalid else "fail",
        },
        "judged_window_truncated": len(markdown) > len(text),
        "sources_window_truncated": len(all_sources) > len(shown),
        "sources_judged": len(shown),
        "sources_total": len(all_sources),
    }

    try:
        data = client.chat_json(
            [
                {"role": "system", "content": _RUBRIC_SYSTEM},
                {"role": "user", "content": payload},
            ],
            temperature=0.0,
            max_tokens=RUBRIC_MAX_TOKENS,
        )
    except Exception as exc:
        return {"error": f"rubric call failed: {exc}", **deterministic}
    if not isinstance(data, dict):
        return {"error": "rubric returned non-object", **deterministic}

    data.update(deterministic)
    # load_bearing_facts is reported, NOT gated on. The schema offers only
    # present|missing with no way to say "the source never contained this", so
    # a missing fact cannot be attributed to the extraction — and a verdict
    # that fires on evidence it cannot attribute would fail every qualitative
    # source until reviewers stopped reading it. The other three criteria are
    # different in kind: each detects an extraction ERROR (a dropped source,
    # invented attribution, invented facts), so those still gate. See
    # docs/decisions/006-review-first-ingestion.md.
    data["facts_missing"] = sorted(
        k for k, v in (data.get("load_bearing_facts") or {}).items()
        if str(v).lower() == "missing"
    )
    data["verdict"] = (
        "pass"
        if not data.get("missed_sources")
        and data.get("attribution_fidelity", {}).get("verdict") == "pass"
        and data.get("body_fidelity", {}).get("verdict") == "pass"
        and not invalid
        else "fail"
    )
    return data


# ---------------------------------------------------------------------------
# Endpoint helper
# ---------------------------------------------------------------------------


def decode_artifact(filename: str, content_b64: str) -> bytes:
    raw = base64.b64decode(content_b64.encode("ascii"), validate=True)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact exceeds {MAX_ARTIFACT_BYTES // (1024 * 1024)}MB limit")
    return raw
