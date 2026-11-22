# Security Policy

## Supported versions

| Version | Security support |
|---|---|
| Current default branch | Supported |
| Untagged snapshots and forks | Best effort only |

This research project does not promise a particular response or remediation
time, but maintainers will triage credible reports as promptly as practical.

## Report privately

When the project is public, use the forge’s confidential vulnerability report
if it has one. On GitLab, open a confidential issue and assign it to the
maintainers. Do not put exploit details in a public issue.

Until a public project path exists, contact the repository owner through a
private channel. Do not open an issue containing exploit details, credentials,
personal data, employer material, private infrastructure, or other sensitive
evidence.

Enabling and verifying that private channel immediately after any visibility
change is a mandatory step in
[docs/release/PUBLIC_RELEASE_CHECKLIST.md](docs/release/PUBLIC_RELEASE_CHECKLIST.md).

Include, when possible:

- a concise description and likely impact;
- affected version, commit, module, or endpoint;
- minimal reproduction steps or a proof of concept;
- whether credentials or sensitive data may have been exposed;
- suggested remediation;
- a safe way to contact you; and
- any disclosure deadline you are working under.

Redact live secrets and unnecessary personal data. Use placeholder values in a
proof of concept.

## Security scope

Examples of in-scope reports include:

- remote code execution, command injection, path traversal, or unsafe file
  access;
- authentication or authorization bypass in an exposed deployment;
- secret leakage through configuration, logs, traces, errors, dashboard
  responses, or generated artifacts;
- prompt or data pathways that expose restricted source material across
  isolation boundaries;
- unsafe deserialization or model/tool invocation;
- destructive maintenance behavior that bypasses the explicit operator gate;
- dependency vulnerabilities with a demonstrable effect on this project; and
- accidental inclusion of employer, client, subscription, confidential,
  personal, or private-infrastructure data.

Ordinary model hallucinations, scenario disagreements, and uncalibrated scores
are generally quality issues, not security vulnerabilities, unless they enable
a concrete security or data-exposure impact.

## Credential or data incident

If a secret or sensitive artifact is committed:

1. Treat it as compromised; do not rely on deleting the current file.
2. Revoke or rotate credentials immediately.
3. Restrict access to affected systems and preserve appropriate audit evidence.
4. Determine whether logs, releases, caches, forks, packages, or Git history
   contain the material.
5. Remove or rewrite history only through a coordinated incident response.
6. Notify affected parties and platform support when required.
7. Document the root cause and add a preventive release check.

For provenance disputes or non-sensitive citation corrections, use an ordinary
issue and follow [DATA_PROVENANCE.md](DATA_PROVENANCE.md).

## Deployment responsibility

Atlas Workbench is a research prototype. Operators are responsible for
authentication, transport security, endpoint exposure, model and dependency
patching, backups, least privilege, secrets management, source authorization,
and monitoring in their environment.

The bundled dashboard intentionally accepts only loopback bind addresses. Its
APIs also require a loopback peer and Host header; state-changing requests
require JSON and reject cross-origin browser requests. It is not represented as
a hardened shared service. Do not work around these controls to expose it to an
untrusted or multi-user network; build a separately authenticated, authorized,
TLS-protected deployment if remote access is required.

Optional DriftLab traces and `PipelineResult.state` can contain research inputs,
model outputs, local paths, and endpoint details. They are runtime-sensitive
artifacts and must be redacted before sharing.

The optional scout introduces outbound network access only when an operator
explicitly enables it. Keep the source registry reviewed and static; do not
accept arbitrary request URLs from dashboard or model output. Preserve public
HTTP(S) validation, redirect validation, DNS/IP protections, response-size and
item limits, and short timeouts. The default transport connects directly to a
validated public DNS answer, verifies the original TLS hostname, rejects
compressed responses, and runs each fetch in a killable deadline process.
It fails closed when a daemonic or pseudo-file Python entry point cannot
provide that isolation; spawn-based programmatic callers must use the standard
guarded `__main__` entry point.
Scout proposals are untrusted metadata and must not be rendered as HTML,
executed, or promoted directly into evidence.

## Coordinated disclosure

Please allow maintainers a reasonable opportunity to investigate and prepare a
fix before public disclosure. Maintainers will aim to acknowledge the report,
confirm the affected scope, coordinate remediation, and credit the reporter
unless anonymity is requested.
