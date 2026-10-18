"""Tests for the external feature registry and association screen."""

import json

import pytest

from contingency_atlas.scoring.ext_features import (
    FeatureEntry,
    FeatureRegistry,
    benjamini_hochberg,
    block_bootstrap_log_rr,
    fisher_exact_enrichment,
    llm_null_pass,
    rate_ratio,
    run_screen,
    time_shift_null,
)
from contingency_atlas.scoring.ext_features import (
    test_associations as screen_associations,
)
from contingency_atlas.scoring.models import IncidentEvent
from contingency_atlas.scoring.synth_ext import generate_ext_series
from contingency_atlas.scoring.synthetic import R0Config, generate_r0
from contingency_atlas.scoring.telemetry import TelemetryLogger


def _events_from_grid(grid: dict[str, list[int]], n_periods: int) -> list[IncidentEvent]:
    events = []
    seq = 0
    for rid, periods in grid.items():
        for t in periods:
            seq += 1
            events.append(IncidentEvent(f"e{seq}", rid, t, 100.0))
    return events


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_fisher_exact_enrichment_known_table():
    # strongly enriched: 8/10 fire when on vs 2/30 when off
    p = fisher_exact_enrichment(8, 2, 2, 28)
    assert p < 0.01
    # no enrichment: rates equal
    p_flat = fisher_exact_enrichment(5, 5, 15, 15)
    assert p_flat > 0.4


def test_rate_ratio_smoothed():
    assert rate_ratio(8, 2, 2, 28) > 5
    assert rate_ratio(0, 0, 0, 0) is None


def test_benjamini_hochberg_monotone():
    q = benjamini_hochberg({"a": 0.001, "b": 0.01, "c": 0.5}, q=0.1)
    assert q["a"] <= q["b"] <= q["c"]
    assert q["a"] <= 0.1
    assert q["c"] > 0.1


def test_bootstrap_ci_excludes_zero_for_real_signal():
    rng_f = [1] * 10 + [0] * 30
    rng_y = [1, 1, 1, 1, 1, 1, 1, 0, 0, 0] + [0] * 28 + [1, 1]
    ci = block_bootstrap_log_rr(rng_f, rng_y, draws=200, seed=1)
    assert ci is not None and ci[0] > 0


# ---------------------------------------------------------------------------
# Registry + pre-registration
# ---------------------------------------------------------------------------


def test_registry_roundtrip_and_prereg_gate(tmp_path):
    reg = FeatureRegistry(tmp_path / "reg.yaml")
    reg.register(FeatureEntry("f1", "Feature One", "macro", "2026-01-01", series={0: 1.0}))
    reg.save()
    reg2 = FeatureRegistry(tmp_path / "reg.yaml")
    assert reg2.entries["f1"].series == {0: 1.0}
    with pytest.raises(ValueError):
        reg2.register(FeatureEntry("f1", "dup", "macro", "2026-01-01"))
    with pytest.raises(KeyError):
        reg2.require_registered("not-registered")


def test_unregistered_feature_cannot_be_tested(tmp_path):
    reg = FeatureRegistry(tmp_path / "reg.yaml")
    reg.register(FeatureEntry("f1", "f", "macro", "2026-01-01", series={t: 1.0 for t in range(12)}))
    # only registered candidates are screened; unregistered ids raise on demand
    with pytest.raises(KeyError):
        reg.require_registered("f2")


# ---------------------------------------------------------------------------
# Association screen end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def r0_screen(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ext")
    ds = generate_r0(R0Config())
    n_periods = 40
    reg = FeatureRegistry(tmp / "reg.yaml")
    for fid, spec in generate_ext_series(ds.events, ds.atoms, n_periods).items():
        reg.register(FeatureEntry(
            feature_id=fid, name=spec["name"], family=spec["family"],
            registered_at=spec["registered_at"], series=spec["series"],
        ))
    report = run_screen(
        reg, ds.events, [a.risk_id for a in ds.atoms],
        train_quarters=32, n_periods=n_periods, min_support=8, seed=1,
    )
    return ds, reg, report


def test_planted_strong_feature_accepted(r0_screen):
    ds, reg, report = r0_screen
    assert report["registry_status"]["ai-capex-concentration-high"] == "accepted"
    accepted_targets = {p["risk_id"] for p in report["accepted_pairs"]}
    identity_atoms = set(ds.truth["channels"]["identity"])
    assert accepted_targets <= identity_atoms  # no false channel attribution
    for p in report["accepted_pairs"]:
        assert p["q_value"] <= 0.10
        assert p["label"] == "associational_feature"


def test_noise_features_rejected_or_filtered(r0_screen):
    ds, reg, report = r0_screen
    # sanctions: enough support (blocky), tested, fails FDR -> rejected
    assert report["registry_status"]["sanctions-regime-active"] == "rejected"
    # rate-hike: support below s_min -> never tested -> stays candidate
    entry = reg.entries["rate-hike-quarter"]
    assert entry.status == "candidate"
    assert "insufficient support" in entry.notes


def test_weak_planted_feature_filtered_by_support(r0_screen):
    ds, reg, report = r0_screen
    entry = reg.entries["regional-outage-cluster"]
    assert entry.status == "candidate"  # never tested
    assert "insufficient support" in entry.notes


def test_holdout_not_used_for_discovery(r0_screen):
    """Every accepted pair must have its FDR decision from train data; the
    holdout table exists and is separate."""
    ds, reg, report = r0_screen
    screen = screen_associations(
        reg, ds.events, [a.risk_id for a in ds.atoms],
        train_quarters=32, n_periods=40, min_support=8, seed=1,
    )
    for res in screen["results"]:
        train_total = sum(res.table_train)
        hold_total = sum(res.table_holdout)
        assert train_total == 32
        assert hold_total == 8


def test_time_shift_null_flags_artifact():
    # feature perfectly mirrors the atom's own firing series: shifting
    # destroys the alignment -> null_ok (real alignment), whereas an
    # association that IMPROVES under most shifts would be flagged
    events = _events_from_grid({"a": [2, 3, 4, 10, 11, 18, 19, 20, 26, 27]}, 32)
    entry = FeatureEntry("f", "mirror", "macro", "2026-01-01",
                         series={t: 1.0 for t in [2, 3, 4, 10, 11, 18, 19, 20, 26, 27]})
    null = time_shift_null(entry, events, "a", train_quarters=32, n_periods=32)
    assert null["base_p"] < 0.05
    assert null["null_ok"] is True


# ---------------------------------------------------------------------------
# LLM null pass (mock clients)
# ---------------------------------------------------------------------------


def test_llm_null_pass_accept_and_reject(tmp_path):
    class Client:
        def __init__(self, text):
            self.text = text

        def chat(self, messages, **kwargs):
            return self.text

    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="null-test")
    entry = FeatureEntry("f", "Some macro feature", "macro", "2026-01-01")
    accept = llm_null_pass(
        entry, "risk-x", 3.0, 0.02,
        explain_client=Client("common seasonality in reporting cycles"),
        vote_client=Client('{"verdict": "accept", "reason": "null unconvincing"}'),
        explain_model="m1", vote_model="m2", telemetry=logger,
    )
    assert accept["verdict"] == "accept"
    reject = llm_null_pass(
        entry, "risk-x", 3.0, 0.02,
        explain_client=Client("obvious reporting artifact"),
        vote_client=Client('{"verdict": "reject", "reason": "artifact more plausible"}'),
        explain_model="m1", vote_model="m2", telemetry=logger,
    )
    assert reject["verdict"] == "reject"
    # explain + vote calls all instrumented
    assert len(logger.load_events()) == 4


# ---------------------------------------------------------------------------
# Adversarial null default-on (P6): fail-closed on client error + CLI flip
# ---------------------------------------------------------------------------


def _mirror_registry_and_events(tmp_path):
    """One feature perfectly mirroring its atom: passes FDR + holdout +
    deterministic null, so the LLM null pass is the deciding vote."""
    fires = [2, 3, 4, 10, 11, 18, 19, 20, 26, 27]
    events = _events_from_grid({"a": fires}, 32)
    reg = FeatureRegistry(tmp_path / "reg.yaml")
    reg.register(FeatureEntry(
        "f", "mirror", "macro", "2026-01-01",
        series={t: 1.0 for t in fires},
    ))
    return reg, events


class _BrokenClient:
    """Simulates an unreachable endpoint: every call raises."""

    def __init__(self, *args, **kwargs):
        pass

    def chat(self, messages, **kwargs):
        raise RuntimeError("endpoint down")

    def close(self):
        pass


def test_run_screen_llm_null_fail_closed_on_client_error(tmp_path):
    """Offline/broken endpoint: the pair is kept OUT and the failure is
    recorded on the pair and in the report — never a crash, never a
    silent acceptance (P6 fail-closed)."""
    reg, events = _mirror_registry_and_events(tmp_path)
    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="fail-closed")
    report = run_screen(
        reg, events, ["a"],
        train_quarters=32, n_periods=32, min_support=8, seed=1,
        llm_null={
            "explain_client": _BrokenClient(),
            "vote_client": _BrokenClient(),
            "explain_model": "m1",
            "vote_model": "m2",
            "telemetry": logger,
        },
    )
    assert report["accepted_pairs"] == []
    assert report["n_pairs_fdr_pass"] == 1
    rejected = report["rejected_pairs"]
    assert len(rejected) == 1
    llm_null = rejected[0]["llm_null"]
    assert llm_null["error"] is True
    assert llm_null["verdict"] == "reject"
    assert "fail-closed" in llm_null["reason"]
    assert report["n_llm_null_fail_closed"] == 1
    assert report["registry_status"]["f"] == "rejected"
    # the failed explain call was still telemetry-instrumented
    assert any(e["status"] == "error" for e in logger.load_events())


def test_ext_demo_default_attempts_llm_null(tmp_path, monkeypatch):
    """CLI default: an OMLX client is constructed and consulted even
    without --with-llm-null (the flag is now a deprecated no-op)."""
    from contingency_atlas.scoring.cli import eval_main, score_main

    r0 = tmp_path / "r0"
    assert score_main(["r0-generate", "--out", str(r0)]) == 0

    class AcceptingClient:
        calls = 0

        def __init__(self, settings):
            pass

        def chat(self, messages, **kwargs):
            type(self).calls += 1
            return '{"verdict": "accept", "reason": "null unconvincing"}'

        def close(self):
            pass

    monkeypatch.setattr("contingency_atlas.llm.OMLXClient", AcceptingClient)
    out = tmp_path / "ext"
    rc = eval_main([
        "ext-demo",
        "--risks", str(r0 / "r0_risks.json"),
        "--events", str(r0 / "r0_events.jsonl"),
        "--out", str(out),
    ])
    assert rc == 0
    assert AcceptingClient.calls > 0  # attempted by default
    report = json.loads((out / "ext_report.json").read_text())
    assert report["registry_status"]["ai-capex-concentration-high"] == "accepted"
    assert report["accepted_pairs"]
    for pair in report["accepted_pairs"]:
        assert pair["llm_null"]["verdict"] == "accept"


def test_ext_demo_no_llm_null_disables(tmp_path, monkeypatch):
    """--no-llm-null restores the deterministic-only screen: no client is
    constructed and no pair carries an LLM verdict."""
    from contingency_atlas.scoring.cli import eval_main, score_main

    r0 = tmp_path / "r0"
    assert score_main(["r0-generate", "--out", str(r0)]) == 0

    class GuardClient:
        constructed = 0

        def __init__(self, settings):
            type(self).constructed += 1

    monkeypatch.setattr("contingency_atlas.llm.OMLXClient", GuardClient)
    out = tmp_path / "ext"
    rc = eval_main([
        "ext-demo",
        "--risks", str(r0 / "r0_risks.json"),
        "--events", str(r0 / "r0_events.jsonl"),
        "--out", str(out),
        "--no-llm-null",
    ])
    assert rc == 0
    assert GuardClient.constructed == 0
    report = json.loads((out / "ext_report.json").read_text())
    assert report["registry_status"]["ai-capex-concentration-high"] == "accepted"
    assert report["n_rejected_llm_null"] == 0
    assert report["n_llm_null_fail_closed"] == 0
    for pair in report["accepted_pairs"]:
        assert pair["llm_null"] is None


def test_ext_demo_offline_default_is_fail_closed(tmp_path, monkeypatch):
    """Default behavior with a dead endpoint: every FDR survivor is kept
    out, the report records why, and the run still completes."""
    from contingency_atlas.scoring.cli import eval_main, score_main

    r0 = tmp_path / "r0"
    assert score_main(["r0-generate", "--out", str(r0)]) == 0
    monkeypatch.setattr("contingency_atlas.llm.OMLXClient", _BrokenClient)
    out = tmp_path / "ext"
    rc = eval_main([
        "ext-demo",
        "--risks", str(r0 / "r0_risks.json"),
        "--events", str(r0 / "r0_events.jsonl"),
        "--out", str(out),
    ])
    assert rc == 0
    report = json.loads((out / "ext_report.json").read_text())
    assert report["n_pairs_fdr_pass"] > 0
    assert report["accepted_pairs"] == []
    assert report["n_llm_null_fail_closed"] == report["n_pairs_fdr_pass"]
    assert report["registry_status"]["ai-capex-concentration-high"] == "rejected"
