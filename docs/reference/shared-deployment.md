# Single-tenant shared deployment

Atlas Workbench supports a shared workspace behind an **authenticating TLS
reverse proxy with a server-side signing adapter**. The backend continues to
bind only to loopback. Default `local` mode is for one trusted local operator;
do not expose that mode through a network proxy.

This release supplies request verification, role enforcement and an adapter
building block. It does not install an identity provider, implement an OIDC or
SAML login, isolate tenants, or verify that the deployment's external identity
configuration is correct. All authorized readers share one organization's
data, including its private context. Use separate deployments and stores for
separate organizations.

## Deployment contract

1. Terminate HTTPS at the authenticating proxy. It must validate the user's
   IdP session and map an immutable subject plus allowed groups to Atlas roles.
   For OIDC, use the validated issuer and subject as the stable identity; for
   SAML, use the validated persistent identity and issuer. Do not derive a role
   from query parameters, form fields or an unverified forwarding header.
2. Configure the backend and server-side adapter with one identical, randomly
   generated signing secret containing at least 32 bytes. Keep it in their
   private environments or secret manager. Never embed it in HTML, JavaScript,
   source control, URLs, browser storage or client-visible configuration.
3. Set `ATLAS_ACCESS_MODE=proxy`, `ATLAS_PUBLIC_ORIGIN=https://atlas.example.org`
   and `ATLAS_PROXY_HMAC_SECRET` on the backend. Set `ATLAS_PROXY_NONCE_DB` to a
   private writable SQLite path when the default scenario-store sibling path
   `generated/security/proxy_nonces.sqlite3` is unsuitable. Use a durable local
   filesystem with reliable SQLite locking. Configuration failure denies
   access; it does not fall back to local mode.
4. Strip all incoming Atlas identity/signature headers, authenticate the user,
   preserve the exact HTTP method, path including untouched query string,
   body bytes and **original browser Origin**, then create fresh signed headers
   for the loopback request. Buffering limits at the proxy must be no larger
   than the backend's endpoint limits. Never rewrite a foreign or missing
   Origin into the configured public origin. Disable caching of all API
   responses and do not retry a signed mutation automatically.
5. Restrict direct backend access to the same protected host. Loopback source
   and Host validation remain required; they alone never authenticate an API
   request in proxy mode. A valid signature is also required. Prevent other
   local accounts from reading stores, the nonce database or the signing key.
6. Synchronize the proxy/backend clocks. Rotate the shared signing secret on
   both sides together and validate a fresh request before serving traffic.
   During a key cutover, already signed in-flight requests may be rejected.

The HTML/CSS/JavaScript shell contains no private records and may load without
an identity. Every `/api/` request requires authorization, including health,
data and access-identity reads. The proxy may require login for the shell too.
The application does not add CORS permission for arbitrary browser origins.

## Permission table

Roles are distinct, not cumulative. `admin` can use all recognized endpoints.
Unknown roles, capabilities, methods and API paths are denied, even for admin.
The only additional capability is `export`.

| Action | Viewer | Analyst | Reviewer | Admin |
| --- | --- | --- | --- | --- |
| Read research, scenarios, reviews, private context, jobs, diagnostics and integration summary | Yes | Yes | Yes | Yes |
| Read intake sources and model availability | No | Yes | No | Yes |
| Prepare/submit research or imports; run analysis; maintain private context; cancel/retry queued work | No | Yes | No | Yes |
| Approve/reject review records, batch decisions, rate advisory memos | No | No | Yes | Yes |
| Read settings/readiness; change models/framework settings; enable private context | No | No | No | Yes |
| Preview/download exports or integration packages | With `export` | With `export` | With `export` | Yes |
| Record integration acknowledgments or create retry packages | No | With `export` | No | Yes |

The code's explicit path table in `web/access.py` is authoritative. Permission
for a path does not create that endpoint; backups and restore remain operator
CLI actions unless separately wired into a deployment. GUI availability follows
the verified role, and server checks remain authoritative.

Proxy mode replaces supplied `actor` and `reviewer` values with the verified
subject before mutation dispatch. Export preview ownership is bound to that
subject. A user's guessed preview token does not grant access to another
user's frozen snapshot. Role separation is **not** a four-eyes rule: this
release does not prohibit an admin from both preparing and deciding a record,
or enforce separation of duties across a person's multiple IdP assignments.

## Signature protocol v1

Headers are `X-Atlas-Subject`, `X-Atlas-Role`, `X-Atlas-Capabilities`,
`X-Atlas-Timestamp`, `X-Atlas-Nonce`, `X-Atlas-Body-SHA256` and
`X-Atlas-Signature`. Capabilities are sorted, unique and comma-separated; empty
means none. Duplicate identity headers are rejected. Subject length is 1–160
characters with no leading/trailing whitespace or control characters.

The signed message is the UTF-8 encoding of a compact JSON array, using
`ensure_ascii=False` and no spaces between elements:

```python
[
    "atlas-proxy-v1", canonical_https_origin, subject, role,
    sorted_capabilities, uppercase_method, exact_path_and_query,
    unix_timestamp_as_string, nonce, sha256_of_exact_raw_body_hex,
]
```

The signature header is `v1=` followed by lowercase HMAC-SHA256 hexadecimal
using the shared secret. SHA-256 of the empty byte string is used for GETs.
POST JSON is signed before parsing; whitespace or encoding changes invalidate
the signature. Method, query, body, origin, roles and subject are all bound.

The backend accepts assertions at most 60 seconds old and at most 5 seconds
ahead. Nonces must be 22–128 base64url characters and must come from a secure
random generator; the supplied helper generates 24 random bytes. The backend
atomically consumes each nonce in a durable SQLite table before dispatch.
Replays are rejected across requests, threads, workers and backend restarts.
An unreadable, unwritable or full nonce store fails closed. Only hashes of
nonces and their expiry are persisted, not user claims or signing secrets.

One-use assertions do not make business operations exactly once. If a response
is lost, inspect the resulting job, decision or package before submitting a
newly signed retry. Review revisions and frozen package identifiers provide
additional application-level conflict boundaries.

## Server-side adapter example

The proxy's authenticated server code can call the tested adapter helper:

```python
import os
from workbench_atlas.web.access import proxy_adapter_headers

# These identity values MUST come from the proxy's validated IdP session.
# request_target and raw_body are the exact bytes/target forwarded downstream.
headers = proxy_adapter_headers(
    incoming_headers,
    secret=os.environ["ATLAS_PROXY_HMAC_SECRET"],
    public_origin=os.environ["ATLAS_PUBLIC_ORIGIN"],
    subject=validated_session_subject,
    role=mapped_atlas_role,
    capabilities=mapped_atlas_capabilities,
    method=request_method,
    target=request_target,
    body=raw_body,
    backend_authority="127.0.0.1:8080",
)
# Forward raw_body unchanged to the fixed loopback backend with these headers.
# Do not follow redirects or accept a client-supplied backend destination.
```

This helper drops client identity, authorization, cookie and forwarding
headers; only Accept, Content-Type and the original Origin are retained before
the new authenticated headers are added. Its arguments are a privileged
server-side interface, not an endpoint the browser may call. The surrounding
proxy adapter must validate its session, reject ambiguous headers and avoid
request-body or target normalization after signing.

## Operational checks

Before admitting users, test an unsigned API read (denied), a permitted signed
read, a role-forbidden mutation (denied), a replay (denied), a foreign-Origin
mutation (denied) and a decision whose recorded actor matches the verified
subject. Verify that credentials and signed headers are excluded from proxy
logs. Rate-limit login and API traffic at the proxy and apply TLS, session
expiry and account revocation policies there. Atlas accepts an already signed
assertion only within its short validity window; it does not query an IdP for
revocation on every request.

The [operations guide](operations.md) covers private snapshots and recovery.
Signing credentials and nonce protection state are excluded from backups.
After recovery, rotate signing material and expire outstanding assertions
before cutover; never reset a live nonce database under an unchanged key.
