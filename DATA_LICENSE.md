# Data and Documentation License

This file separates the project’s software license from the rights that may
apply to data, documentation, source publications, and generated output.

## Source code

Unless a file states otherwise, source code authored for Contingency Atlas is
licensed under the [Apache License 2.0](LICENSE), SPDX identifier
`Apache-2.0`.

## Project documentation

Original project documentation in this repository is licensed under the
Apache License 2.0 unless a file states otherwise. Quotations, book and product
titles, organization names, trademarks, and linked third-party material remain
subject to their owners’ rights.

## Synthetic fixtures

Data files that are both:

1. created specifically by the Contingency Atlas project; and
2. conspicuously labeled `synthetic`

are dedicated to the public domain under
[CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/),
SPDX identifier `CC0-1.0`, to the extent the contributor has rights to make
that dedication. The bundled fixtures identify that license in
[`examples/fixture_manifest.json`](examples/fixture_manifest.json); the full
legal code is included at [`LICENSES/CC0-1.0.txt`](LICENSES/CC0-1.0.txt).

A fixture is not synthetic merely because a model edited or summarized a
real-world source. Synthetic fixtures must not contain copied third-party
expression, personal data, or allegations about an identifiable real party.

## Project-authored research metadata and summaries

Where a released data file explicitly identifies this license, the project’s
original provenance metadata, independent factual summaries, and independently
created taxonomy annotations are licensed under
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/),
SPDX identifier `CC-BY-4.0`.

Attribution should include:

> Contingency Atlas contributors, record identifier, release or commit, and
> source URL.

The license applies only to the project-authored contribution. It does not
grant rights in the underlying source publication, third-party database,
trademark, personal data, or facts subject to other legal restrictions.

## Public-source records

Contingency Atlas cites public sources; it does not relicense them. Source
publishers retain any rights they may have in their publications. A URL,
factual citation, short quotation, or independently written summary does not
imply that the source licensed its content under this project’s terms.

Users must consult the source and its applicable terms before copying,
redistributing, scraping, archiving, or submitting source material to a model.

## Operator-imported MITRE ATLAS data

The optional MITRE ATLAS overlay reads an operator-supplied release file into
the local database. That upstream knowledge-base content is not bundled with
Contingency Atlas and is not relicensed as project-authored data. The official
`mitre-atlas/atlas-data` repository identifies its content as copyright
2021–2026 MITRE and licensed under the Apache License 2.0.

Imported tactic, technique, mitigation, and case-study records — including
their identifiers, relationships, names, and descriptions — retain the
upstream terms and notices. The importer records the content version, format
version, operator-asserted source URL, and source-file SHA-256 so a local or
redistributed copy can be traced to the exact input. The importer records but
does not fetch or authenticate that URL; an independently supplied expected
digest can verify the exact bytes before parsing. Anyone redistributing
imported records or an export containing them is responsible for preserving
the applicable upstream copyright, license, release, attribution, and
trademark notices.

Project-authored scenario mappings, rationales, and review decisions do not
change the license of the underlying ATLAS objects. Conversely, the upstream
license does not convert unrelated Contingency Atlas records into MITRE
content. MITRE ATLAS™ is referenced only for identification; no affiliation,
sponsorship, approval, or endorsement by MITRE is implied.

## Excluded material

No license is granted by this project for:

- employer, client, confidential, personal, restricted, or subscription
  material;
- third-party articles, reports, filings, images, tables, taxonomies,
  databases, or schemas;
- ORX member, subscription, loss-event, news, scenario, taxonomy, identifier,
  schema, or other proprietary content;
- third-party trademarks, names, logos, or likenesses; or
- model weights and model outputs governed by another provider’s terms.

Such material is not intended to be included in the repository.

## Generated output

The repository license does not determine whether a particular model output is
protectable, accurate, safe to use, or free of third-party rights. Output may
reflect its input sources, model license, service terms, and applicable law.
Users are responsible for reviewing those constraints before use or
redistribution.

## File-level notices control

If a file carries a more specific license or attribution notice, that notice
controls for that file. Dependency licenses are summarized in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

This document describes the project’s intended licensing boundaries and is not
legal advice.
