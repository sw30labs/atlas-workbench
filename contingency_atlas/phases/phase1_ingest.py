"""Phase 1: public-source evidence ingestion and normalization.

Ingests operational-risk reporting from public regulatory releases, incident
disclosures, security advisories, and financial media, then normalizes it into
the project-native evidence schema.

The LLM extracts structured fields from unstructured text:
  - Organization, country, region
  - Loss amount (USD)
  - Source publication date
  - Risk category, event-driver class, primary-cause class
  - Record-level provenance

Input:  raw text (article, enforcement action, alert)
Output: normalized row in the evidence table
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from ..llm import OMLXClient, LLMError
from ..db import add_evidence_event
from ..guard_hook import evidence_event_record, validate
from ..provenance import canonical_public_url, is_public_http_url
from ..schema import (
    EVIDENCE_EVENT_COLUMNS as EVIDENCE_EVENT_SCHEMA_COLUMNS,
    normalize_instrument_status,
)
from ..categories import (
    EVENT_DRIVER_CLASSES,
    PRIMARY_CAUSE_CLASSES,
    REGIONS,
    RISK_CATEGORIES,
    TAXONOMY_VERSION,
    derive_category,
    normalize_region,
)

logger = logging.getLogger(__name__)
PROMPT_VERSION = "phase1-ingestion-v2"


_EXTRACT_PROMPT_TEMPLATE = """You are extracting operational-risk events from public source text for an independent research database.

Given the text below, extract these fields as JSON:
{
  "headline": "short normalized event title",
  "organization": "organization named in the source",
  "country": "country where the event occurred",
  "region": "one of: __REGIONS__",
  "loss_amount_usd": number or null,
  "source_date": "required source publication date in YYYY-MM-DD format",
  "risk_category": "one of the Atlas 1.0 categories: __RISK_CATEGORIES__",
  "event_driver_class": "one of: __EVENT_DRIVER_CLASSES__",
  "primary_cause_class": "one of: __PRIMARY_CAUSE_CLASSES__",
  "source_url": "the supplied canonical public source URL",
  "source_publisher": "publisher or issuing authority",
  "source_title": "title used by the source",
  "source_type": "one of: regulatory_release, company_disclosure, security_advisory, public_reporting, research, other",
  "instrument_status": "for regulatory_release only: one of proposed, enacted, enforcement, vacated; otherwise null",
  "independently_authored_summary": "a factual 1-2 sentence summary in original wording, with no copied passage",
  "provenance_note": "brief note describing what was extracted and any limitations"
}

Do not infer private or confidential information. Only include fields supported
by the supplied text. Use null for unknown optional fields. A publication date
is required for acceptance; return null rather than inventing one when the
source text does not establish it. Use the exact spelling of the listed values
for the closed vocabularies; if none of them fits, return null rather than
inventing a value.
Text: """

# The vocabularies come from categories.py rather than being spelled out here,
# so this lane and the deep-research lane cannot drift apart.
EXTRACT_PROMPT = (
    _EXTRACT_PROMPT_TEMPLATE
    .replace("__REGIONS__", ", ".join(REGIONS))
    .replace("__RISK_CATEGORIES__", ", ".join(RISK_CATEGORIES))
    .replace("__EVENT_DRIVER_CLASSES__", ", ".join(EVENT_DRIVER_CLASSES))
    .replace("__PRIMARY_CAUSE_CLASSES__", ", ".join(PRIMARY_CAUSE_CLASSES))
)

# Exact columns of the evidence table. LLM output is filtered to this set before
# it reaches SQL — model-invented keys must never become SQL identifiers.
EVIDENCE_EVENT_COLUMNS = frozenset(name for name, _ in EVIDENCE_EVENT_SCHEMA_COLUMNS)
MODEL_EXTRACTABLE_COLUMNS = EVIDENCE_EVENT_COLUMNS - {
    "event_id",
    "record_type",
    "provenance_class",
    "source_locator",
    "retrieved_at",
    "research_inference",
    "rights_review",
    "content_fingerprint",
    "review_status",
    "reviewed_at",
    "taxonomy_version",
}


@dataclass
class IngestionResult:
    """Result of a single ingestion call."""

    events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: int = 0


def extract_event_from_text(
    text: str,
    client: OMLXClient,
    *,
    source_url: str | None = None,
) -> dict[str, Any] | None:
    """Extract a structured event from unstructured text using the LLM.

    Returns a dict matching the evidence schema, or None on failure.
    """
    if not is_public_http_url(source_url):
        logger.warning("Public evidence ingestion requires an http(s) source URL")
        return None
    # Canonicalize before the URL reaches the record: the content fingerprint
    # below is computed over source_url, so encoding afterwards would leave a
    # fingerprint that no longer reproduces from the stored value.
    source_url = canonical_public_url(source_url)

    prompt = EXTRACT_PROMPT + text[:8000]  # Cap input length
    prompt += f"\n\nSource URL: {source_url}"

    try:
        data = client.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=2048,
        )
    except (LLMError, httpx.HTTPError) as e:
        logger.warning(f"Extraction LLM call failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Extraction failed unexpectedly: {e}", exc_info=True)
        return None

    if not isinstance(data, dict):
        logger.error(f"Extraction returned non-object JSON: {type(data).__name__}")
        return None

    # Keep only known evidence columns; the model may add fields of its own.
    dropped = set(data) - MODEL_EXTRACTABLE_COLUMNS
    if dropped:
        logger.debug(f"Dropping unknown extraction fields: {sorted(dropped)}")
    data = {k: v for k, v in data.items() if k in MODEL_EXTRACTABLE_COLUMNS}

    # Ensure required fields exist
    if not all(
        data.get(field)
        for field in (
            "headline",
            "source_publisher",
            "source_title",
            "source_date",
            "independently_authored_summary",
        )
    ):
        return None

    # A public record without a verifiable publication date is incomplete
    # provenance. Reject it instead of silently storing an undated event.
    source_date = str(data["source_date"])
    try:
        parsed_date = datetime.strptime(source_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    if parsed_date.strftime("%Y-%m-%d") != source_date:
        return None

    classification_terms = [
        str(data.get("headline") or ""),
        str(data.get("independently_authored_summary") or ""),
        str(data.get("risk_category") or ""),
        str(data.get("event_driver_class") or ""),
        str(data.get("primary_cause_class") or ""),
    ]
    if data.get("risk_category") not in RISK_CATEGORIES:
        data["risk_category"] = derive_category(
            {
                "title": data.get("headline"),
                "description": data.get("independently_authored_summary"),
                "core_risks": classification_terms,
            }
        )
    # Classification fields are NOT derived when the model declines or
    # answers outside the vocabulary — a NULL is honestly "unclassified", where
    # a keyword guess is a false claim inside an evidence record. Measured on
    # this register's own phase-1 rows, the deriver reproduces the model's label
    # for 3 of 25 (an upper bound of 10 of 25 if it is also fed the model's
    # rejected free text), and it assigns "Model failure" or "Data integrity" to
    # 11 of 25 where the model assigned neither even once: "Fed Vice Chair
    # Bowman calls for tailored supervision" becomes Model failure on the word
    # "model". Nothing reads these two columns, so a blank costs nothing; a
    # wrong label would have to be caught by a human later.
    #
    # risk_category keeps derive_category deliberately: it IS read (phase-3/5
    # context, the dashboard, the C-04 shape), a blank would break those
    # consumers, and its rules have not been shown to misfire this way.
    if data.get("event_driver_class") not in EVENT_DRIVER_CLASSES:
        data["event_driver_class"] = None
    if data.get("primary_cause_class") not in PRIMARY_CAUSE_CLASSES:
        data["primary_cause_class"] = None
    data["region"] = normalize_region(data.get("region"))

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d%H%M%S")
    data["event_id"] = f"EVT-{timestamp}-{uuid.uuid4().hex[:8]}"
    data["record_type"] = "event"
    data["retrieved_at"] = now.isoformat()
    data["taxonomy_version"] = TAXONOMY_VERSION
    data["source_url"] = source_url
    data.setdefault("source_title", data.get("headline"))
    data.setdefault(
        "source_type",
        "public_reporting",
    )
    data["instrument_status"] = normalize_instrument_status(
        data.get("instrument_status")
    )
    primary_source_types = {
        "regulatory_release",
        "company_disclosure",
        "security_advisory",
    }
    data["provenance_class"] = (
        "public_primary"
        if data["source_type"] in primary_source_types
        else "public_secondary"
    )
    data["source_locator"] = None
    data["research_inference"] = None
    data["rights_review"] = "pending"
    fingerprint_input = "\n".join(
        str(data.get(field) or "")
        for field in (
            "source_url",
            "source_publisher",
            "source_title",
            "source_date",
            "independently_authored_summary",
        )
    )
    data["content_fingerprint"] = hashlib.sha256(
        fingerprint_input.encode("utf-8")
    ).hexdigest()
    data["review_status"] = "pending"
    data["reviewed_at"] = None
    data.setdefault(
        "provenance_note",
        (
            "Extracted from the cited public source; summary independently "
            "authored during ingestion."
        ),
    )

    # Validate loss amount
    loss = data.get("loss_amount_usd")
    if loss is not None:
        try:
            data["loss_amount_usd"] = float(loss)
        except (ValueError, TypeError):
            data["loss_amount_usd"] = None

    return data


def ingest_text(
    text: str,
    client: OMLXClient,
    db_path: str,
    *,
    source_url: str | None = None,
) -> IngestionResult:
    """Extract an event from text and add it to the evidence database.

    Returns an IngestionResult with the extracted event.
    """
    result = IngestionResult()

    event = extract_event_from_text(text, client, source_url=source_url)
    if event is None:
        result.errors.append("Failed to extract event from text")
        return result

    # Ontology gate (C-04/C-05/C-10 door half): a rejected event is not
    # persisted; the extraction error goes to the result, not the store.
    gate = validate([evidence_event_record(event)], seam="phase1_ingest")
    if gate.rejected_ids:
        msg = f"guard rejected event {event.get('event_id', 'unknown')!r}"
        logger.warning("%s: %s", msg, event.get("headline", "unknown"))
        result.errors.append(msg)
        return result

    try:
        added = add_evidence_event(db_path, event)
    except sqlite3.Error as e:
        logger.error(f"Failed to store event {event.get('headline', 'unknown')!r}: {e}")
        result.errors.append(f"DB insert failed: {e}")
        return result
    if added:
        result.events.append(event)
        logger.info(f"Ingested: {event.get('headline', 'unknown')}")
    else:
        result.skipped = 1
        logger.info("Skipped duplicate event")

    return result


def ingest_batch(
    texts: list[str],
    client: OMLXClient,
    db_path: str,
    *,
    source_urls: list[str] | None = None,
) -> IngestionResult:
    """Ingest multiple texts. Returns aggregated result."""
    result = IngestionResult()
    urls = source_urls or []
    if len(urls) != len(texts) or any(not url for url in urls):
        result.errors.append(
            "Each evidence text requires one aligned public source URL"
        )
        result.skipped = len(texts)
        return result

    for i, text in enumerate(texts):
        sub = ingest_text(text, client, db_path, source_url=urls[i])
        result.events.extend(sub.events)
        result.errors.extend(sub.errors)
        result.skipped += sub.skipped

    logger.info(
        f"Batch ingestion: {len(result.events)} added, "
        f"{result.skipped} skipped, {len(result.errors)} errors"
    )
    return result


# ── RSS/Feed parsing helpers ──

def parse_rss_item(item: dict[str, Any]) -> str:
    """Convert an RSS feed item dict to text for LLM extraction."""
    parts = []
    if "title" in item:
        parts.append(item["title"])
    if "summary" in item:
        parts.append(item["summary"])
    if "description" in item:
        parts.append(item["description"])
    return " ".join(parts)


def parse_enforcement_action(
    agency: str,
    action_text: str,
    *,
    url: str | None = None,
) -> str:
    """Format a regulatory enforcement action for extraction.

    Args:
        agency: e.g. "FINRA", "FCA", "MAS", "SEC"
        action_text: The enforcement action text
        url: Source URL
    """
    return f"Agency: {agency}\n{action_text}"
