# Validation records

The files in `2026-10-29/` preserve the original 2.1 release's measured results.
Each record contains its own test date, scope and limitations. They were moved
without rewriting the measurements; source paths inside older reports refer
to the repository layout used for that run.

Current checks write to the ignored `reports/` directory. GitHub Actions uploads
those reports as workflow artifacts. To reproduce the checks, follow the
[contributor guide](../contributing.md) and the
[deployment guide](../deployment.md).

Passing checks establish the behavior of the tested inputs and environment.
Production DNS, hosting, identity-provider enrollment, backup recovery and
organization-specific access policies require validation on the selected host.
