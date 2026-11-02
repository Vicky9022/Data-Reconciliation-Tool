# Reconcile 2.1 · Deployment release

**Deployment is now verified. Start with [DEPLOYMENT.md](DEPLOYMENT.md).** The Docker build, production-image login/MFA/export/restart checks, trusted HTTPS proxy, live PostgreSQL/remote-worker integration, and local SMTP recovery checks have passed. Exact Linux dependencies and the Python base image are pinned. The downloadable image archive contains the application and edge images.

Read [RELEASE-2.1.md](RELEASE-2.1.md) for the implementation history. Earlier Docker/integration blockers described there are superseded by the deployment evidence in `deployment_readiness.json`.

For public source publication, see [GITHUB.md](GITHUB.md). Production configuration and runtime data are excluded from version control.

Source repository: [Vicky9022/Data-Reconciliation-Tool](https://github.com/Vicky9022/Data-Reconciliation-Tool).


A local-first reconciliation application for an analyst team. It compares retained source and destination snapshots, isolates ambiguous keys, records explicit rules, and supports an evidence-led investigation workflow.

This replaces the earlier Streamlit demonstration with an authenticated web application and a disk-backed engine. It is packaged for **one dashboard/coordinator process on a persistent local volume, with optional PostgreSQL/S3 remote workers**, behind HTTPS. The dashboard itself remains single-node; the new remote comparison workers do not establish dashboard high availability or certify suitability for your infrastructure. Live PostgreSQL, Docker deployment, disaster recovery on your hosting provider, and organization-specific access policies require environment validation before business data goes live.

## Open the application

Requires Python 3.12 or later. The CSV engine, web API, authentication, persistent queue, audit records and browser UI use the Python standard library. The local CSV demo needs no dependency installation.

```powershell
python run.py
```

Open **http://127.0.0.1:8765** and choose **Explore synthetic workspace**. The first visit creates a measured comparison of 10,000 generated customers. On Windows, `start-demo.ps1` can also find the bundled Codex Python runtime when Python is not on PATH. The launcher binds only to localhost and explicitly enables synthetic demo sign-in. Never expose this launcher to a network.

To enable Excel downloads and the optional PostgreSQL driver:

```powershell
python -m pip install -r requirements.txt
```

## What is included

| Area | Implemented behavior |
|---|---|
| Analyst workspace | Responsive dashboard; measured counts; exception distribution; source/destination comparison |
| Dataset ingestion | Strict UTF-8 CSV; administrator-configured PostgreSQL table pairs |
| Business matching | Exact stable keys; composite keys; row-order independence; empty-key exceptions |
| Discrepancies | Source-only / destination-only keys; duplicate groups; value mismatches; invalid values; missing and unexpected columns; changed declared types |
| Rules | Versioned JSON, exact text defaults, explicit trimming/case normalization, Decimal tolerances, ISO date tolerances, nullable fields and severity |
| Investigation | Search and category/severity/status filters; 50-row server pagination; side-by-side evidence; decision history |
| Reviews | Analyst investigation and reported corrections; administrator exception acceptance; mandatory rationale |
| Run history | Dataset and local-date filters; recent measured trends; retained immutable rule and input fingerprints |
| Export | Filtered CSV with spreadsheet-formula neutralization; Excel with literal string cells |
| Access | Scrypt password hashes, opaque sessions stored as keyed hashes, CSRF verification, eight-hour expiry, viewer/analyst/admin roles, access revocation |
| Execution | Persistent states; one local worker or remote PostgreSQL/S3 workers; bounded queue; interrupted-run recovery; failed runs hide partial results |
| Audit | Append-only SQL triggers; serialized SHA-256 hash chain; administrator activity view; offline integrity verification |
| Operations | Container configuration, HTTPS reverse proxy, health endpoint, request IDs, logs, live SQLite backups, password reset and session revocation |

## Verify

```powershell
python -m unittest discover -s tests -v
python benchmark.py
```

`tests/` uses the standard-library test runner; no pytest installation is needed. It checks the independently defined injection oracle, decimal precision, duplicate overlap, composite keys, schema drift, malformed input, CSRF/origin checks, RBAC, review evidence, audit integrity, formula-safe exports, idempotent submissions, rate limiting, production cookie flags, coordinator locking and restart recovery.

Current verification: 52 local tests passed, and all 3 live integration cases passed separately in staging. Production container, HTTPS and local SMTP smoke checks also passed. The final verification record is in `verification.json`. Browser checks also confirmed persisted review notes, the corrected fixture improving from 150 findings to zero, and a mobile layout without document overflow.

### Actual benchmark

The measured fixture was run on October 28, 2026 using Python 3.12.14 on Windows. The exact output is in `measured_results.json`.

| Metric | Measured |
|---|---:|
| Source / destination records | 10,000 / 9,995 |
| Matching unique pairs | 9,875 |
| Known exception events / detected exact events | 150 / 150 |
| Detection recall | 100% on this fixture |
| False-positive rate | 0% on this fixture |
| Exact category/key/field classification | 100% on this fixture |
| Runtime with tracemalloc | 7.697 seconds |
| Peak traced Python allocations | 1,492,175 bytes |
| Repeated summary identical | Yes |

The benchmark excludes generation, upload decoding, API persistence and export. Traced memory excludes native SQLite allocations and is not process RSS. These measurements are evidence for this fixture, not a general accuracy or throughput guarantee. Runs in the UI record their own elapsed processing time without tracemalloc.

## Comparison contract

- Key values match exactly, including whitespace. Use a transaction identifier or composite key to distinguish legitimate repeated transactions.
- Duplicate groups are never paired arbitrarily. Group evidence shows the first 20 rows; complete input snapshots are retained. Duplicate groups can also produce missing/unexpected findings.
- Exceptions count events. A record may have several findings. `affected_keys` counts distinct valid business keys with record-level findings; invalid keys and schema findings are reported separately.
- Matching records count unique source/destination pairs whose common non-key fields pass the configured rules. A dataset may have matching pairs and still require review because of schema differences.
- Blank fields are null-like by default. `nullable:false` rejects them. CSV cannot distinguish a database NULL from an empty string; use an explicit agreed source encoding before export if that distinction matters.
- Text is exact unless an approved rule explicitly enables normalization. Numeric comparison uses Decimal with support for up to 1,000 digits and exponent magnitude 1,000, avoiding binary floating-point rounding. Date tolerances are days; numeric tolerances are absolute. Dates must be ISO-compatible. Mixing timezone-aware and naive timestamps is invalid.
- CSV has no declared database types. Optional `schemas.source` and `schemas.destination` maps in the rule JSON allow declared-type comparison. PostgreSQL ingestion captures catalog type strings automatically. Any changed declaration requires review; automatic type compatibility approval is deliberately not inferred.
- Field rules validate values only for unambiguous paired records. Missing and duplicate groups retain their evidence and are investigated under their own categories.
- The fingerprint includes exact snapshot bytes, full rules, schema declarations and engine version. Changing row order preserves comparison behavior but changes the byte-level fingerprint.
- A review decision never edits snapshots or rewrites a run result. A “resolved” status records a reported correction. Create a new comparison to verify the correction.

## Production deployment

The 2.1 release adds mandatory native MFA by default in the production Compose profile, default-deny dataset grants, an OIDC proxy overlay, PostgreSQL/S3 workers, and an instrumented staging stack. See RELEASE-2.1.md before following the base single-node deployment instructions.


See **OPERATIONS.md** for deployment, connection configuration, secrets, backup/restore, integrity checks and operating limits. **API.md** describes the authenticated interface.

The supplied Docker Compose deployment starts one non-root application process and a Caddy HTTPS proxy. The application refuses production startup without a 32+ character session secret and first-start administrator credentials. The production factory never enables synthetic sign-in.

## Project map

```text
engine.py          Disk-backed matching, rules, evidence and fingerprints
server.py          WSGI API, access control, queue, persistence and audit
synthetic.py       Synthetic inputs and independent injection oracle
static/            Browser UI (no build pipeline or external assets)
tests/             Engine and service regression tests
benchmark.py       Reproducible measured fixture
manage.py          Operator backup, integrity and credential utilities
run.py             Localhost synthetic demo launcher
Dockerfile         Non-root production container
compose.yaml       Single-node application and HTTPS proxy
```
