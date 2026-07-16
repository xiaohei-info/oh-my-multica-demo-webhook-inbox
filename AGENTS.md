<!-- OMAC:PROJECT_RULES:START -->
# Project Rules

Durable, repository-wide constraints for the webhook inbox service.

## Service scope

- FastAPI + SQLite only. No background workers, external databases, message brokers,
  or cloud dependencies.
- No frontend, admin dashboard, webhook forwarding, retry queue, Kubernetes manifests,
  or hosted deployment.

## Data ownership and persistence

- `Event` is the only persisted entity. It is append-only; there is no delete or
  update path except incrementing `duplicate_count`.
- `event_id` is the primary key. Uniqueness is enforced by the database, not by
  in-memory locks.
- `body_raw` stores the exact request bytes. Equality comparison for dedup is
  byte-for-byte, not a digest.
- `received_at` is generated server-side once per accepted request and never
  overwritten on duplicate.
- Schema migrations are additive-only: new columns must have defaults. No renames
  or drops.

## Module boundaries

- Dependency direction at runtime: `api` → `service` → `repository`. It is strictly
  downward.
- `service` must not import FastAPI. `api` must not execute SQL directly.
- `WEBHOOK_SECRET` is loaded once by `config` at startup and injected into the
  `service` constructor. `config` may read it once for validation and handoff;
  `service` holds the injected value and is the only module that may retain or use
  it after injection. No other module may access the secret or read it at request
  time.
- `api` is the only module that reads the request body, using a bounded streaming
  reader with a hard 1 MiB limit before any JSON parsing.
- `repository.upsert_event` absorbs PK collisions internally and always returns an
  `EventResult`; it never propagates `IntegrityError` to the caller.

## Security

- Compare signatures with `hmac.compare_digest` (constant-time). Do not roll a
  custom comparison.
- Never log `WEBHOOK_SECRET`, the signature header, or the full webhook payload.
  Log only `event_id`, status code, and timing.
- Reject request bodies larger than 1 MiB with `413` before JSON parsing, via
  `api.read_limited_body`.
- Fail startup with a clear message if `WEBHOOK_SECRET` is missing or empty.
- `/health` must return `503` (not `200`) with stable JSON code `db_unhealthy`
  when the SQLite readiness query fails.

## Error contract

- Every error response uses the stable JSON shape
  `{ "error": "<code>", "message": "<human-readable>" }`.
- Error codes are stable: `missing_signature`, `invalid_signature`,
  `missing_event_id`, `invalid_json`, `body_too_large`, `conflict`, `not_found`,
  `db_unhealthy`.

## Testing

- Automated tests cover: main path, auth failures (missing + invalid signature),
  invalid JSON, empty body, idempotency, conflicting duplicates, size limits,
  missing records, healthy health, and db-unhealthy health (`503`).
- Coverage must be at least 90%.

## Build and delivery

- Provide linting (`ruff check` + `ruff format`), a reproducible dependency
  definition (`requirements.txt` generated via `pip-compile` with pinned hashes),
  a non-root Docker image with a `HEALTHCHECK`, and GitHub Actions CI covering the
  four supported Python versions: `3.10`, `3.11`, `3.12`, `3.13`.
- Provide a README with sections: Architecture, Local setup, Test commands, Docker
  usage, Environment variables, and copyable signed-webhook examples.
- All implementation work is delivered through reviewable pull requests with CI.
- Independent reviewers verify security, correctness, tests, and scope before merge.

## Delivery verification gates

Before acceptance, all of the following must pass on the default branch:

| Gate | Command | Pass criterion |
|---|---|---|
| Tests + coverage | `python -m pytest --cov=src --cov-report=term-missing --cov-fail-under=90 tests/` | exit 0; coverage ≥ 90% |
| Lint | `ruff check src tests && ruff format --check src tests` | exit 0 |
| Type check | `python -m mypy src` | exit 0 |
| Reproducible deps | `pip install --require-hashes -r requirements.txt` from clean venv | pinned versions and hashes; installs cleanly |
| CI matrix | GitHub Actions runs `python-version: ["3.10", "3.11", "3.12", "3.13"]` | all four pass |
| Docker | `docker build -t webhook-inbox .`; `docker run --rm --name webhook-inbox -e WEBHOOK_SECRET=test -d -p 8000:8000 webhook-inbox`; `docker exec webhook-inbox id -u` | build ok; the `id -u` output is a number ≥ 1000 |
| HEALTHCHECK | after startup, `docker inspect --format='{{.State.Health.Status}}' webhook-inbox` | output is `healthy` |
| Missing-secret startup | start with `WEBHOOK_SECRET=` | exits non-zero with `StartupError` |
| README | `README.md` present with all six required sections | all sections + copyable curl |
| CI pipeline | `.github/workflows/ci.yml` runs on push + PR | lint, matrix, docker build jobs |
<!-- OMAC:PROJECT_RULES:END -->
