"""Activation preserves valid YAML semantics and failed writes preserve the file."""

from types import SimpleNamespace

import pytest
import yaml

from workbench_atlas.web import context_setup


@pytest.fixture
def activation(tmp_path, monkeypatch):
    path = tmp_path / "fictional-configuration.yaml"
    settings = SimpleNamespace(context_db_path=tmp_path / "fictional-private.db")
    monkeypatch.setattr(context_setup, "yaml_config_path", lambda: path)
    monkeypatch.delenv("ATLAS_CONTEXT_ENABLED", raising=False)
    return path, settings


@pytest.mark.parametrize("original", [
    '{provider: omlx, organization_context: {enabled: false, db_path: private.db, future_field: {mode: retained}}}\n',
    '"organization_context": {enabled: false, db_path: private.db}\nprovider: omlx\n',
    '  provider: omlx\n  organization_context:\n    enabled: false\n    db_path: private.db\n',
    '---\nprovider: omlx\n...\n',
    'provider: omlx\norganization_context:\n  enabled: false\n  future_field: [one, two]\nfuture_section: {preserved: true}\n',
    'defaults: &context\n  enabled: false\n  db_path: private.db\norganization_context: *context\n',
    'organization_context: &context\n  enabled: false\n  db_path: private.db\nfuture_section: *context\n',
])
def test_activation_preserves_complete_mapping_for_valid_yaml_styles(activation, original):
    path, settings = activation
    path.write_text(original)
    parsed = yaml.safe_load(original)
    section = parsed.get("organization_context") or {}
    expected = {**parsed, "organization_context": {
        **section, "enabled": True, "db_path": section.get("db_path") or str(settings.context_db_path),
    }}

    result = context_setup.enable_context(settings, actor="Synthetic operator")

    assert result["enabled"] is True
    assert yaml.safe_load(path.read_text()) == expected
    assert not settings.context_db_path.exists()
    assert list(path.parent.glob(".atlas-config-*")) == []


def test_simple_block_edit_preserves_unrelated_text_and_future_context_fields(activation):
    path, settings = activation
    original = ("# Synthetic operator comment\nprovider: omlx\norganization_context:\n"
                "  enabled: false\n  future_option: kept\n"
                "future_section:\n  # Preserve this comment too\n  mode: retained\n")
    path.write_text(original)

    context_setup.enable_context(settings, actor="Synthetic operator")

    text = path.read_text()
    assert text.startswith("# Synthetic operator comment\nprovider: omlx\n")
    assert text.endswith("future_section:\n  # Preserve this comment too\n  mode: retained\n")
    assert yaml.safe_load(text)["organization_context"]["future_option"] == "kept"


@pytest.mark.parametrize("original", ["[", "[]", "false", "0", "organization_context: []", "organization_context: false"])
def test_invalid_or_nonmapping_configuration_is_never_replaced(activation, original):
    path, settings = activation
    path.write_text(original)

    with pytest.raises(ValueError):
        context_setup.enable_context(settings, actor="Synthetic operator")

    assert path.read_text() == original
    assert list(path.parent.glob(".atlas-config-*")) == []
    assert not settings.context_db_path.exists()


@pytest.mark.parametrize("failure", ["serialization", "fsync", "replace"])
def test_verification_or_atomic_write_failure_preserves_existing_config(activation, monkeypatch, failure):
    path, settings = activation
    original = "provider: omlx\norganization_context: {enabled: false}\n"
    path.write_text(original)
    if failure == "serialization":
        monkeypatch.setattr(context_setup.yaml, "safe_dump", lambda *args, **kwargs: "invalid: [")
        error_type = ValueError
    else:
        def fail(*args, **kwargs):
            raise OSError("Synthetic write failure")
        monkeypatch.setattr(context_setup.os, failure, fail)
        error_type = OSError

    with pytest.raises(error_type):
        context_setup.enable_context(settings, actor="Synthetic operator")

    assert path.read_text() == original
    assert list(path.parent.glob(".atlas-config-*")) == []
    assert not settings.context_db_path.exists()


def test_failed_replace_does_not_create_config_and_retry_can_activate(activation, monkeypatch):
    path, settings = activation
    real_replace = context_setup.os.replace

    def fail(*args, **kwargs):
        raise OSError("Synthetic atomic replacement failure")

    monkeypatch.setattr(context_setup.os, "replace", fail)
    with pytest.raises(OSError):
        context_setup.enable_context(settings, actor="Synthetic operator")
    assert not path.exists()
    assert list(path.parent.glob(".atlas-config-*")) == []

    monkeypatch.setattr(context_setup.os, "replace", real_replace)
    context_setup.enable_context(settings, actor="Synthetic operator")
    assert yaml.safe_load(path.read_text())["organization_context"]["enabled"] is True


def test_checkout_activation_writes_the_gitignored_overlay(tmp_path, monkeypatch):
    from workbench_atlas import config

    checkout = tmp_path / "workbench-atlas.yaml"
    checkout.write_text("provider: omlx\n# keep me\n")
    overlay = tmp_path / "workbench-atlas.local.yaml"
    monkeypatch.delenv("ATLAS_CONFIG", raising=False)
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    monkeypatch.delenv("ATLAS_CONTEXT_ENABLED", raising=False)
    monkeypatch.setattr(config, "project_root", lambda: tmp_path)
    settings = SimpleNamespace(context_db_path=tmp_path / "organization-context.db")
    before = checkout.read_text()

    result = context_setup.enable_context(settings, actor="Synthetic operator")

    assert result["enabled"] is True
    assert checkout.read_text() == before
    parsed = yaml.safe_load(overlay.read_text())
    assert parsed == {"organization_context": {
        "enabled": True, "db_path": str(settings.context_db_path),
    }}
    overlay.write_text("provider: dgx\norganization_context:\n  enabled: false\n  db_path: kept.db\n")
    context_setup.enable_context(settings, actor="Synthetic operator")
    assert checkout.read_text() == before
    again = yaml.safe_load(overlay.read_text())
    assert again["provider"] == "dgx"
    assert again["organization_context"]["enabled"] is True
    assert again["organization_context"]["db_path"] == "kept.db"


def test_foreign_config_activation_does_not_touch_the_checkout(activation):
    from workbench_atlas.config import checkout_config_path

    path, settings = activation
    path.write_text("provider: omlx\n")
    checkout = checkout_config_path()
    before = checkout.read_text() if checkout.is_file() else None
    overlay = checkout.with_name("workbench-atlas.local.yaml")
    overlay_before = overlay.read_text() if overlay.is_file() else None

    context_setup.enable_context(settings, actor="Synthetic operator")

    assert yaml.safe_load(path.read_text())["organization_context"]["enabled"] is True
    assert not (path.parent / "workbench-atlas.local.yaml").exists()
    if before is None:
        assert not checkout.is_file()
    else:
        assert checkout.read_text() == before
    if overlay_before is None:
        assert not overlay.is_file()
    else:
        assert overlay.read_text() == overlay_before


def test_deployment_disable_precedes_any_write(activation, monkeypatch):
    path, settings = activation
    monkeypatch.setenv("ATLAS_CONTEXT_ENABLED", " false ")

    with pytest.raises(ValueError, match="deployment"):
        context_setup.enable_context(settings, actor="Synthetic operator")

    assert not path.exists()
    assert list(path.parent.iterdir()) == []
