# Public-release checklist

Keep the repository private until every item below is complete. Changing
visibility is an administrative release action, not a substitute for review.

Items checked below were verified on 2026-08-29 against a separately created,
one-commit clean export. Public package metadata names “Contingency Atlas
contributors.” Named authorship is deliberately retained only where normal
scholarly attribution requires it (the authored white paper and citations);
personal contact data is not published. External URL availability, tagging,
repository settings, and visibility remain release-time actions.

- [x] Run `python -m pytest`, `python scripts/verify_all.py`,
      `python scripts/scout_sources.py validate`, and
      `python scripts/public_release_gate.py --history` from a clean clone.
- [x] Run a dedicated secret scanner against the worktree and complete Git
      history.
- [x] Inspect every tracked filename and blob; confirm that no runtime
      database, sidecar, generated export, model artifact, screenshot with
      sensitive content, or other binary is reachable. The only permitted
      binaries are the exact, digest-bound artifacts listed under
      `REVIEWED_BINARY_SHA256` in `scripts/public_release_gate.py`.
- [x] Confirm the only bundled records are the conspicuously fictional,
      CC0-1.0 fixtures listed in `examples/fixture_manifest.json`.
- [ ] Recheck the employer-independence disclosure, ORX exclusion and
      acknowledgment, Taleb no-persona statement, and all external links.
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
      Keep personal email addresses out of the public history unless one is
      intentionally public.
- [ ] Create the release tag first, then add an accurate `date-released` to
      `CITATION.cff`.
- [ ] Leave the canonical URLs in `pyproject.toml` and `CITATION.cff` on the
      current GitHub project until the real GitLab project path is known.
      Do not invent that path.
- [ ] Coordinate the visibility change with an administrator. Immediately
      enable the forge’s confidential vulnerability report. On GitLab that is
      a confidential issue to the maintainers. Verify that a reporter can use
      it, and test the documented security channel.
- [ ] Confirm the repository is public only after all post-change security
      settings have been verified.

The private reporting channel follows the forge that hosts the public
project. `SECURITY.md` must match that forge before visibility changes.
If the project stays on GitHub, private vulnerability reporting is documented
at
<https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository>.
GitLab uses confidential issues. Canonical URLs stay on the current GitHub
project until the real GitLab path is known.
