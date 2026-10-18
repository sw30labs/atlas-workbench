"""Optional Singularity Atlas Neo4j pull for draft-first research intake.

The local Singularity Atlas graph is an *adjacent* public-feed index, not a
governed Contingency Atlas store. This module may read it when the operator
opts in. It never writes to Neo4j, never imports the other package, and never
raises into the dashboard path: a missing driver, a down database, a remote
URI, or a bad record becomes an unavailable/skipped result with a reason.

Loop archive editions (``origin=archive``) are excluded. That corpus is
all-rights-reserved. Only public feed stories with an admitted HTTP(S) URL
may be offered, and only when they are relevant to operational-risk foresight.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from .config import project_root
from .independence_lexicon import host_is_denied
from .provenance import canonical_public_url, is_public_http_url, url_dedup_variants

logger = logging.getLogger(__name__)

DEFAULT_BOLT_URI = "bolt://127.0.0.1:7689"
DEFAULT_BOLT_USER = "neo4j"
# Published local docker-compose default in the Singularity Atlas repo — not a
# remote secret. Override with SINGULARITY_ATLAS_NEO4J_PASSWORD.
DEFAULT_BOLT_PASSWORD = "singularity-atlas"

MAX_FETCH = 200
MAX_SELECTED = 8
MAX_SKIPPED_IN_PAYLOAD = 25
CONNECT_TIMEOUT_SECONDS = 2.0
# Cap leftovers and unsubmitted selections must remain retryable.
SEEN_DECISIONS = frozenset(
    {
        "selected",
        "skipped_rights",
        "skipped_provenance",
        "skipped_denied_host",
        "skipped_duplicate",
        "skipped_irrelevant",
    }
)

PREFERRED_VECTORS: dict[str, float] = {
    "security": 1.0,
    "compute": 0.9,
    "agency": 0.8,
    "capability": 0.45,
}
PREFERRED_VECTOR_FLOOR = 0.2
KEYWORD_ONLY_HITS = 2

# Operational-risk overlap with the adjacent graph. Culture/space/capital
# stories are admitted only when these terms also fire.
RELEVANCE_TERMS: tuple[str, ...] = (
    "adversarial",
    "agentic",
    "alignment",
    "capacity rationing",
    "chip shortage",
    "cloud",
    "concentration",
    "cyber",
    "data center",
    "datacenter",
    "disruption",
    "fab",
    "foundry",
    "gpu",
    "hyperscaler",
    "infrastructure",
    "jailbreak",
    "model failure",
    "open weight",
    "operational",
    "outage",
    "payment",
    "prompt injection",
    "ransomware",
    "regulation",
    "safety incident",
    "supply chain",
    "third party",
    "third-party",
    "vendor",
)

SkipDecision = Literal[
    "skipped_unavailable",
    "skipped_rights",
    "skipped_provenance",
    "skipped_denied_host",
    "skipped_seen",
    "skipped_duplicate",
    "skipped_irrelevant",
    "skipped_cap",
]


@dataclass(frozen=True)
class SingularityProbe:
    available: bool
    reason: str
    uri: str
    driver_installed: bool
    loopback: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StorySkip:
    story_id: str
    title: str
    url: str
    decision: SkipDecision
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SelectedStory:
    story_id: str
    title: str
    url: str
    source: str
    summary: str
    origin: str
    published_at: str
    vectors: dict[str, float]
    entities: tuple[str, ...]
    salience: float
    rationale: str
    relevance_score: float

    def as_unit(self) -> str:
        vector_line = ", ".join(
            f"{name}={score:.2f}" for name, score in sorted(self.vectors.items())
        ) or "none"
        entity_line = ", ".join(self.entities) or "none"
        return "\n".join(
            (
                f"Singularity Atlas public-feed story {self.story_id}",
                self.title,
                self.summary or self.title,
                self.url,
                f"Source: {self.source or 'unknown'} · published {self.published_at or 'unknown'}",
                f"Entities: {entity_line}",
                f"Themes: {vector_line}",
                f"Why included: {self.rationale}",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["entities"] = list(self.entities)
        return value


@dataclass
class SourcePull:
    status: Literal["skipped", "unavailable", "empty", "selected"]
    probe: SingularityProbe
    selected: list[SelectedStory] = field(default_factory=list)
    skipped: list[StorySkip] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.skipped:
            counts[item.decision] = counts.get(item.decision, 0) + 1
        return {
            "status": self.status,
            "available": self.probe.available,
            "reason": self.probe.reason,
            "uri": self.probe.uri,
            "note": self.note,
            "selected": [item.to_dict() for item in self.selected],
            "skipped": [
                item.to_dict() for item in self.skipped[:MAX_SKIPPED_IN_PAYLOAD]
            ],
            "summary": {
                "selected": len(self.selected),
                "skipped": len(self.skipped),
                **counts,
            },
        }


def connection_settings() -> dict[str, str]:
    """Read local connection settings. Password is never logged or returned to the UI."""
    uri = (
        os.environ.get("SINGULARITY_ATLAS_NEO4J_URI")
        or os.environ.get("ATLAS_NEO4J_URI")
        or DEFAULT_BOLT_URI
    ).strip()
    user = (
        os.environ.get("SINGULARITY_ATLAS_NEO4J_USER")
        or os.environ.get("ATLAS_NEO4J_USER")
        or DEFAULT_BOLT_USER
    ).strip()
    password = (
        os.environ.get("SINGULARITY_ATLAS_NEO4J_PASSWORD")
        or os.environ.get("ATLAS_NEO4J_PASSWORD")
        or DEFAULT_BOLT_PASSWORD
    )
    return {"uri": uri, "user": user, "password": password}


def seen_log_path(workspace: Path | None = None) -> Path:
    return (workspace or project_root()) / "singularity_intake_seen.jsonl"


def job_log_path(workspace: Path | None = None) -> Path:
    return (workspace or project_root()) / "singularity_intake_jobs.jsonl"


def _bolt_host_is_loopback(uri: str) -> bool:
    host = (urlparse(uri).hostname or "").rstrip(".").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _allow_remote() -> bool:
    flag = os.environ.get("SINGULARITY_ATLAS_ALLOW_REMOTE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def probe_singularity_atlas() -> SingularityProbe:
    """Return reachability without raising. Safe to call from GET handlers."""
    cfg = connection_settings()
    uri = cfg["uri"]
    loopback = _bolt_host_is_loopback(uri)
    try:
        from neo4j import GraphDatabase  # type: ignore[import-untyped]
    except ImportError:
        return SingularityProbe(
            available=False,
            reason="neo4j driver is not installed (optional extra 'singularity')",
            uri=uri,
            driver_installed=False,
            loopback=loopback,
        )
    if not loopback and not _allow_remote():
        return SingularityProbe(
            available=False,
            reason="Bolt URI is not loopback; set SINGULARITY_ATLAS_ALLOW_REMOTE=1 to override",
            uri=uri,
            driver_installed=True,
            loopback=False,
        )
    driver = None
    try:
        driver = GraphDatabase.driver(
            uri,
            auth=(cfg["user"], cfg["password"]),
            connection_timeout=CONNECT_TIMEOUT_SECONDS,
        )
        driver.verify_connectivity()
        return SingularityProbe(
            available=True,
            reason="reachable",
            uri=uri,
            driver_installed=True,
            loopback=loopback,
        )
    except Exception as exc:
        return SingularityProbe(
            available=False,
            reason=f"unavailable ({type(exc).__name__})",
            uri=uri,
            driver_installed=True,
            loopback=loopback,
        )
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def _fetch_stories_from_neo4j(limit: int = MAX_FETCH) -> list[dict[str, Any]]:
    from neo4j import GraphDatabase  # type: ignore[import-untyped]

    cfg = connection_settings()
    driver = GraphDatabase.driver(
        cfg["uri"],
        auth=(cfg["user"], cfg["password"]),
        connection_timeout=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (s:Story)
                OPTIONAL MATCH (s)-[a:ABOUT]->(v:Vector)
                WITH s, collect({vector: v.name, score: a.score}) AS vecs
                OPTIONAL MATCH (s)-[:MENTIONS]->(e:Entity)
                RETURN s, vecs, collect(DISTINCT e.name) AS entities
                ORDER BY coalesce(s.published_at, s.ingested_at) DESC
                LIMIT $limit
                """,
                limit=int(limit),
            )
            stories: list[dict[str, Any]] = []
            for record in result:
                raw = record["s"]
                story = dict(raw) if not isinstance(raw, dict) else dict(raw)
                extra = story.get("extra")
                if isinstance(extra, str):
                    try:
                        story["extra"] = json.loads(extra)
                    except json.JSONDecodeError:
                        story["extra"] = {}
                story["vectors"] = {
                    item["vector"]: float(item["score"] or 0.0)
                    for item in (record["vecs"] or [])
                    if item and item.get("vector")
                }
                story["entities"] = [name for name in (record["entities"] or []) if name]
                stories.append(story)
            return stories
    finally:
        driver.close()


def _normalized(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _keyword_hits(text: str) -> list[str]:
    return [term for term in RELEVANCE_TERMS if term in text]


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _vector_scores(raw: object) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        return {}
    scores: dict[str, float] = {}
    for key, value in raw.items():
        name = str(key or "").strip().casefold()
        if not name:
            continue
        try:
            scores[name] = float(value)
        except (TypeError, ValueError):
            continue
    return scores


def relevance_for(story: Mapping[str, Any]) -> tuple[bool, str, float]:
    """Return (relevant, rationale, rank score) for one story dict."""
    title = str(story.get("title") or "").strip()
    summary = str(story.get("summary") or "").strip()
    entities = tuple(str(item) for item in (story.get("entities") or []) if str(item).strip())
    vectors = _vector_scores(story.get("vectors"))
    blob = _normalized(" ".join((title, summary, " ".join(entities))))
    hits = _keyword_hits(blob)
    preferred = {
        name: score
        for name, score in vectors.items()
        if name in PREFERRED_VECTORS and score >= PREFERRED_VECTOR_FLOOR
    }
    salience = 0.0
    try:
        salience = float(story.get("salience") or 0.0)
    except (TypeError, ValueError):
        salience = 0.0
    rank = salience * 0.25 + sum(
        score * PREFERRED_VECTORS[name] for name, score in preferred.items()
    ) + 0.15 * len(hits)

    if preferred:
        names = ", ".join(f"{name} {score:.2f}" for name, score in sorted(preferred.items()))
        extra = f"; terms: {', '.join(hits[:4])}" if hits else ""
        return True, f"operational-risk overlap via {names}{extra}", rank
    if len(hits) >= KEYWORD_ONLY_HITS:
        return True, "operational-risk terms: " + ", ".join(hits[:6]), rank
    if hits:
        return False, "only a weak term overlap: " + ", ".join(hits), rank
    if vectors:
        return False, "themes are outside the operational-risk lens: " + ", ".join(
            sorted(vectors)
        ), rank
    return False, "no operational-risk theme or term overlap", rank


def load_seen_keys(path: Path) -> tuple[set[str], set[str]]:
    """Return previously processed story ids and URL variants."""
    ids: set[str] = set()
    urls: set[str] = set()
    if not path.is_file():
        return ids, urls
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ids, urls
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        story_id = str(record.get("story_id") or "").strip()
        if story_id:
            ids.add(story_id)
        url = str(record.get("url") or "").strip()
        if url:
            urls.update(url_dedup_variants(url))
    return ids, urls


def _append_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def record_source_job(
    pull: SourcePull,
    *,
    workspace: Path | None = None,
    draft_id: str = "",
) -> None:
    """Persist the decision table. Failures are logged, never raised."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    seen_records = []
    for item in pull.skipped:
        if item.decision not in SEEN_DECISIONS:
            continue
        seen_records.append({
            "at": now,
            "decision": item.decision,
            "draft_id": draft_id,
            "rationale": item.rationale,
            "story_id": item.story_id,
            "title": item.title,
            "url": item.url,
        })
    job = {
        "at": now,
        "draft_id": draft_id,
        "note": pull.note,
        "probe": pull.probe.to_dict(),
        "status": pull.status,
        "summary": pull.to_dict()["summary"],
        "selected_ids": [item.story_id for item in pull.selected],
        "rationale": [item.rationale for item in pull.selected],
    }
    try:
        _append_jsonl(seen_log_path(workspace), seen_records)
        _append_jsonl(job_log_path(workspace), [job])
    except OSError as exc:
        logger.warning("could not persist Singularity Atlas intake log: %s", exc)


def record_submitted_stories(
    stories: Sequence[Mapping[str, Any]],
    *,
    workspace: Path | None = None,
    draft_id: str = "",
) -> None:
    """Mark submitted Singularity Atlas stories so they are not pulled again."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    records = []
    for item in stories:
        story_id = str(item.get("story_id") or "").strip()
        if not story_id:
            continue
        records.append({
            "at": now,
            "decision": "selected",
            "draft_id": draft_id,
            "rationale": str(item.get("rationale") or "queued to Review"),
            "story_id": story_id,
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
        })
    try:
        _append_jsonl(seen_log_path(workspace), records)
    except OSError as exc:
        logger.warning("could not persist submitted Singularity Atlas stories: %s", exc)


def compose_material(paste: str, pull: SourcePull) -> str:
    parts: list[str] = []
    stripped = paste.strip()
    if stripped:
        parts.append(stripped)
    if pull.selected:
        parts.append(
            "Singularity Atlas pull (optional local graph). Public feed stories "
            "only; Loop archive editions are excluded on rights grounds. Each "
            "block is one story with its cited public URL."
        )
        parts.extend(item.as_unit() for item in pull.selected)
    return "\n\n".join(parts)


def collect_relevant_stories(
    *,
    include: bool,
    workspace: Path | None = None,
    corpus_urls: Iterable[str] = (),
    fetch_stories: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    probe: SingularityProbe | None = None,
    limit: int = MAX_SELECTED,
) -> SourcePull:
    """Select previously unseen, rights-eligible, operational-risk-relevant stories.

    ``fetch_stories`` is the test seam. Production uses Neo4j when available.
    """
    current_probe = probe or (
        SingularityProbe(
            available=True,
            reason="injected fetch",
            uri=connection_settings()["uri"],
            driver_installed=True,
            loopback=True,
        )
        if fetch_stories is not None
        else probe_singularity_atlas()
    )
    if not include:
        return SourcePull(
            status="skipped",
            probe=current_probe,
            note="Operator did not opt into the Singularity Atlas source.",
        )
    if not current_probe.available and fetch_stories is None:
        return SourcePull(
            status="unavailable",
            probe=current_probe,
            note=(
                "Singularity Atlas was requested but is not reachable; "
                "continuing with pasted material only."
            ),
        )

    try:
        raw_stories = list(fetch_stories() if fetch_stories is not None else _fetch_stories_from_neo4j())
    except Exception as exc:
        failed = SingularityProbe(
            available=False,
            reason=f"read failed ({type(exc).__name__})",
            uri=current_probe.uri,
            driver_installed=current_probe.driver_installed,
            loopback=current_probe.loopback,
        )
        logger.warning("Singularity Atlas story read failed: %s", type(exc).__name__)
        return SourcePull(
            status="unavailable",
            probe=failed,
            note="Singularity Atlas could not be read; pasted material is unchanged.",
        )

    seen_ids, seen_urls = load_seen_keys(seen_log_path(workspace))
    corpus = {variant for url in corpus_urls if url for variant in url_dedup_variants(url)}
    skipped: list[StorySkip] = []
    ranked: list[SelectedStory] = []
    batch_urls: set[str] = set()

    for raw in raw_stories:
        if not isinstance(raw, Mapping):
            continue
        story_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip() or "Untitled story"
        origin = str(raw.get("origin") or "feed").strip().casefold()
        url = str(raw.get("url") or "").strip()
        if not story_id:
            continue
        if origin == "archive":
            skipped.append(StorySkip(
                story_id=story_id, title=title, url=url,
                decision="skipped_rights",
                rationale="Loop archive edition excluded (all-rights-reserved; not a public feed item).",
            ))
            continue
        if not is_public_http_url(url):
            skipped.append(StorySkip(
                story_id=story_id, title=title, url=url,
                decision="skipped_provenance",
                rationale="Story has no admitted public HTTP(S) URL.",
            ))
            continue
        if host_is_denied(url):
            skipped.append(StorySkip(
                story_id=story_id, title=title, url=url,
                decision="skipped_denied_host",
                rationale="Source host is excluded by the public-identity policy.",
            ))
            continue
        canonical = canonical_public_url(url)
        url_keys = set(url_dedup_variants(canonical))
        if story_id in seen_ids or url_keys & seen_urls:
            skipped.append(StorySkip(
                story_id=story_id, title=title, url=canonical,
                decision="skipped_seen",
                rationale="Already processed in a prior Singularity Atlas intake pull.",
            ))
            continue
        if url_keys & corpus or url_keys & batch_urls:
            skipped.append(StorySkip(
                story_id=story_id, title=title, url=canonical,
                decision="skipped_duplicate",
                rationale="Same source URL is already in the governed corpus or this pull.",
            ))
            continue
        relevant, rationale, rank = relevance_for(raw)
        if not relevant:
            skipped.append(StorySkip(
                story_id=story_id, title=title, url=canonical,
                decision="skipped_irrelevant",
                rationale=rationale,
            ))
            continue
        entities = tuple(
            str(item).strip()
            for item in (raw.get("entities") or [])
            if str(item).strip()
        )
        ranked.append(SelectedStory(
            story_id=story_id,
            title=title,
            url=canonical,
            source=str(raw.get("source_label") or raw.get("source") or "").strip(),
            summary=str(raw.get("summary") or "").strip(),
            origin=origin or "feed",
            published_at=str(raw.get("published_at") or raw.get("ingested_at") or "").strip(),
            vectors=_vector_scores(raw.get("vectors")),
            entities=entities,
            salience=_safe_float(raw.get("salience")),
            rationale=rationale,
            relevance_score=rank,
        ))
        batch_urls.update(url_keys)

    ranked.sort(key=lambda item: (-item.relevance_score, item.story_id))
    selected = ranked[: max(0, int(limit))]
    for extra in ranked[len(selected):]:
        skipped.append(StorySkip(
            story_id=extra.story_id,
            title=extra.title,
            url=extra.url,
            decision="skipped_cap",
            rationale=f"Relevant but over the {limit}-story pull cap; left for a later prepare.",
        ))

    if selected:
        status: Literal["skipped", "unavailable", "empty", "selected"] = "selected"
        note = (
            f"Included {len(selected)} public-feed stor"
            f"{'y' if len(selected) == 1 else 'ies'} with operational-risk overlap; "
            f"skipped {len(skipped)}."
        )
    else:
        status = "empty"
        note = (
            "Singularity Atlas was reachable but no unseen public-feed story "
            "matched the operational-risk filter."
        )
    return SourcePull(
        status=status,
        probe=current_probe,
        selected=selected,
        skipped=skipped,
        note=note,
    )
