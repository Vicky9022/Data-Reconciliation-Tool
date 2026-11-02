# Operations guide

For the current tested container release and startup helper, follow [DEPLOYMENT.md](DEPLOYMENT.md). The production image disables Gunicorn's unused control socket to support a read-only filesystem.

**Version 2.1 additions and overriding procedures are in [RELEASE-2.1.md](RELEASE-2.1.md).** This guide describes the base dashboard/coordinator deployment. Optional comparison workers run separately through PostgreSQL and S3; they do not share the SQLite volume.


## Deployment contract

Use one application process, eight request threads, one background reconciliation worker, and a persistent local data volume. Do not use Gunicorn preload, multiple workers, multiple replicas, NFS, or a shared network-mounted SQLite file. An exclusive process lock protects the coordinator. Remote comparison execution now uses the optional PostgreSQL/S3 adapter. High availability for the dashboard metadata and coordinator still requires a separate server-database migration and failover design.

Production now defaults to per-dataset read/write grants; administrators retain workspace access. SSO/MFA, recovery, legal holds and an optional locked archive path are implemented in 2.1. Custom business-rule expressions, scheduled reconciliation, run cancellation and automatic remediation remain outside this release. No external AI service receives your data.

## HTTPS deployment with containers

1. Install Docker Engine and Compose on the deployment host.
2. Point a domain you control to the host; allow inbound ports 80 and 443.
3. Copy `.env.example` to `.env`. Set your domain, first administrator email, a unique 12–200 character administrator password, and a cryptographically random session secret. Generate a secret using `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Store these settings in your organization's secret manager.
4. Run `docker compose up --build -d` from the project directory.
5. Open `https://YOUR_DOMAIN`. Sign in with the bootstrap account. Add analyst/viewer members from Team & access.
6. Run the fixture and environment acceptance tests with a non-confidential dataset before importing business records. Monitor `docker compose logs -f app edge`.

The Compose configuration keeps the application private to the container network. Caddy manages the public HTTPS endpoint. The container runs as UID 10001, drops capabilities, mounts a persistent `/data`, and has a read-only root filesystem. Ensure volume ownership and host permissions allow only approved operators. Encrypt the host volume using your platform's storage controls; the application does not itself encrypt SQLite or CSV files at rest.

The production Dockerfile now pins its Python base image digest and exact Linux dependency versions. Docker build, real staging integration, production container, HTTPS and SMTP checks have passed; see deployment_readiness.json. Run your organization-specific infrastructure and security acceptance before confidential data rollout.

### Direct WSGI deployment

With the environment configured and dependencies installed:

```text
gunicorn --workers 1 --threads 8 --bind 127.0.0.1:8000 --timeout 120 --graceful-timeout 120 'server:create_app()'
```

On Windows use Waitress: `waitress-serve --call --listen=127.0.0.1:8000 --threads=8 server:create_app`. Run either behind a trusted HTTPS reverse proxy, set `RECON_PUBLIC_URL` to its exact HTTPS origin, and enforce request body/header timeouts, connection limits, and login edge rate limits there. The development launcher is for local demonstrations only.

## Configuration

| Variable | Meaning |
|---|---|
| RECON_DATA_DIR | Persistent data directory; defaults to project runtime/ |
| RECON_SECRET | 32+ random characters; keys stored session-token hashes; rotation logs everyone out |
| RECON_ADMIN_EMAIL | First-start bootstrap administrator email |
| RECON_ADMIN_PASSWORD | First-start password; ignored once users exist |
| RECON_PUBLIC_URL | Exact public HTTPS origin used for browser origin checks |
| RECON_CONNECTIONS | JSON describing approved PostgreSQL source/destination pairs |
| LOG_LEVEL | Server log level; default INFO |

Remove bootstrap credentials from the running environment after initialization. Keep the session secret stable across restarts. Session cookies are Secure, HttpOnly and SameSite=Strict in production. Sessions expire after eight hours. Login failures are limited to ten per remote address in 15 minutes. Behind a proxy this address is the proxy, so add a per-client limiter at the edge; forwarded address headers are intentionally not trusted by the application.

Use a fresh, separate data directory for production; never reuse the synthetic demo runtime. Existing users take precedence over bootstrap credentials on restart.

## PostgreSQL connection pairs

Install psycopg through requirements.txt. Credentials stay in server environment variables. The browser only selects an approved alias, never an arbitrary URL, SQL query, schema or table.

```json
{
  "customer-migration": {
    "source": {"dsn_env":"CUSTOMER_SOURCE_DSN","schema":"public","table":"customers"},
    "destination": {"dsn_env":"CUSTOMER_DESTINATION_DSN","schema":"public","table":"customers"}
  }
}
```

Set that JSON as `RECON_CONNECTIONS`; set the two named DSN variables using PostgreSQL URLs with `sslmode=verify-full` and your trusted CA configuration. For Docker, explicitly pass these variables through the app service environment using your secret-delivery system. Use dedicated SELECT-only database roles and network allowlists. The tool executes a quoted table SELECT and catalog metadata query inside read-only, repeatable-read transactions, with a ten-second connection timeout and 60-second statement timeout. Rows are fetched in batches of 1,000, then materialized into bounded CSV snapshots for the queue.

Source and destination are separate snapshots. Coordinate a quiescent cutover window or export consistent snapshots externally if data is changing during ingestion. Live PostgreSQL connectivity and catalog behavior require validation against your server version and permissions.

## Limits and capacity

| Resource | Bound |
|---|---:|
| JSON request | 32 MiB |
| Each UTF-8 snapshot | 1 GiB through chunked upload; 12 MiB inline JSON |
| Columns per dataset | 200 |
| Records per snapshot | 5,000,000 |
| Default Python CSV field-size limit | 131,072 characters |
| Findings per run | 2,000,000 |
| Pending/running comparisons | 10 |
| Concurrent reconciliation workers | 1 locally; remote workers via the distributed overlay |
| Visible run/audit history list | Most recent 200 entries |
| Exception page | 50 findings |
| Duplicate evidence preview | First 20 rows per side/key |
| Minimum free space for submission | 256 MiB |

The engine indexes rows on disk and emits findings to a spool. Browser uploads are chunked, database snapshots are written to disk, and exports stream from temporary files. The inline JSON upload remains buffered within its smaller limit. A 12 MiB file can expand materially during JSON parsing, staging and evidence export. Benchmark your row width and exception density; partition larger datasets by a stable, agreed key range. Monitor disk capacity and process memory. The admission free-space check is not a storage quota or a guarantee against disk exhaustion mid-run.

## Health and logs

`GET /healthz` checks database availability and worker liveness. The service returns a request ID with every response. Logs contain request routes and sanitized processing errors, not input CSV contents or database credentials. A failed run exposes its error and keeps the snapshots for investigation. Queue states persist; after a process restart, interrupted running jobs are requeued and recomputed from their retained inputs. Results become visible only after successful atomic publication of all findings and the summary.

Audit events are append-only to ordinary application SQL operations. Hash-chain verification detects edits when compared to a trusted previous head. A privileged operator who can rewrite the entire database can also rewrite the chain; this is not WORM storage or a cryptographic third-party timestamp. Archive backups and chain heads to a separately controlled system if your governance requires stronger guarantees.

## Backup and restore

With the environment configured:

```text
python manage.py --data-dir /data backup /backups/reconcile-2026-10-28.zip
python manage.py --data-dir /data verify-audit
```

Backup uses SQLite's online backup API, then copies exactly the retained snapshots referenced by that database copy. There is no automatic retention deletion, so the inputs remain available while backup runs. Backups include credentials' password hashes and confidential evidence: encrypt and restrict them. The session secret is not part of the backup and must be recovered separately from your secret manager.

To restore, stop the service, unpack the archive into a new empty data directory, restore correct permissions, point RECON_DATA_DIR to it, restore the session secret, and start exactly one process. Verify the audit chain, run counts and a known historical comparison. Queued/interrupted runs will resume. Rehearse this procedure before production adoption and measure your own recovery objectives.

## Password reset and member access

Admins create members and revoke/restore access in Team & access. Revocation deletes all existing sessions. An authorized host operator can reset a password interactively without putting it in shell history:

```text
python manage.py --data-dir /data reset-password analyst@example.com
```

The utility prompts twice, hashes the new password, revokes sessions, and records an audit event. Version 2.1 includes an encrypted SMTP recovery outbox and an OIDC proxy integration; actual SMTP and identity-provider settings must be supplied by the operator.

## Upgrade and retention

Back up before upgrading. Schema versions 1 and 2 are tracked in the migrations table; future upgrades must add explicit ordered migrations and a rollback/restore plan. Keep the engine version with historic run artifacts. Retention is operator-controlled. The 2.1 utility supports dry-run plans, legal holds, verified encrypted archives and an explicit purge step; master audit retention is separate. Implement a documented retention/archival policy appropriate to your records before long-term use. Do not manually edit original runs, issues, snapshot files or review decisions.
