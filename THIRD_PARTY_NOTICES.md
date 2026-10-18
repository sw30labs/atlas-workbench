# Third-Party Notices

Contingency Atlas depends on or integrates with third-party software. Those
projects remain governed by their own licenses. The project’s Apache License
2.0 does not replace or narrow third-party terms.

## Direct Python dependencies

The following table reflects the direct dependency declarations and package
metadata reviewed on 3 August 2026. Installed transitive dependencies may add
further notices.

| Project | Declared range | License | Purpose |
|---|---:|---|---|
| [LangGraph](https://github.com/langchain-ai/langgraph) | `>=1.0` | MIT | Stateful workflow orchestration |
| [HTTPX](https://github.com/encode/httpx) | `>=0.27` | BSD-3-Clause | HTTP client |
| [Pydantic](https://github.com/pydantic/pydantic) | `>=2.0` | MIT | Validation and settings models |
| [Tenacity](https://github.com/jd/tenacity) | `>=8.0` | Apache-2.0 | Retry behavior |
| [PyYAML](https://github.com/yaml/pyyaml) | `>=6.0` | MIT | YAML configuration |
| [pypdf](https://github.com/py-pdf/pypdf) | `>=4.0` | BSD-3-Clause | PDF text extraction for deep research |

Development dependency:

| Project | Declared range | License | Purpose |
|---|---:|---|---|
| [pytest](https://github.com/pytest-dev/pytest) | `>=8.0` | MIT | Test runner |

## Operator-imported MITRE ATLAS data

Contingency Atlas can import an operator-selected release from the official
[MITRE ATLAS data repository](https://github.com/mitre-atlas/atlas-data). The
upstream data is not bundled with this repository and is not downloaded by the
application. When an operator imports it, the local database can contain
upstream tactics, techniques, mitigations, case studies, relationships, names,
and descriptions alongside project-authored mapping decisions.

The upstream repository states:

- copyright 2021–2026 MITRE; and
- license: Apache License 2.0.

The exact upstream `LICENSE`, release statement, content version, format
version, operator-asserted source URL, and source-file SHA-256 should travel
with any copy or redistribution of imported content. The importer records but
does not fetch or authenticate that URL. The Contingency Atlas license does
not replace or narrow those terms.

MITRE ATLAS™ is used here only to identify the external knowledge base and
interoperability target. Contingency Atlas is an independent project; MITRE
has not affiliated with, sponsored, approved, or endorsed it. No MITRE or
MITRE ATLAS logo is bundled.

## Optional external integrations

[oMLX](https://github.com/jundot/omlx) is an optional external local-inference
server licensed under Apache-2.0. It is not bundled with Contingency Atlas.
Model weights served through oMLX have their own licenses and acceptable-use
terms.

[DriftLab](https://github.com/sw30labs/driftlab-dgx) is an optional external
runtime-governance integration licensed under Apache-2.0. It is not a declared
Python dependency or bundled with Contingency Atlas; the pipeline detects it
lazily and remains usable when it is absent. The reproducible command in the
README pins package version 2.1.0 to Git commit
`9f9281c95156dc0092c4b7e23acb9051638cc4fd`.

## Other material not bundled

Links to ORX, regulators, publishers, books, standards, news organizations, or
other research sources are citations and acknowledgments. Their sites,
publications, data, logos, and trademarks are not dependencies and are not
redistributed under the Contingency Atlas license. The optional MITRE ATLAS
import described above likewise begins with a separately obtained local file;
it is not part of the Contingency Atlas source distribution.

## Redistribution obligations

This repository does not vendor the dependencies listed above. Package
installers obtain them separately. Anyone distributing a binary, container,
application bundle, or vendored copy is responsible for:

1. generating a complete software bill of materials from the resolved build;
2. preserving applicable copyright and license notices;
3. including required license and NOTICE files;
4. reviewing transitive dependencies and optional extras; and
5. complying with model, dataset, font, icon, and asset licenses.

Dependency versions and metadata change. The lockfile or resolved environment
used for a release is authoritative for that release.

If a notice is missing or inaccurate, please open an issue or follow
[SECURITY.md](SECURITY.md) when disclosure would reveal sensitive information.
