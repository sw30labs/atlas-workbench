# Post-Incident Review - 2026-INC-0107 - Cloud region failover

> **SYNTHETIC — fictional ACME Bank demo data. Not real ACME or any bank's data.**

| Field | Value |
|---|---|
| PIR reference | PIR-2026-009 |
| Incident | 2026-INC-0107 (Major Incident, P1) |
| Services | Customer Identity & Access (CIAM); Mobile Banking App |
| Date | Tuesday 3 March 2026 |
| Duration | 6 h 10 min (07:12 - 13:22 GMT) |
| Impact tolerance | 2 hours (CIAM) - **breached** |
| Author | Major Incident Manager, Digital |
| Status | Final, approved 2 April 2026 |

## Summary

At 07:12 a network policy change in ACME's cloud landing zone removed DNS resolution for the primary-region endpoints of CIAM hosted in AWS (eu-west-2). Customers could not log in to the Mobile App or Online Banking (Web). The team invoked regional failover at 07:58. Failover took far longer than the 60 minutes assumed in the self-assessment because the session store had to be promoted manually and reserved capacity in the secondary region was sized for 60% of peak. No fault on the cloud provider's side was identified; the root cause was internal to ACME.

## Timeline

| Time | Event |
|---|---|
| 07:12 | Landing-zone network policy change applied (CHG-2026-03117) |
| 07:19 | Login failure alerts; contact centre call volumes rise |
| 07:58 | Regional failover invoked |
| 09:40 | Secondary region at capacity; login throttling applied |
| 11:05 | Session store promoted manually; throttling eased |
| 13:22 | Full service restored in secondary region |

## Impact

- Customers unable to log in at peak: ~380,000; failed login attempts: 1.26 million
- Downtime for login: 370 minutes
- Contact centre: 3.4x normal call volume; average wait 41 minutes
- Gross loss: GBP 620,000 (compensation GBP 410k, overtime and support GBP 210k), booked under Basel ET6

## Findings

1. **O1 - Failover runbook not rehearsed end to end.** Session store promotion was a manual step never exercised in a live failover.
2. **O2 - Secondary capacity under-sized.** Reserved capacity covered 60% of peak, not 100%.
3. **O3 - Change risk misclassified.** Landing-zone network policy changes were treated as standard changes.

## Assumption invalidated

Self-assessment assumption **A-02** ("secondary cloud region can absorb 100% of CIAM peak load within 60 minutes") is **invalidated** by this incident. Evidence: this PIR and the incident timeline in the major-incident record.

The scenario team should revisit Cloud region loss (ACME-SCN-2025-014).

## Actions

| Ref | Action | Owner | Due | Status |
|---|---|---|---|---|
| A1 | Re-engineer CIAM session store replication to active-active | Head of Digital Identity | 2026-10-31 | In progress |
| A2 | Lower DNS TTLs for customer-facing endpoints and add failover health checks | Head of Cloud Platforms | 2026-05-31 | Closed |
| A3 | Increase reserved capacity in the secondary region to 100% of CIAM peak | Head of Cloud Platforms | 2026-06-30 | Closed |
| A4 | Add network policy changes in the landing zone to CAB high-risk category | Head of Service Management | 2026-04-30 | Closed |

Closure evidence for closed actions is held in GRC (GRC-ATT-26-05520, GRC-ATT-26-05521, GRC-ATT-26-05533).

_SYNTHETIC — fictional ACME Bank demo data. Not real ACME or any bank's data._
