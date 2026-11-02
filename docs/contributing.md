# Contributing

The repository contains the reconciliation engine, web application, tests and deployment configuration. Keep a change focused on a reproducible problem or a documented behavior, and include the checks needed to review it.

## Set up a local workspace

Use Python 3.12 or later. From the repository root:

```text
python -m venv .venv
```

Activate the environment with `.venv\Scripts\Activate.ps1` on Windows or `source .venv/bin/activate` on Linux/macOS, then install the package:

```text
python -m pip install -e . --no-deps
python -m reconcile
```

Open `http://127.0.0.1:8765` and select **Explore synthetic workspace**. This launcher binds to localhost and enables generated-data sign-in. Never expose it as a production service.

The local CSV demo uses the standard library. Install the optional database, Excel, cryptography and production-server dependencies when working on those paths:

```text
python -m pip install -r requirements.txt
```

The Linux container uses the exact versions in `requirements.lock.txt`. Dependency upgrades need their own build, tests and security checks.

## Choose the right directory

| Change | Directory |
|---|---|
| Matching rules, API, workers, access control or governance | `src/reconcile/` |
| Browser interface | `src/reconcile/static/` |
| Regression or integration coverage | `tests/` |
| Measured fixture or HTTP exercise | `benchmarks/` |
| Deployment helper or service checks | `scripts/` |
| Container overlays, Caddy or staging fixtures | `deploy/` |
| Guides and release evidence | `docs/` |

Use a `codex/` branch for a change and open a pull request against `main`. Never force-push over existing work. Describe the problem, the resulting behavior and the checks you ran. Explain any changed rule semantics or operating limits so a reviewer can assess existing results.

## Run the checks

```text
python -m unittest discover -s tests -t . -v
python -m benchmarks.benchmark
python scripts/verify.py
```

The standard-library test suite needs no pytest installation. Live integration tests are skipped until explicitly enabled against the disposable staging stack. Excel and encrypted-archive tests need the relevant optional dependencies. Generated reports go under `reports/`, which is ignored by Git. Keep the recorded results in `docs/validation/2026-10-29/` as historical evidence; do not overwrite them with a new run.

For database and worker changes, start the staging stack:

```text
docker compose -f deploy/staging.yaml up --build -d
python scripts/checks/wait_ready.py
```

Then enable the live integration tests in that shell. On Linux/macOS:

```sh
RECON_RUN_INTEGRATION=1 python -m unittest discover -s tests -t . -p test_integration.py -v
```

On PowerShell:

```powershell
$env:RECON_RUN_INTEGRATION = '1'
python -m unittest discover -s tests -t . -p test_integration.py -v
Remove-Item Env:RECON_RUN_INTEGRATION
```

The staging profile publishes only loopback ports and uses public synthetic fixture credentials. Never aim these acceptance tests at production or import confidential data into the lab. See the [deployment guide](deployment.md) for the local endpoints and the [release history](releases/2.1.md) for fixture expectations.

The workflow in [`.github/workflows/ci.yaml`](../.github/workflows/ci.yaml) runs regression tests, the measured fixture, production-container and HTTPS checks, live PostgreSQL/worker integration, and dependency/static analysis. It requires no production credentials. Check its result for the commit being reviewed; a local pass does not establish a hosted CI pass.

## Keep evidence and credentials out of commits

Use synthetic data in fixtures and screenshots. Never commit `.env`, the `runtime/` database or uploads, private keys, backups, governed archives, generated reports or Docker image bundles. Keep `.env.example` limited to placeholders. Source publication and application deployment are separate steps; production credentials belong on the selected host or in its secret manager.

Preserve snapshot immutability and explicit comparison rules. A marked resolution must not rewrite historical evidence, and an application change must not write back to a source or destination database. For deployment, backup and restore procedures, use the [deployment](deployment.md) and [operations](operations.md) guides. The [API reference](api.md) documents roles, requests and findings.
