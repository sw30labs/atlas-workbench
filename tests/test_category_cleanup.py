"""Tests for rule-based category cleanup."""


class TestCategoryCleanup:
    def test_physical_resilience_alignment(self):
        from workbench_atlas.phases.category_cleanup import infer_category
        cat, kw = infer_category({
            "title": "Regional flood interrupts a recovery site",
            "description": "A severe storm limits physical access",
            "risk_category": "Digital resilience",
        })
        assert cat == "Physical resilience"
        assert kw in {"flood", "storm", "physical site"}

    def test_cloud_identity_stays_digital(self):
        from workbench_atlas.phases.category_cleanup import infer_category
        cat, _ = infer_category({
            "title": "Cloud Identity Concentration Failure",
            "description": "Shared cloud identity provider outage",
            "risk_category": "Digital resilience",
        })
        assert cat == "Digital resilience"

    def test_apply_cleanup(self, empty_db):
        from workbench_atlas.db import upsert_scenario, get_scenario
        from workbench_atlas.phases.category_cleanup import apply_category_cleanup

        upsert_scenario(empty_db, {
            "scenario_id": "ATLAS-MISLABELED",
            "title": "Regional flood interrupts a recovery site",
            "description": "A severe storm limits physical access",
            "risk_category": "Digital resilience",
            "status": "active",
        })
        result = apply_category_cleanup(empty_db, dry_run=False)
        assert result.applied >= 1
        got = get_scenario(empty_db, "ATLAS-MISLABELED")
        assert got["risk_category"] == "Physical resilience"
