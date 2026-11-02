# Deployment release · October 29, 2026

The application is packaged and tested for a **single Linux x86-64 dashboard/coordinator**, with a persistent data volume and optional remote comparison workers. This is a deployable software release; it does not claim multi-node dashboard failover, an organizational SLA, or independent security certification.

## What has actually passed

- 52 local regression/configuration tests and all 3 live integration tests, run separately.
- A real PostgreSQL 17 source role rejected writes; database ingestion and the PostgreSQL/S3 worker pipeline detected the fixture's 125 known findings.
- The production Gunicorn image ran as a non-root user with a read-only root filesystem. Demo login was disabled; native login, mandatory MFA, CSV/Excel export and retained runs survived a container restart.
- An actual Caddy HTTPS proxy reached the production application with certificate-chain and hostname verification using an isolated test CA. No global trust setting or browser warning was bypassed.
- SMTP recovery delivered to the local Mailpit sink, a one-use recovery link worked, and the disposable test member was revoked afterward. No external email was sent.
- The pinned Python dependency audit reported no known vulnerabilities. Medium/high static checks passed after three parameterized SQL false positives were reviewed and documented. These suppressions are narrow B608 annotations, backed by a negative injection regression test. This is not a replacement for an independent penetration review or a full operating-system image scan.

The [recorded validation results](validation/2026-10-29/) include regression, integration, production-container, HTTPS, local SMTP and security checks. New checks write to the ignored `reports/` directory.

## Deploy the supplied prebuilt images

Use a server with Docker Engine, Docker Compose and Python 3.12+. Point your domain's DNS records to that server and allow TCP ports 80 and 443. Use a persistent local volume and appropriate host storage encryption/access controls.

Extract the source project. Place `Reconcile-Deployment-Images.tar.gz` beside the extracted `reconciliation-platform` directory. Run these commands from the repository root:

```text
docker load -i ../Reconcile-Deployment-Images.tar.gz
python3 scripts/deploy.py configure
python3 scripts/deploy.py check
python3 scripts/deploy.py start --prebuilt
```

Configure prompts for your domain, administrator email and password. It generates separate random session, MFA and archive keys in a protected `.env`, without printing credentials. Keep an escrowed copy in your secret manager; the image archive and source ZIP deliberately contain no deployment credentials or business data.

Start validates the local image against the release manifest, waits for container readiness, and then checks the public HTTPS health endpoint with certificate verification. It reports the URL only after that succeeds. Actual public certificate issuance requires your real domain and reachable ports; the local certificate test does not substitute for ownership/DNS configuration.

Sign in at the reported URL. The initial administrator must enroll an authenticator before accessing datasets. Save the one-use recovery codes securely. Create team members and grant dataset permissions before importing business data.

## Build from source instead

```text
python3 scripts/deploy.py configure
python3 scripts/deploy.py build
python3 scripts/deploy.py start
```

The Dockerfile pins its Python base image digest and installs exact Linux package versions from `requirements.lock.txt`. The prebuilt archive includes both `reconcile-platform:2.1` and the Caddy edge image. [`deploy/images.json`](../deploy/images.json) records their identities and the archive SHA-256. Rebuild and retest deliberately when upgrading dependencies.

## Optional integrations

- **PostgreSQL inputs:** configure approved connection aliases and SELECT-only identities with verified TLS. Bind each analyst-accessible alias to an approved dataset.
- **Remote workers:** use `deploy/distributed.yaml`, a migrated PostgreSQL job database, and a private encrypted artifact bucket. Provide workload IAM permissions; remote workers do not mount the dashboard database.
- **Organization SSO:** use `deploy/sso.yaml` with your issuer/client/callback settings. Pre-provision authorized members and apply MFA at your identity provider.
- **Recovery mail:** configure your approved SMTP service. The local Mailpit test verifies application behavior, not your organization's mail delivery.
- **Monitoring:** use `monitoring/compose.yaml` and configure the protected metrics token plus an approved Alertmanager recipient. No external recipient is invented by this release.

The helper covers the baseline native-login deployment. Optional overlays use their documented Docker Compose commands and require an environment acceptance run with your actual services. See the [2.1 release history](releases/2.1.md) and [operations guide](operations.md) for configuration, backup, legal holds and governed archives.

## Staging

```text
docker compose -f deploy/staging.yaml up --build -d
```

The staging app is at `http://127.0.0.1:8766`; choose the synthetic workspace. PostgreSQL and two remote workers are real services. S3 uses the Moto test service, not a production object store. Mailpit captures mail locally at `http://127.0.0.1:8025`. All published staging ports bind to loopback. Staging uses public fixture credentials and must never be exposed as production.

## Operations and rollout

Keep the dashboard/coordinator at one process; do not scale that container or share its SQLite file over NFS. Scale comparison workers through the external broker adapter. Back up the data volume and escrow the encryption keys before upgrades. Governed archives require their own storage and key recovery.

Before importing your confidential records, validate a representative dataset, test restore on your selected host, configure retention/alerts, and apply your organization's security approval. Public DNS, production identity-provider enrollment, actual cloud IAM, and company-specific availability targets remain operator inputs rather than fabricated test results.
