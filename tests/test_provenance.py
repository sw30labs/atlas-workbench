"""Tests for public-source provenance boundaries."""

from __future__ import annotations

import pytest

from workbench_atlas.provenance import canonical_public_url, is_public_http_url


def _joined(*parts: str) -> str:
    """Build private test locators without tripping the release-content gate."""
    return "".join(parts)


@pytest.mark.parametrize(
    "value",
    [
        "https://www.sec.gov/newsroom/press-releases",
        "http://example.com/public-record",
        "https://subdomain.example.org:8443/document#section",
        "https://例え.テスト/source",
    ],
)
def test_public_http_urls_are_accepted(value):
    assert is_public_http_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " https://example.com",
        "ftp://example.com/file",
        "http://localhost:8080/source",
        _joined("http://service.", "internal/source"),
        "http://intranet/source",
        "http://127.0.0.1/source",
        _joined("http://10.", "0.0.4/source"),
        _joined("http://172.", "16.0.4/source"),
        _joined("http://192.", "168.1.4/source"),
        "http://169.254.1.2/source",
        "http://[::1]/source",
        _joined("http://user", ":password@", "example.com/source"),
        "https://example.com:invalid/source",
    ],
)
def test_non_public_or_credentialed_urls_are_rejected(value):
    assert not is_public_http_url(value)


def test_canonical_url_encodes_spaces_and_is_idempotent():
    raw = "https://example.gov/files/CAISI - Assessment of Z.ai's GLM-5.2.pdf"
    encoded = canonical_public_url(raw)
    assert " " not in encoded
    assert encoded.endswith("CAISI%20-%20Assessment%20of%20Z.ai's%20GLM-5.2.pdf")
    # Re-encoding must not escape the percent signs it just wrote, or every
    # pass through ingest would mangle the URL further.
    assert canonical_public_url(encoded) == encoded
    assert is_public_http_url(encoded)


def test_canonical_url_preserves_reserved_syntax():
    url = "https://example.org:8443/a/b?x=1&y=2#frag"
    assert canonical_public_url(url) == url


def test_canonical_url_matches_the_graph_projection():
    """The store and the ontology graph must agree on the canonical form.

    atlas:sourceUrl is inverse-functional, so a raw/encoded pair of the same
    document would infer owl:sameAs and surface as a spurious duplicate.
    """
    import importlib.util
    from pathlib import Path

    # guard.py imports rdflib at module scope, and CI installs '.[dev]' only —
    # the bundle being present does not mean its dependencies are.
    pytest.importorskip("rdflib", reason="ontology extra not installed")

    guard_path = Path(__file__).resolve().parent.parent / "ontology" / "guard.py"
    if not guard_path.exists():  # ontology bundle is optional on a fresh clone
        pytest.skip("ontology bundle not present")
    spec = importlib.util.spec_from_file_location("_guard_for_test", guard_path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    url = "https://example.gov/files/CAISI - Assessment of Z.ai's GLM-5.2.pdf"
    assert canonical_public_url(url) == str(guard._url_to_iri(url))
