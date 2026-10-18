"""Bounded, read-only scenario summaries for portfolio API consumers.

The legacy aggregate remains unchanged. Offset pages are consistent within
one request; use a frozen export when a multi-page result must survive edits.
"""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs

from contingency_atlas.config import Settings
from contingency_atlas.schema import SCENARIO_ORIGINS

MAX_PAGE_SIZE = 200
MAX_OFFSET = 1_000_000
QUERY_TIMEOUT_SECONDS = 2.0
_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_STATUS = "lower(COALESCE(NULLIF(trim(status), ''), 'active'))"
_ORIGIN = "COALESCE(NULLIF(trim(origin), ''), 'legacy_unknown')"
_SCORED = "CASE WHEN typeof(urgency_score) IN ('real','integer') THEN urgency_score END"
_SORTS = {"urgency": _SCORED, "title": "title COLLATE NOCASE", "year": "created_year", "id": "scenario_id"}
_KEYS = frozenset({"limit", "offset", "sort", "direction", "status", "origin", "category", "q"})
_SELECT = f"""
    scenario_id AS id, title, risk_category AS category, {_ORIGIN} AS origin,
    {_STATUS} AS status, {_SCORED} AS urgency_score, created_year AS year,
    impact_band AS impact_rank, financial_impact_band AS fin_impact,
    controllability, time_horizon, last_scored,
    substr(COALESCE(description, ''),1,400) AS description_excerpt,
    length(COALESCE(description, '')) > 400 AS description_truncated
"""


class PortfolioError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _parameters(query_string: str) -> dict:
    if not isinstance(query_string, str) or len(query_string) > 4096:
        raise PortfolioError("Portfolio query is too large")
    if re.search(r"%(?![0-9a-fA-F]{2})", query_string):
        raise PortfolioError("Invalid portfolio query encoding")
    try:
        raw = parse_qs(query_string, keep_blank_values=True, max_num_fields=16,
                       encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise PortfolioError("Invalid portfolio query") from exc
    if set(raw) - _KEYS or any(len(values) != 1 for values in raw.values()):
        raise PortfolioError("Portfolio query contains unknown or repeated parameters")
    params = {key: values[0] for key, values in raw.items()}
    for key, default, minimum, maximum in (("limit", 50, 1, MAX_PAGE_SIZE), ("offset", 0, 0, MAX_OFFSET)):
        value = params.get(key, str(default))
        if not _NUMBER.fullmatch(value) or len(value) > 7 or not minimum <= int(value) <= maximum:
            raise PortfolioError(f"{key} must be an integer from {minimum} to {maximum}")
        params[key] = int(value)
    params.setdefault("sort", "urgency")
    params.setdefault("direction", "desc" if params["sort"] in {"urgency", "year"} else "asc")
    if params["sort"] not in _SORTS or params["direction"] not in {"asc", "desc"}:
        raise PortfolioError("Choose sort=urgency|title|year|id and direction=asc|desc")
    params.setdefault("status", "all")
    params.setdefault("origin", "all")
    if params["status"] not in {"all", "active", "draft", "retired"}:
        raise PortfolioError("status must be all, active, draft or retired")
    if params["origin"] not in {"all", *SCENARIO_ORIGINS}:
        raise PortfolioError("origin must be all or a recognized provenance origin")
    for key, maximum in (("q", 200), ("category", 160)):
        raw_value = params.get(key, "")
        value = raw_value.strip()
        if len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in raw_value):
            raise PortfolioError(f"{key} must contain at most {maximum} printable characters")
        params[key] = value
    return params


def build_scenario_page(settings: Settings, query_string: str = "") -> dict:
    """Return one deterministic summary page without migrating or creating stores."""
    params = _parameters(query_string)
    conditions, values = [], []
    for name, expression in (("status", _STATUS), ("origin", _ORIGIN)):
        if params[name] != "all":
            conditions.append(f"{expression} = ?")
            values.append(params[name])
    if params["category"]:
        conditions.append("risk_category = ? COLLATE NOCASE")
        values.append(params["category"])
    if params["q"]:
        literal = params["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append("(title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')")
        values.extend([f"%{literal}%"] * 2)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    sort = _SORTS[params["sort"]]
    direction = params["direction"].upper()
    # NULL/unscored rows remain explicit and sort last in either direction.
    # rowid also makes old stores with duplicate/missing IDs deterministic.
    order = f" ORDER BY ({sort} IS NULL) ASC, {sort} {direction}, scenario_id ASC, rowid ASC"
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    try:
        path = Path(settings.scenario_db_path).expanduser().resolve()
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute("BEGIN")
            if time.monotonic() > deadline:
                raise PortfolioError("Portfolio query exceeded its time budget; narrow the filters", 503)
            total = connection.execute("SELECT COUNT(*) FROM scenarios" + where, values).fetchone()[0]
            rows = connection.execute("SELECT " + _SELECT + " FROM scenarios" + where + order + " LIMIT ? OFFSET ?",
                                      [*values, params["limit"], params["offset"]]).fetchall()
            items = [dict(row) for row in rows]
    except (OSError, sqlite3.Error) as exc:
        if isinstance(exc, sqlite3.OperationalError) and "interrupted" in str(exc).lower():
            raise PortfolioError("Portfolio query exceeded its time budget; narrow the filters", 503) from exc
        raise PortfolioError("The scenario store is unavailable; check workspace health", 503) from exc
    for item in items:
        item["description_truncated"] = bool(item["description_truncated"])
    next_offset = params["offset"] + len(items)
    has_more = next_offset < total
    continuation_limited = has_more and next_offset > MAX_OFFSET
    return {
        "scenarios": items,
        "page": {"offset": params["offset"], "limit": params["limit"], "returned": len(items),
                 "total": total, "has_more": has_more,
                 "next_offset": next_offset if has_more and not continuation_limited else None,
                 "continuation_limited": continuation_limited},
        "sort": {"by": params["sort"], "direction": params["direction"]},
        "filters": {key: params[key] for key in ("status", "origin", "category", "q")},
        "consistency": "Each page is a read-only snapshot; live changes can shift later offsets. Use a frozen export for a stable multi-page record.",
    }


class PortfolioHandlerMixin:
    def _portfolio_get(self, path: str) -> bool:
        """Call after the main handler authorizes this GET request."""
        if path != "/api/scenarios":
            return False
        from contingency_atlas.web.server import get_settings
        try:
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            self._send_json(build_scenario_page(get_settings(), query))
        except PortfolioError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
        return True
