# Public-release checklist

The repository was made public on 2026-11-21 after the review below.
Changing visibility is an administrative release action, not a substitute
for review.

The content checks were re-verified on 2026-11-21 in this worktree. Public
package metadata names “Atlas Workbench contributors.” Named authorship is
deliberately retained only where normal scholarly attribution requires it
(the authored white paper and citations). Git commits and the annotated
`0.2.0` tag keep the maintainer identity that created them.

- [x] Run `python -m pytest`, `python scripts/verify_all.py`,
      `python scripts/scout_sources.py validate`, and
      `python scripts/public_release_gate.py --history`.
- [x] Run a dedicated secret scanner against the worktree and complete Git
      history.
- [x] Inspect every tracked filename and blob; confirm that no runtime
      database, sidecar, generated export, model artifact, screenshot with
      sensitive content, or other binary is reachable. The only permitted
      binaries are the exact, digest-bound artifacts listed under
      `REVIEWED_BINARY_SHA256` in `scripts/public_release_gate.py`.
- [x] Confirm the only bundled records are the conspicuously fictional,
      CC0-1.0 fixtures listed in `examples/fixture_manifest.json`.
- [x] Recheck the employer-independence disclosure, ORX exclusion and
      acknowledgment, Taleb no-persona statement, and the external links in
      those statements and the project metadata. On 2026-11-21 the ECB press
      link was updated to the page it now redirects to.
- [x] Verify that all non-synthetic records, if any are introduced later, have
      record-level public-source provenance and completed human review; confirm
      no pending, rejected, or `needs_update` record entered a released output.
- [x] Review optional traces, `PipelineResult.state` captures, job output, and
      other runtime artifacts for raw source material, local paths, endpoints,
      and model content; confirm none are tracked or packaged.
- [x] Confirm the bundled dashboard still enforces loopback-only binds and APIs.
- [x] Confirm scouting remains disabled without explicit opt-in, is bounded by
      time, response size, and item count, accepts only public HTTP(S) registry
      URLs, and emits proposals without writing evidence or review decisions.
- [x] Review dependency resolution, source/wheel contents, licenses, and
      third-party notices.
- [x] Decide whether the release metadata should name an individual author.
      Package files name “Atlas Workbench contributors” and contain no
      personal email address. Commit and tag metadata keep the existing
      maintainer identity, as described above.
- [x] The `0.2.0` tag was created on 2026-11-21. `CITATION.cff` records
      version `0.2.0` and `date-released: 2026-11-21`.
- [x] Confirm the canonical URLs in `pyproject.toml` and `CITATION.cff`
      are `https://github.com/sw30labs/atlas-workbench`. That repository is
      the project. Do not point them at another forge.
- [x] On 2026-11-21 the repository visibility was changed to public.
      GitHub private vulnerability reporting is enabled. The security
      policy is published, and the Security tab offers “Report a
      vulnerability” through a private advisory. Secret scanning and push
      protection are enabled.
- [x] The repository is public, and the post-change security settings
      above were verified on 2026-11-21.

The private reporting channel is GitHub private vulnerability reporting.
`SECURITY.md` must match that before visibility changes. The setting is
documented at
<https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository>.
