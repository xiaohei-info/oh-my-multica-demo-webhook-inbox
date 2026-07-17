# Webhook Inbox

[![CI](https://github.com/xiaohei-info/oh-my-multica-demo-webhook-inbox/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaohei-info/oh-my-multica-demo-webhook-inbox/actions/workflows/ci.yml)
[![Delivered by oh-my-multica](https://img.shields.io/badge/delivered%20by-oh--my--multica-6f42c1)](https://github.com/xiaohei-info/oh-my-multica)

[English](README.md) | [简体中文](README.zh-CN.md)

This repository is the finished result of one
[oh-my-multica](https://github.com/xiaohei-info/oh-my-multica) delivery: a Webhook Inbox built through Multica work
items and Coding Agent runtimes, reviewed in five public Pull Requests, and accepted against 11 service-level flows.

<p align="center">
  <img src="docs/assets/oh-my-multica-webhook-inbox-demo.svg" alt="Real Webhook Inbox delivery: a five-node DAG, signed event ingestion, idempotent retry, and final acceptance" width="100%">
</p>

## Requirement

The brief was to build a small service that receives signed webhook events from third-party systems. It had to
verify the HMAC-SHA256 signature against the exact request body before parsing JSON, store valid events in SQLite,
and remain correct when senders retry an event or deliver it concurrently.

The same event ID and body could never create a second record. Reusing an ID with different content had to be
rejected without changing the original event. The service also needed event lookup, database health checks, a
1 MiB body limit, stable JSON errors, safe logging, reproducible dependencies, CI, and a non-root container.

The full input is checked in as [`GOAL.md`](GOAL.md).

## How the Agents worked together

```mermaid
flowchart LR
    R[One delivery goal] --> P[Design, acceptance, project rules]
    P --> D[Agent-authored 5-node DAG]
    D --> F[Shared foundation]
    F --> A[HTTP API]
    F --> S[Service and SQLite dedup]
    A --> X[Delivery assets]
    S --> X
    X --> I[Integration acceptance]
    I --> Q[Independent review and CI]
    Q --> M[Merge]
    M --> Z[Final acceptance: 11/11]
```

Planning started with the repository and [`GOAL.md`](GOAL.md). Planner and Orchestrator Agents turned that input into
acceptance criteria and a five-node DAG: one shared foundation, two parallel implementation tracks, delivery assets,
and final integration. Worker Agents owned individual nodes instead of the whole project. Reviewer Agents reran each
node's checks before merge, and the Acceptor Agent tested the integrated `main` branch through the public HTTP API.

The deterministic Loop kept the graph moving. It calculated which nodes were ready, required evidence before a node
could advance, enforced merge conditions, and stopped only after final acceptance passed.

Planning, orchestration, and acceptance used `codex-ubuntu`. Three lower-cost `newapi` runtimes handled most of the
implementation work, while separate Reviewer runtimes checked their output independently.

| Node | Responsibility | Public delivery |
| --- | --- | --- |
| Shared foundation | Domain types, configuration, errors, quality baseline | [PR #2](https://github.com/xiaohei-info/oh-my-multica-demo-webhook-inbox/pull/2) |
| HTTP API | Bounded body reads, headers, stable HTTP errors, health endpoint | [PR #3](https://github.com/xiaohei-info/oh-my-multica-demo-webhook-inbox/pull/3) |
| Persistence and dedup | Verify-before-parse service flow and transaction-safe SQLite deduplication | [PR #4](https://github.com/xiaohei-info/oh-my-multica-demo-webhook-inbox/pull/4) |
| Delivery assets | Hashed dependencies, CI matrix, Docker image, operator docs | [PR #5](https://github.com/xiaohei-info/oh-my-multica-demo-webhook-inbox/pull/5) |
| Integration acceptance | Full-path acceptance harness and integrated service verification | [PR #6](https://github.com/xiaohei-info/oh-my-multica-demo-webhook-inbox/pull/6) |

## Delivered behavior

| Scenario | Service result |
| --- | --- |
| A new event has a valid ID, signature, and JSON body | Store it atomically and return `201` |
| The same ID and exact body are delivered again | Return `200` with `"duplicate": true`; keep one database row |
| The same ID is reused with different content | Return `409`; keep the original event unchanged |
| The signature is missing or invalid | Return `401`; persist nothing |
| The request body is larger than 1 MiB | Return `413`; persist nothing |
| A caller requests a stored or unknown event | Return the parsed event with `200`, or `404` |
| A caller checks service and database health | Return `200` when healthy, or `503` when the database is unavailable |

The delivered service uses FastAPI and SQLite, with transaction-safe deduplication for both sequential retries and
concurrent delivery. It runs as UID 1001 in Docker, stores its database under `/data`, and includes a container
healthcheck.

## Delivery evidence

| Evidence | Result |
| --- | --- |
| Delivery DAG | 5/5 nodes converged to `done` |
| Pull requests | 5 reviewed PRs merged |
| Test suite | 86 tests passed |
| Coverage | 97.18%, above the 90% gate |
| CI | Python 3.10, 3.11, 3.12, and 3.13 passed |
| Container delivery | Non-root image, healthcheck, signed-webhook smoke test passed |
| Final acceptance | 11/11 flows passed on the integrated `main` branch |
| Controller result | exit 0 |

These results are checked in as the [manifest DAG](.omac/webhook-inbox.yaml),
[acceptance document](.omac/webhook-inbox.acceptance.yaml), and [delivery goal](GOAL.md).

## Reproduce the evidence

To rerun the same checks, you need Python 3.10+, OpenSSL, and Docker for the container checks.

### Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
```

### Tests

```bash
bash tests/acceptance.sh
bash tests/verify_delivery.sh
```

`tests/acceptance.sh` starts `compose:app` in isolated temporary environments
and runs all 11 approved flows, including concurrent same-ID delivery and
persistence across restart. Each flow uses bounded startup checks and cleans up
its processes and temporary files.

The normal quality gates are also available:

```bash
.venv/bin/python -m pytest --cov=src --cov-report=term-missing --cov-fail-under=90 tests/
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/python -m mypy src
```

## Service

### Architecture

```text
HTTP request
    │
    ▼
FastAPI boundary (src/api.py)
    │  bounded raw-body read, headers, stable error mapping
    ▼
Service (src/service.py)
    │  constant-time HMAC, verify before JSON parse
    ▼
Repository (src/repository.py)
       SQLite primary-key dedup, exact-byte comparison, WAL
```

[`compose.py`](compose.py) wires the layers together. The API layer handles
HTTP concerns, the service controls authentication and parsing order, and the
repository owns the deduplication transaction.

### Endpoints

| Method | Path | Success | Main failures |
| --- | --- | --- | --- |
| `POST` | `/webhooks` | `201` new / `200` duplicate | `400`, `401`, `409`, `413` |
| `GET` | `/events/{event_id}` | `200` | `404` |
| `GET` | `/health` | `200` | `503` |

### Run locally

```bash
WEBHOOK_SECRET=changeme DATABASE_PATH=./inbox.db \
  .venv/bin/python -m uvicorn compose:app --host 127.0.0.1 --port 8000
```

### Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `WEBHOOK_SECRET` | Yes | — | HMAC key used to verify `X-Webhook-Signature` |
| `DATABASE_PATH` | No | `./webhook_inbox.db` | SQLite database path |

### Docker

```bash
docker build -t webhook-inbox .
docker run --rm -p 127.0.0.1:8000:8000 \
  -e WEBHOOK_SECRET=changeme \
  -v webhook-inbox-data:/data \
  webhook-inbox
```

The image runs as UID 1001, persists SQLite data under `/data`, and reports
container health through `GET /health`.

### Signed webhook example

```bash
SECRET="changeme"
BODY='{"type":"invoice.paid","amount":42}'
SIG="$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | sed 's/^.* //')"

curl -sS -X POST http://127.0.0.1:8000/webhooks \
  -H "Content-Type: application/json" \
  -H "X-Event-ID: evt-$(date +%s)" \
  -H "X-Webhook-Signature: sha256=$SIG" \
  --data-binary "$BODY"
```

Replaying the exact event ID and raw body returns `200` with
`"duplicate": true`. Reusing the ID with different bytes returns `409`.

## Production constraints

- HMAC comparison is constant time, and signature verification happens before
  JSON parsing.
- The 1 MiB body limit is enforced on raw bytes before persistence.
- SQLite uniqueness and transactions are the deduplication authority; no
  process-local mutex is required.
- Missing secrets fail startup. Secrets, signature headers, and full payloads
  are not logged.
- Dependencies are pinned with hashes. CI covers Python 3.10 through 3.13.
- The Docker image runs as UID 1001 and has a container healthcheck.

## About oh-my-multica

[oh-my-multica](https://github.com/xiaohei-info/oh-my-multica) adds a software
delivery control layer to Multica. In this demo, Agents chose the design,
decomposed the requirement, wrote the code, and reviewed the changes. The Loop
handled dependencies, evidence gates, merge conditions, recovery, and the final
completion decision.

Read the [oh-my-multica README](https://github.com/xiaohei-info/oh-my-multica#readme) for the delivery model behind
this project.

## License

[MIT](LICENSE)
