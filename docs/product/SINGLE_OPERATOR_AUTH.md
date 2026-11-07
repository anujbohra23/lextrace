# Single-operator API prerequisite

This is a local single-operator pilot, not public multi-user authentication.
Use a trusted local machine and loopback listeners. Remote access needs TLS and
separately scoped deployment/security work. There are no accounts, login screens,
roles, password reset, token issuance endpoints or authentication bypasses.

Export `LEXTRACE_OPERATOR_ID` as a stable operator identifier (1–128 ASCII letters,
digits, underscores, dots, @ or hyphens). Export `LEXTRACE_OPERATOR_API_SECRET` as
a freshly generated `secrets.token_urlsafe(32)` value (at least 256 random bits).
The accepted secret format is 43–256 URL-safe ASCII characters; length does not
prove entropy. Configure both before using the API. Python does not load `.env`.
Keep credentials in local secret/environment configuration, never in Git, URLs,
command-line request URLs, generated artifacts or frontend public build variables.

Send `Authorization: Bearer <secret>` on API requests. Missing/invalid credentials
return a generic 401 with a Bearer challenge. Missing/invalid server configuration
returns 503. Errors omit supplied credentials and storage details. Comparison uses
constant-time digest comparison. Only `/health`, `/docs`, `/redoc`,
`/docs/oauth2-redirect` and `/openapi.json` are public; these contain health or static
API schemas, not private records. `/ready`, search, cases, research/runs, monitoring,
and all matter/document/upload/evidence routes require authentication. Valid CORS
preflights can run without credentials and never invoke application handlers.

The middleware stores an immutable `OperatorPrincipal` on the request. Handlers
must use `require_operator(request)` (or its FastAPI dependency) for reviewer
identity; client bodies/headers claiming a reviewer identity have no authority.
Future investigation handlers must also call
`MatterStore.require_investigation_matter(matter_id, thread_id)` before reading,
resuming, recovering, canceling or exposing tokens for a thread. That guard reads
the persisted M7 thread/matter association and checks the matter still exists;
knowing a thread ID does not authorize a different matter context. No investigation
routes are added in this prerequisite.

## Workspace ownership and migration

A new additive `pilot_operator_v1` singleton table binds the entire matter database
and associated pilot workspace to one operator ID. Binding is serialized in an
SQLite immediate transaction. Per-matter ownership columns add no isolation in this
single-operator model: all existing and future matters belong to this binding.
The binding survives deleting every matter. Changing the token while retaining
the operator ID supports credential rotation; changing the ID always denies API
access, even when legacy adoption is enabled. Never share runtime/cache/private
storage between separately configured operator workspaces. CLI and direct Python
storage access remain trusted local administrator interfaces, outside HTTP auth.

For an existing database with matters and no binding, API access fails closed.
After verifying that all legacy content belongs to the configured operator, set
`LEXTRACE_ADOPT_LEGACY_MATTERS=true`, make an authenticated request, then remove
that flag and restart. This explicitly adopts the entire legacy workspace. It does
not transfer an existing binding. No automatic ownership transfer or recovery
endpoint exists. Preserve the matter database when restoring the associated
private files/checkpoints/runtime databases; do not remove the binding to change
identity. Empty databases bind on first authenticated request. Database failures
return a generic 503; ownership/adoption failures return a generic 403.

## Browser and tests

The existing frontend sends a bearer header from the current tab's session storage
key `lextrace.operatorToken`. Provision the generated token through the browser's
session-storage editor on the trusted local frontend origin; do not paste it into
URLs, checked-in code or public environment variables. Provision each new tab as
needed and remove it or close the tab when done. Upload and CSV download use the
same header. The token is accessible to JavaScript in this trusted origin, so this
pilot must not host untrusted scripts. Set `NEXT_PUBLIC_LEXTRACE_API_URL` only to
the trusted API origin. No server proxy that grants anonymous browser access is
introduced. CORS permits Authorization from explicitly configured origins.

Offline API tests use an explicitly synthetic credential, temporary databases and
real token verification. There is no test-only production authentication switch.
