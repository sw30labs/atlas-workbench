# ADR-020: A CSV export is a zip, and the CLI writes that suffix

- **Status:** accepted
- **Date:** 2026-11-21

## Context

`atlas export --format csv` renders a zip of CSV files (relationships,
quoting, and a `manifest.json`). The command used to write those bytes
to the path the operator passed, including `brief.csv`. The file was a
zip with a CSV name, so Excel and pandas refused it.

## Decision

The archive bytes are unchanged. When the requested path does not
already end in `.zip`, the CLI appends `.zip` (`brief.csv` becomes
`brief.csv.zip`) and prints the path it wrote. A path that already
ends in `.zip` is left as given. The command does not overwrite an
existing file.

## Consequences

- A script that opened `brief.csv` as a zip must open `brief.csv.zip`.
- The filename now matches the media type `application/zip`.

## Alternatives considered

- A separate `csv-zip` format token. Rejected: `csv` already means this
  archive, and a second token would split one renderer in two.
- Writing a single flat CSV. Rejected: the archive is what keeps the
  relationship tables and the manifest together.
