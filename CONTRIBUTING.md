# Contributing

Thank you for helping improve Atlas Workbench. Contributions are welcome when
they preserve evidence quality, independent provenance, and the project’s
non-affiliation boundaries.

## Before you begin

Read:

- [README.md](README.md)
- [DISCLAIMER.md](DISCLAIMER.md)
- [DATA_PROVENANCE.md](DATA_PROVENANCE.md)
- [DATA_LICENSE.md](DATA_LICENSE.md)
- [SECURITY.md](SECURITY.md)

For a substantial feature, schema change, taxonomy change, or data-bearing
contribution, open an issue first so scope and review evidence can be agreed
before implementation.

Do not use a public issue for a vulnerability, credential, confidential data,
personal data, or accidental employer-material disclosure. Follow the private
process in [SECURITY.md](SECURITY.md).

## Development setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev,ontology]'
python -m pytest
```

Keep credentials in environment variables. The committed
`workbench-atlas.yaml` file is non-secret configuration only.

## Contribution workflow

1. Create a focused branch.
2. Make the smallest coherent change that solves the issue.
3. Add or update tests for changed behavior.
4. Update documentation when behavior, configuration, data requirements, or
   user-facing output changes.
5. Run the full test suite and relevant verification scripts.
6. Inspect the complete diff, including generated or binary artifacts.
7. Open a pull request describing intent, risks, validation, and provenance.

Avoid mixing broad formatting, generated files, and functional changes in one
pull request.

## Data-bearing contributions

Data receives stricter review than code. A contribution containing an event,
signal, scenario, fixture, screenshot, export, report, or research summary must
meet [DATA_PROVENANCE.md](DATA_PROVENANCE.md).

At minimum:

- classify each record as `public_primary`, `public_secondary`, or `synthetic`;
- supply the required record-level metadata;
- use an original summary rather than copied source text;
- separate sourced facts from researcher inference;
- confirm access and reuse terms;
- record human review status;
- use project-native identifiers and taxonomy names; and
- explain how the artifact was checked for restricted information.

Generated text is not evidence. A model cannot supply, verify, or replace a
source citation.

Output from the optional scout is also not evidence. A source proposal is only
a lead for a researcher to inspect. Before proposing a registry change or
turning a lead into a record:

- confirm that the endpoint is public and appropriate for bounded automated
  access;
- respect its terms, robots rules, rate expectations, and reuse restrictions;
- prefer an official feed or index over scraping full publications;
- record only the metadata necessary to identify a lead; and
- complete the normal provenance and human-review workflow separately.

Registry inclusion does not authorize copying, redistribution, model use, or
approval of material from that source.

Do not contribute:

- Employer code, data, methods, reports, communications, credentials, client
  information, or non-public knowledge;
- ORX member, subscription, loss-event, news, scenario, taxonomy, identifier,
  schema, or other proprietary content;
- paywalled, member-only, confidential, scraped, or untraceable material;
- personal data that is unnecessary for a documented public-interest purpose;
- real-party allegations disguised by simple name replacement;
- private hostnames, usernames, paths, endpoints, or infrastructure details;
  or
- secrets, tokens, model credentials, private keys, or local runtime stores.

When an example does not require a real institution, use “a hypothetical major
global bank” or an obviously fictional institution and label the scenario
hypothetical.

## Code expectations

- Support Python 3.12 or newer.
- Favor clear, typed, testable units.
- Keep deterministic analysis deterministic.
- Fail safely when provenance, configuration, or required input is missing.
- Redact secrets from state, logs, errors, traces, fixtures, and screenshots.
- Keep irreversible or destructive behavior behind an explicit operator gate.
- Do not silently turn model output into facts or evidence.
- Preserve the `ATLAS_AUTO_RETIRE` safety boundary.

## Documentation and naming

Use the public brand **Atlas Workbench** and the subtitle:

> An evidence-grounded banking operational-risk foresight and scenario engine

Use the tagline:

> Map tomorrow’s failure modes before they become today’s losses.

Avoid language claiming that the project predicts events, guarantees outcomes,
clones a named person, implements another provider’s proprietary taxonomy, or
represents an employer.

References to ORX and Nassim Nicholas Taleb should normally appear only in
approved disclaimer, provenance, and acknowledgment contexts. Other use
requires a clear, documented reason and additional review.

## Pull-request checklist

Include this checklist in a pull request and mark each applicable item:

- [ ] Tests pass.
- [ ] New behavior is documented.
- [ ] No secrets, personal data, private infrastructure, or local paths are
      present.
- [ ] No employer, client, subscription, or confidential material is present.
- [ ] Data records satisfy the provenance schema and have human review.
- [ ] Synthetic records are prominently labeled.
- [ ] Source access and reuse terms were checked.
- [ ] Images, PDFs, databases, and other binary artifacts were inspected.
- [ ] Dependency and third-party notices were updated if needed.
- [ ] The complete diff and tracked-file list were reviewed.

## Licensing contribution

By contributing, you represent that you have the right to submit the work and
agree that:

- code and ordinary project documentation are contributed under Apache-2.0;
- explicitly labeled original synthetic fixtures are irrevocably dedicated
  under CC0-1.0 to the extent legally possible, with the CC0 fallback license
  applying where a waiver is ineffective; and
- explicitly licensed original research metadata and summaries are
  contributed under CC-BY-4.0,

as described in [DATA_LICENSE.md](DATA_LICENSE.md). Do not submit material if
you cannot make the applicable grant.
