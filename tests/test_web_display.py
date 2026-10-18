"""Display diagnostics must not expose configured URL credentials."""

import json
from urllib.parse import urlunsplit

import pytest

from contingency_atlas.web import server
from contingency_atlas.web import settings as settings_ui
from contingency_atlas.web.display import safe_endpoint_message, safe_endpoint_url


def credential_url():
    # Wholly fictional negative fixture; never contacted or used as a credential.
    return urlunsplit(("http", ":".join(("synthetic-user", "synthetic-password"))
                      + "@127.0.0.1:8000", "/v1", "token=synthetic-query", "synthetic-fragment"))


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
    ("https://[::1]:8443/v1", "https://[::1]:8443/v1"),
    ("https://example.org:invalid/v1", ""),
    ("http://[invalid", ""),
    ("not an endpoint", ""),
    (None, ""),
])
def test_endpoint_display_preserves_safe_connection_location(url, expected):
    assert safe_endpoint_url(url) == expected


def test_url_and_probe_message_strip_credentials_and_query():
    url = credential_url()
    assert safe_endpoint_url(url) == "http://127.0.0.1:8000/v1"
    assert safe_endpoint_message(f"Request to '{url}' failed") == "Request to 'http://127.0.0.1:8000/v1' failed"
    assert safe_endpoint_message(None) is None


def test_model_status_handler_returns_only_sanitized_endpoint(settings, monkeypatch):
    configured = settings.model_copy(update={"omlx_base_url": credential_url()})
    monkeypatch.setattr(server, "get_settings", lambda: configured)
    monkeypatch.setattr(server.runner, "omlx_reachable", lambda *_args: False)
    handler = object.__new__(server.DashboardHandler)
    handler.path = "/api/omlx"
    handler._local_request_allowed = lambda: True
    handler._authorize = lambda *_args: True
    payloads = []
    handler._send_json = lambda payload, **_kwargs: payloads.append(payload)
    handler.do_GET()
    assert payloads[0]["base_url"] == "http://127.0.0.1:8000/v1"
    assert configured.omlx_base_url == credential_url()


def test_settings_scrubs_endpoint_credentials_from_all_probe_diagnostics(settings, monkeypatch):
    configured = settings.model_copy(update={"omlx_base_url": credential_url()})
    error = "Synthetic connection error for " + credential_url()
    monkeypatch.setattr(settings_ui, "_served_models", lambda *_args: (False, [], error, False, error))
    monkeypatch.setattr(settings_ui, "_scout_registry_payload", lambda: {})
    payload = settings_ui.build_settings_payload(configured)
    encoded = json.dumps(payload)
    for secret in ("synthetic-user", "synthetic-password", "synthetic-query", "synthetic-fragment"):
        assert secret not in encoded
    assert payload["inference"]["base_url"] == "http://127.0.0.1:8000/v1"
    assert "Synthetic connection error" in payload["inference"]["probe_error"]
