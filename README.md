# Webhook Inbox Demo

A small, production-constrained webhook inbox built with **FastAPI** and **SQLite**.
It verifies HMAC-SHA256 webhook signatures, stores events atomically, and makes
duplicates idempotent through a database uniqueness constraint rather than an
in-memory lock.

The service is a demonstration project for [oh-my-multica](https://github.com/xiaohei-info/oh-my-multica-demo-webhook-inbox):
the featured HTTP, persistence, container, and operator tracks are each defined
in [GOAL.md](GOAL.md#production-constraints) and delivered through reviewable PRs.

---

### Architecture

```
            ┌─────────────────────────────┐
  HTTPS ─── │  FastAPI (src/api.py)        │
            │  - bounded raw-body stream   │
            │  - stable JSON error shape   │
            └─────────────┬───────────────┘
                          │ EventResult / Event / HealthResult
                          ▼
            ┌─────────────────────────────┐
            │  Service (src/service.py)    │
            │  - verify-before-parse       │
            │  - constant-time HMAC        │
            └─────────────┬───────────────┘
                          │
                          ▼
            ┌─────────────────────────────┐
            │  Repository (src/repository)  │
            │  - SQLite PRIMARY KEY dedup   │
            │  - exact-byte comparison      │
            │  - WAL concurrent readers     │
            └─────────────────────────────┘
```

Framework code (`src/api.py`) pushes bytes without parsing JSON: the 1 MiB
content-length limit runs down the raw stream, *before* any HMAC or JSON parsing
happens. Service code (`src/service.py`) verifies the signature before it parses
the body. Persistence (`src/repository.py`) relies on a SQLite PRIMARY KEY and
exact-byte comparison for dedup; there is no in-memory lock.

**Endpoints:**

| Method | Path             | Success | Error cases                                                |
|--------|------------------|---------|------------------------------------------------------------|
| POST   | `/webhooks`      | `201`   | `401` invalid/missing sig · `400` bad JSON / ID · `413` · `409` |
| GET    | `/events/{id}`   | `200`   | `404`                                                      |
| GET    | `/health`        | `200`   | `503`                                                      |

### Local setup

Requires Python 3.10+ and the pinned [requirements.txt](requirements.txt).

```bash
python3 -m venv .venv
source .venv/bin/activate
# The requirements file carries --hash=sha256 pins for every artifact.
# --require-hashes makes the install fail closed if any wheel is tampered.
pip install --require-hashes --no-deps --no-cache-dir -r requirements.txt
pip install --no-deps -e ".[dev]"
```

Run the service:

```bash
WEBHOOK_SECRET=changeme DATABASE_PATH=./inbox.db \
  uvicorn compose:app --host 127.0.0.1 --port 8000 --reload
```

### Tests

```bash
mypy src
ruff check src tests
ruff format --check src tests
pytest --cov=src --cov-branch --cov-report=term-missing --cov-fail-under=90
```

The unit suite is framework-isolated: `tests/test_api.py` uses the `ReceivingService`
fake, `tests/test_repository.py` runs real SQLite, `tests/test_drive_integration.py`
exists only for full-path scenarios documented in the acceptance flows.

### Dockerfile usage

A multi-stage build produces a non-root image and a container-level healthcheck.
Pip installs happen with `--require-hashes`.

```bash
docker build --network=host -t webhook-inbox .
docker run --rm -p 127.0.0.1:8000:8000 \
  -e WEBHOOK_SECRET=devpw -v inbox-data:/data \
  webhook-inbox:latest
```

Container behaviour:

* image uses the unprivileged `USER app` (uid `1001`)
* `HEALTHCHECK` queries `http://127.0.0.1:8000/health`
* the RW `/data` volume holds the SQLite database

### Environment variables

| Variable         | Required | Default                       | Purpose                                         |
|------------------|:--------:|-------------------------------|-------------------------------------------------|
| `WEBHOOK_SECRET` | yes      | —                             | HMAC key used for `X-Webhook-Signature`         |
| `DATABASE_PATH`  | no       | `./webhook_inbox.db`         | Path to the SQLite file used by the repository  |

Startup fails fast with `StartupError` if `WEBHOOK_SECRET` is unset or empty.
Secrets, signatures, and full webhook payloads are never logged.

### Signed webhook example

Generate the signature against the **raw** body bytes, then send it to the service:

```bash
SECRET="test-secret"
BODY='{"type":"invoice.paid","amount":42}'

# sha256=<hex-hmac-of-raw-body>
SIG="$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | sed 's/^.* //')"

curl -sS -X POST http://127.0.0.1:8000/webhooks \
  -H "Content-Type: application/json" \
  -H "X-Event-ID: evt-$(date +%s)" \
  -H "X-Webhook-Signature: sha256=$SIG" \
  --data-binary "$BODY"
```

Replaying the exact same event id and raw body returns `200` with
`"duplicate": true`; the same event id with a different body returns `409`.
