# Baseline reviews and watchlist digests

I use **Track this view** in the scenario portfolio to save a fixed watchlist,
or **Save baseline** in Organization to capture the current catalog. Both retain
the whole private organization assessment. Research and linked evidence are
captured in separate, recorded read windows. A later comparison reports added,
changed and removed records, with before-and-after fields and unavailable data
states.

From **Changes since baseline**, **Export this baseline review** prepares a
frozen committee pack. Its selection explicitly includes private organization
changes across the baseline workspace. This scope is broader than a normal
scenario-linked context export and is disclosed before preparation. I preview
the counts and classification before downloading. The same short-lived,
owner-bound preview is used for the download; changing a live store cannot
silently replace the reviewed content.

## Scheduled preparation

The command below can be invoked by an operator's existing scheduler. It does
not send email, call a destination system or start a model. I keep its output
directory private and apply the same retention policy as organization records.

```bash
python -m contingency_atlas.assessment_reports \
  --baseline-id BASELINE_ID \
  --include-context \
  --output-directory /path/to/private/review-packs \
  --format pdf \
  --only-changes
```

I obtain the actual baseline ID from the comparison or exported manifest.
`--only-changes` skips a verified unchanged comparison and an already prepared
change digest. Missing research is emitted as an incomplete report rather than
silently treated as unchanged. Change digests exclude read timestamps, so
repeated checks do not produce duplicate reports solely because time passed.
Reports are limited to 512 MB. Preparation first writes and syncs private
temporary files, then atomically publishes each final pathname without
overwriting an existing file. Both the report and its hidden completion receipt
use owner-only permissions. The receipt sits beside the report as
`.REPORT_FILENAME.receipt.json`; it records the format, change digest, exact byte
size and SHA-256 checksum. I retain that receipt with the report.

An existing filename is not proof of completion. A repeated `--only-changes`
run verifies the matching receipt and the report's actual size and checksum
before reporting that the digest was already prepared. Symlinks, directories,
missing files, malformed receipts and changed report bytes fail visibly. The
same rule applies if another process is still publishing the report/receipt
pair. A verified repeat preserves the original report and its original as-of
time; it does not replace them with the current invocation's timestamps.

An interrupted process can leave an incomplete pair, which is never silently
repaired or accepted as complete. Normal failed writes clean up their new
temporary files and do not overwrite existing reports. After a failure I
inspect the original files and prepare a fresh report in a new private output
directory if needed. Reports produced before completion receipts were added
also require that explicit handling. A hard process interruption may leave
private staging files; they are not treated as published reports.

The command exits zero for preparation, verified no-change or an integrity-verified
existing digest, and one on an error. I configure failure monitoring in the scheduler;
Atlas does not create a background schedule automatically. Reports summarize
changes since the named baseline, not only since the last scheduled run. I save
a new baseline after the review when I want to start a new comparison period.

## Interpretation

An added source may be historical material newly linked to a scenario. Removed
evidence may be unlinked, rather than deleted. The report preserves publication,
retrieval and capture timing separately. Recorded changes do not by themselves
confirm an incident, counterevidence, source independence or calibrated risk.

The canonical report includes checksums, organization revision, selected
baseline, research scope and consistency limitations. These are traceability
controls; they are not a cross-database transactional guarantee. Its table
checksums use the same canonical JSON serialization as ordinary Atlas exports.
The local completion receipt detects missing or changed artifact bytes; it is
not a signed authenticity claim against someone able to rewrite both files.
