# HTTP API v2.1

See the [deployment guide](deployment.md) for hosting and the [operations guide](operations.md) for configuration. All API responses are JSON unless an export/snapshot is requested. Sign in using the same origin. The server sets an opaque HttpOnly cookie. Include `X-CSRF-Token` from the login response for POST/PATCH requests. CORS is disabled. Error responses contain `error` and `request_id`; responses also include `X-Request-ID`.

| Method / route | Role | Purpose |
|---|---|---|
| GET /healthz | Public | Database and worker readiness |
| GET /api/config | Public | Demo state and default rule specification |
| POST /api/login | Public, limited | `{email,password}`; returns identity, role, csrf |
| POST /api/demo-login | Local demo only | Enter generated-data workspace; absent in production |
| GET /api/me | Viewer+ | Current session identity and CSRF token |
| POST /api/logout | Viewer+ | Revoke current session |
| GET /api/connections | Viewer+ | Configured database aliases, without credentials |
| GET /api/runs | Viewer+ | Latest 200 runs with saved rules and summaries |
| POST /api/runs | Analyst+ | Submit snapshot comparison; returns 202 `{id}` |
| GET /api/runs/{id} | Viewer+ | Run state, saved rules, error or completed summary |
| GET /api/runs/{id}/issues | Viewer+ | Paginated/filterable findings and decision histories |
| GET /api/runs/{id}/export | Viewer+ | Filtered CSV; `format=xlsx` for Excel |
| GET /api/runs/{id}/snapshot?side=source | Admin | Retained original source/destination CSV |
| POST /api/issues/{id}/review | Analyst+ | Append decision and mandatory rationale |
| GET /api/audit | Admin | Latest 200 audit events and chain hashes |
| GET /api/users | Admin | Member list without hashes |
| POST /api/users | Admin | `{email,password,role}`; create a member |
| PATCH /api/users/{id} | Admin | `{active:0|1}`; revoke/restore another member |

### Submit a run

```json
{
  "dataset": "Customer migration",
  "source_csv": "customer_id,balance\nC101,5000\n",
  "destination_csv": "customer_id,balance\nC101,5000.01\n",
  "rules": {
    "version":"2026-10-28.1",
    "keys":["customer_id"],
    "fields":{
      "balance":{"type":"numeric","tolerance":"0.01","severity":"high","nullable":false}
    }
  }
}
```

Use `connection:"approved-alias"` instead of CSV strings for an administrator-configured PostgreSQL pair. Local demo requests can use `demo:true` and optionally `corrected:true`. Production refuses demo generation.

An optional `Idempotency-Key` header (8–100 alphanumeric, underscore or hyphen characters) is scoped to the submitting user. A repeated key returns the existing run identifier. Reuse a key only for retries of the same logical request; choose a new one for changed inputs.

Rules accept `text`, `numeric` and `date`; `trim`, `casefold`, `nullable`, `severity`, and absolute `tolerance`. Tolerances should be supplied as decimal strings for exact policy representation. Date tolerances are in days. Optional `schemas:{source:{column:type},destination:{column:type}}` captures declared CSV schema metadata. Unknown options and rules for nonexistent columns fail validation. Key columns cannot have comparison/normalization rules.

### Findings

Query parameters on issues/export: `q` (literal case-insensitive search in key, column and values), `category`, `severity`, `status`, and `page` (issues only, 50 rows). Categories include `missing_destination`, `unexpected_destination`, `duplicate_source`, `duplicate_destination`, `value_mismatch`, `invalid_value`, `invalid_key`, `missing_column`, `unexpected_column`, and `changed_type`.

Review payload: `{decision:"investigating",note:"Evidence and rationale of 10–2000 characters"}`. Decisions: `open`, `investigating`, `resolved`, `accepted`. Only administrators may accept an exception. A recorded resolution does not verify that source/destination data changed.

### Error behavior

401: no current session; 403: wrong role/CSRF/origin; 400: invalid request/rules; 404: unknown route/resource; 409: unfinished results or duplicate member; 413: size limit; 415: unsupported media type; 429: login/queue limit; 502: configured database read failed; 503: unavailable storage, worker or optional export/driver dependency.

Run ingestion errors discovered by the worker produce a persisted `failed` run with a safe error message. Poll run state until completed/failed. A failed run never exposes partially published issues.


## Version 2.1 endpoints

The same session, role, CSRF and origin requirements apply. Production dataset grants additionally protect run lists, detail, issues, exports, reviews and connection aliases. Read grants do not permit run submission or reviews; workspace roles still apply.

| Method / route | Role | Purpose |
|---|---|---|
| POST /api/sso-login | Trusted identity proxy | Create a session for a pre-provisioned identity; shared proxy secret and forwarded email required |
| GET /api/security | Viewer+ | Authenticator state |
| POST /api/security/enroll | Self, reauthenticated | Current password; returns authenticator secret and URI |
| POST /api/security/confirm | Self | Confirm code; returns ten one-use recovery codes once |
| POST /api/security/password | Self, reauthenticated | Change password and revoke all sessions |
| POST /api/security/disable | Self, password and MFA/recovery code | Remove an old authenticator, revoke sessions, and re-enroll on next sign-in |
| POST /api/password-reset/request | Public, limited | Queue an encrypted recovery email; generic account-eligibility response |
| POST /api/password-reset/complete | Public, limited | One-use token, new password, and enabled MFA/recovery code |
| GET /api/access | Admin | Dataset grants |
| POST /api/access | Admin | dataset, user_id, permission=read/write/revoke |
| POST /api/uploads | Analyst+ | Allocate an upload session |
| POST /api/uploads/{id} | Owner | offset, base64 data and optional per-chunk sha256 |
| GET /api/uploads/{id} | Owner | Saved byte offset, sealing state and whole-file checksum |
| POST /api/uploads/{id}/complete | Owner | Validate UTF-8, verify optional checksum and seal |
| GET /api/operations | Admin | Operational counts, storage, heartbeat and alerts |
| GET /api/retention | Admin | Evidence inventory and legal-hold state |
| POST /api/runs/{id}/hold | Admin | hold boolean and rationale of 10–2000 characters |
| GET /metrics | Bearer token | Prometheus metrics; RECON_METRICS_TOKEN must be configured |

To submit a large comparison, use source_upload and destination_upload identifiers instead of CSV strings. Both must be sealed, unexpired and owned by the submitting user. Input snapshots are copied into the retained run evidence before queueing. Default limits are 1 MiB per chunk, 1 GiB per snapshot, 4 GiB temporary upload storage per user and 24-hour temporary upload expiry. Offset conflicts return 409; callers can inspect the saved offset before retrying.

PostgreSQL requests now queue ingestion instead of buffering the two tables inside the HTTP request. The configured dataset binding must match the submitted dataset for non-admin users. Capture records the actual schemas and snapshots used by the engine.

Mandatory MFA configurations allow an unenrolled authenticated member to access only identity, logout and enrollment endpoints until confirmation. SSO-only mode disables native password login and recovery. Recovery uses a durable encrypted outbox; successful token resets revoke sessions and preserve enrolled MFA.

Archived runs retain their summary/rules and review states, while purged evidence values are available only through the governed archive. The snapshot route returns a conflict rather than inventing missing-record evidence. CSV exports stream from disk. Excel exports split at worksheet limits and explicitly identify cells requiring CSV for their complete text.
