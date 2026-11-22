"""Critic pod: batch-level issues for the reviewer. Deterministic."""

from __future__ import annotations

from .models import Candidate, DocumentResult, Issue

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def critique(results: list[DocumentResult], candidates: list[Candidate], link_issues: list[Issue]) -> list[Issue]:
    issues: list[Issue] = []
    by_file = {r.entry.path: r for r in results}
    for result in results:
        issues.extend(result.issues)
        path = result.entry.path
        for flag in result.flags:
            if flag.flag == "injection":
                issues.append(Issue("warning", "injection",
                                    f"{flag.reason}; excluded from extraction and never acted on. Excerpt: "
                                    f"{flag.excerpt[:200]!r}", path, flag.locator))
            elif flag.flag == "pii":
                issues.append(Issue("warning", "pii", flag.reason, path, flag.locator))
            elif flag.flag == "unreadable":
                issues.append(Issue("error", "unreadable", flag.reason, path))
            elif flag.flag in ("superseded", "duplicate"):
                issues.append(Issue("info", flag.flag, f"{flag.reason}; records not extracted", path))
        for route in result.routes:
            if route.confidence == "low" and route.route not in ("ignore", "quarantine"):
                issues.append(Issue("warning", "low_confidence_route",
                                    f"{route.route} ({route.target}, score {route.score:g}): "
                                    + "; ".join(route.reasons[-2:]), path, route.target))
    for issue in link_issues:
        issues.append(issue)
        if issue.code == "conflict":
            for c in candidates:
                for conflict in c.meta.get("conflicts", []):
                    for file in conflict["files"]:
                        if file in by_file:
                            by_file[file].flag("conflict", f"{c.kind} {conflict['field']} differs across "
                                               + " and ".join(conflict["files"]))
    for c in candidates:
        prov = c.provenance[0]
        for error in c.errors:
            code = "missing_required" if "Field required" in error else "validation_error"
            issues.append(Issue("error", code, f"{c.kind} {c.id}: {error}", prov.file, prov.locator, [c.id]))
    unique, seen = [], set()
    for issue in issues:
        key = (issue.severity, issue.code, issue.message, issue.file, issue.locator)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    unique.sort(key=lambda i: (_SEVERITY_ORDER[i.severity], i.code, i.file, i.locator, i.message))
    return unique
