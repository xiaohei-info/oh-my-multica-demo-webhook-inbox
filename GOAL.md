# Webhook Inbox Demo — Delivery Goal

Build and deliver a small, production-constrained webhook inbox that demonstrates how
oh-my-multica turns one human requirement into reviewed, merged, and accepted software.

## Product scope

Implement a Python service using FastAPI and SQLite with these HTTP endpoints:

- `POST /webhooks` accepts a JSON webhook body.
  - Read the event identifier from `X-Event-ID`.
  - Verify `X-Webhook-Signature: sha256=<hex>` using HMAC-SHA256 over the exact raw
    request body and the `WEBHOOK_SECRET` environment variable.
  - Reject a missing or invalid signature with `401` and persist nothing.
  - Persist a valid new event atomically and return `201`.
  - Treat the same event identifier and identical raw body as an idempotent duplicate:
    return the existing event with `200`, mark it as a duplicate, and create no new row.
  - Reject the same event identifier with a different body using `409`.
  - Reject request bodies larger than 1 MiB using `413` and persist nothing.
- `GET /events/{event_id}` returns the stored event or `404`.
- `GET /health` returns service and database health using `200` when ready.

Stored and returned event data must include the event identifier, parsed JSON payload,
and server-generated receipt time. API errors must be stable JSON responses.

## Production constraints

- Use a database uniqueness constraint and transaction-safe logic for deduplication;
  correctness must not depend on an in-memory lock.
- Compare signatures in constant time. Do not log secrets, signatures, or full webhook
  payloads. Fail startup clearly when required configuration is missing.
- Keep the design intentionally small and maintainable. Avoid speculative abstractions,
  background workers, external databases, message brokers, and cloud dependencies.
- Provide automated tests for the main path, authentication failures, idempotency,
  conflicting duplicates, size limits, missing records, and health behavior. Achieve at
  least 90% test coverage.
- Provide linting, a reproducible dependency definition, a non-root Docker image with a
  health check, and GitHub Actions CI covering supported Python versions.
- Write a concise README with architecture, local setup, test commands, Docker usage,
  environment variables, and copyable signed-webhook examples.

## Delivery boundaries

- No frontend, user account system, webhook forwarding, retry queue, admin dashboard,
  Kubernetes manifests, or hosted deployment.
- All implementation work must be delivered through reviewable pull requests with CI.
- Independent reviewers must verify security, correctness, tests, and scope before merge.
- Final acceptance must run against the integrated default branch and leave objective
  evidence that every acceptance condition passed.
