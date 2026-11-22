"""Bulk ingest pods: folder -> one reviewable, staged batch.

Staging only; see docs/reference/bulk-ingest.md. Public API:

- :func:`run_batch` / :class:`BatchResult` — run every pod and write ``batch.json`` + handoff payloads.
- :func:`plan_submission` / :func:`submit_batch` — human-selected import through ``imports``.
- :func:`run_bulk_ingest_job` — entry point for a job-runner phase.
"""

from .inventory import Limits, build_inventory
from .models import BATCH_SCHEMA_VERSION, ROUTES
from .orchestrator import BatchCancelled, BatchResult, batches_dir, run_batch, run_bulk_ingest_job
from .submit import SUBMIT_ERROR_CODES, SubmitError, load_batch, plan_submission, submit_batch

__all__ = [
    "BATCH_SCHEMA_VERSION", "ROUTES", "SUBMIT_ERROR_CODES", "BatchCancelled", "BatchResult", "Limits",
    "SubmitError", "batches_dir", "build_inventory", "load_batch", "plan_submission", "run_batch",
    "run_bulk_ingest_job", "submit_batch",
]
