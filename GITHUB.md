# Publish the deployment source to GitHub

Repository: https://github.com/Vicky9022/Data-Reconciliation-Tool

Use this directory as the repository root. Keep the application source, tests,
deployment configuration and recorded verification evidence together. The
requested repository visibility is **public**.

The source repository excludes `.env`, the `runtime/` database and uploads,
private keys, backups, archives and generated Docker image bundles. Staging
credentials in `deploy/staging.yaml` are public synthetic fixtures; never use
them for production. Enter real deployment credentials privately on the host
using `python3 deploy_release.py configure`.

## Publish a prepared local repository

After signing into GitHub CLI with an account authorized to create repositories:

```text
gh auth login
gh repo create Data-Reconciliation-Tool --public --source . --remote origin --push
```

Use the create command only when the repository does not already exist.
The prepared local release branch is `codex/deployment-release`. The public
repository uses `main` as its default. If publishing to an existing
repository, inspect its existing history and default branch before adding its
remote. Never force-push over existing work.

The workflow in `.github/workflows/ci.yaml` runs regression tests, builds and
checks the production image and HTTPS proxy, exercises live PostgreSQL and
remote workers against staging, and audits dependencies/static analysis.
It requires no production credentials. Check the actual GitHub Actions results
after the push; local verification does not establish that hosted CI passed.

## Deploy after publication

Follow [DEPLOYMENT.md](DEPLOYMENT.md) on the selected Linux x86-64 host. Pushing
source to GitHub does not publish the application. The baseline deployment
uses one coordinator, a persistent local volume and Caddy HTTPS. A real host,
domain/DNS and privately configured administrator credentials are required.

Treat the downloadable prebuilt images as a separate release asset rather
than Git source. Their manifest and SHA-256 are in `deployment_images.json`.
Keep deployment keys in your secret manager and configure off-host backups.
