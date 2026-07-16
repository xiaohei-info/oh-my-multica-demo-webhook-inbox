#!/usr/bin/env bash
# Checked-in, end-to-end acceptance harness for the Webhook Inbox.
#
# Reproduces every approved AITEAM-788 acceptance probe against the
# integrated default branch. Each flow starts its own server process from a
# temporary database, exercises the public HTTP surface with the real
# composed app (compose:app -- the same entry point used by the Dockerfile
# and CI), and asserts the documented outcome.
#
# compose:app is used rather than `uvicorn src.api:app` because the module
# attribute src.api:app is the intentionally minimal _StubService
# placeholder that wires no secret validation, persistence, or service
# layer, so it cannot satisfy a single flow. The real integration root is
# compose.py (see Dockerfile CMD, README "Run the service", CI).

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi

pass=0
fail=0
note() { printf '  - %s\n' "$*"; }

kill_server() {
  if [ -n "${AC_PID:-}" ]; then
    kill "$AC_PID" 2>/dev/null || true
    wait "$AC_PID" 2>/dev/null || true
    AC_PID=""
  fi
}

cleanup_server() {
  kill_server
  [ -z "${AC_TMP:-}" ] || rm -rf "$AC_TMP"
  AC_TMP=""
}

boot_server() {
  cleanup_server
  AC_TMP="$(mktemp -d)"
  AC_LOG="$AC_TMP/server.log"
  AC_DB="$AC_TMP/inbox.db"
  AC_PORT="$($PY -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
  WEBHOOK_SECRET="test-secret" DATABASE_PATH="$AC_DB" \
    $PY -m uvicorn compose:app --host 127.0.0.1 --port "$AC_PORT" >"$AC_LOG" 2>&1 &
  AC_PID=$!
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$AC_PORT/health" >/dev/null 2>&1 && return 0
    kill -0 "$AC_PID" 2>/dev/null || break
    sleep 0.1
  done
  note "server did not become healthy"
  tail -20 "$AC_LOG" >&2 || true
  return 1
}

wait_bounded() {
  local pid="$1" rc
  for _ in $(seq 1 50); do
    if ! kill -0 "$pid" 2>/dev/null; then
      if wait "$pid"; then return 0; else rc=$?; return "$rc"; fi
    fi
    sleep 0.1
  done
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  return 124
}

secret="test-secret"
sign() { printf '%s' "$1" | openssl dgst -sha256 -hmac "$secret" -hex | sed 's/^.* //'; }

echo "Webhook Inbox acceptance - AITEAM-788"
echo "====================================="

run_flow() {
  local name="$1" rc; shift
  (
    AC_TMP=""; AC_PID=""
    trap cleanup_server EXIT
    set -euo pipefail
    "$@"
  )
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "PASS: $name"; pass=$((pass + 1))
  else
    echo "FAIL: $name"; fail=$((fail + 1))
  fi
}

# 1. flow-receive-valid-webhook
flow_receive_valid_webhook() {
  boot_server || return 1
  note "valid signed delivery returns 201 with stored event"
  local body='{"type":"invoice.paid","amount":42}' sig
  sig=$(sign "$body")
  code=$(curl -sS -o "$AC_TMP/new.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-valid-1' -H "X-Webhook-Signature: sha256=$sig" \
    --data-binary "$body")
  [ "$code" = "201" ] || { note "expected 201, got $code"; return 1; }
  for pair in 'evt-empty-object:{}' 'evt-empty-array:[]'; do
    id=${pair%%:*}; payload=${pair#*:}
    h=$(sign "$payload")
    c=$(curl -sS -o "$AC_TMP/$id.json" -w '%{http_code}' -X POST \
      "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
      -H "X-Event-ID: $id" -H "X-Webhook-Signature: sha256=$h" \
      --data-binary "$payload")
    [ "$c" = "201" ] || { note "valid signed $id expected 201, got $c"; return 1; }
  done
  note "exact bytes stored once; logs expose neither secret nor payload"
  $PY - "$AC_TMP/new.json" "$AC_DB" "$AC_LOG" <<'PY'
import json, sqlite3, sys
response, db, log = sys.argv[1:]
item = json.load(open(response))
assert item["event_id"] == "evt-valid-1"
assert item["payload"] == {"type": "invoice.paid", "amount": 42}
assert item["received_at"]
rows = sqlite3.connect(db).execute(
    "SELECT event_id, body_raw, duplicate_count FROM events ORDER BY event_id"
).fetchall()
assert rows == [
    ("evt-empty-array", b"[]", 0),
    ("evt-empty-object", b"{}", 0),
    ("evt-valid-1", b'{"type":"invoice.paid","amount":42}', 0),
], rows
text = open(log).read()
assert "test-secret" not in text and '{"type":"invoice.paid","amount":42}' not in text
PY
}

# 2. flow-reject-missing-signature
flow_reject_missing_signature() {
  boot_server || return 1
  note "unsigned delivery returns 401 missing_signature, nothing stored"
  code=$(curl -sS -o "$AC_TMP/response.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-no-signature' --data-binary '{"ok":true}')
  [ "$code" = "401" ] || { note "expected 401, got $code"; return 1; }
  $PY - "$AC_TMP/response.json" "$AC_DB" <<'PY'
import json, sqlite3, sys
body = json.load(open(sys.argv[1]))
assert body["error"] == "missing_signature" and isinstance(body["message"], str) and body["message"]
assert sqlite3.connect(sys.argv[2]).execute("SELECT count(*) FROM events").fetchone()[0] == 0
PY
}

# 3. flow-reject-invalid-signature
flow_reject_invalid_signature() {
  boot_server || return 1
  note "malformed and non-matching signatures return 401 invalid_signature"
  for header in 'sha256=deadbeef' 'not-a-signature'; do
    code=$(curl -sS -o "$AC_TMP/${header//=/_}.json" -w '%{http_code}' -X POST \
      "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
      -H 'X-Event-ID: evt-invalid-signature' -H "X-Webhook-Signature: $header" \
      --data-binary '{"ok":true}')
    [ "$code" = "401" ] || { note "header $header expected 401, got $code"; return 1; }
  done
  $PY - "$AC_TMP" "$AC_DB" <<'PY'
import json, pathlib, sqlite3, sys
for path in pathlib.Path(sys.argv[1]).glob("*.json"):
    body = json.load(open(path))
    assert body["error"] == "invalid_signature" and isinstance(body["message"], str) and body["message"]
assert sqlite3.connect(sys.argv[2]).execute("SELECT count(*) FROM events").fetchone()[0] == 0
PY
}

# 4. flow-reject-missing-event-id (missing/empty id, then invalid json)
flow_reject_missing_event_id() {
  boot_server || return 1
  note "absent and empty X-Event-ID return 400 missing_event_id"
  local body='{"ok":true}' sig
  sig=$(sign "$body")
  code=$(curl -sS -o "$AC_TMP/absent.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H "X-Webhook-Signature: sha256=$sig" --data-binary "$body")
  [ "$code" = "400" ] || { note "absent id expected 400, got $code"; return 1; }
  code=$(curl -sS -o "$AC_TMP/empty.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID;' -H "X-Webhook-Signature: sha256=$sig" --data-binary "$body")
  [ "$code" = "400" ] || { note "empty id expected 400, got $code"; return 1; }
  $PY - "$AC_TMP/absent.json" "$AC_TMP/empty.json" "$AC_DB" <<'PY'
import json, sqlite3, sys
for response in sys.argv[1:3]:
    body = json.load(open(response))
    assert body["error"] == "missing_event_id" and isinstance(body["message"], str) and body["message"]
assert sqlite3.connect(sys.argv[3]).execute("SELECT count(*) FROM events").fetchone()[0] == 0
PY
  note "signed empty and malformed JSON return 400 invalid_json"
  for pair in 'evt-empty-json:' 'evt-invalid-json:{not-json'; do
    id=${pair%%:*}; payload=${pair#*:}
    h=$(sign "$payload")
    c=$(curl -sS -o "$AC_TMP/$id.json" -w '%{http_code}' -X POST \
      "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
      -H "X-Event-ID: $id" -H "X-Webhook-Signature: sha256=$h" \
      --data-binary "$payload")
    [ "$c" = "400" ] || { note "$id expected 400, got $c"; return 1; }
  done
  $PY - "$AC_TMP" "$AC_DB" <<'PY'
import json, pathlib, sqlite3, sys
for path in pathlib.Path(sys.argv[1]).glob("evt-*.json"):
    body = json.load(open(path))
    assert body["error"] == "invalid_json" and isinstance(body["message"], str) and body["message"]
assert sqlite3.connect(sys.argv[2]).execute("SELECT count(*) FROM events").fetchone()[0] == 0
PY
}

# 5. flow-idempotent-duplicate (sequential + concurrent + restart)
flow_idempotent_duplicate() {
  boot_server || return 1
  local body='{"kind":"same","n":1}' sig
  sig=$(sign "$body")
  note "first delivery 201, second 200 duplicate"
  first=$(curl -sS -o "$AC_TMP/first.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-duplicate-1' -H "X-Webhook-Signature: sha256=$sig" \
    --data-binary "$body")
  [ "$first" = "201" ] || { note "first expected 201, got $first"; return 1; }
  second=$(curl -sS -o "$AC_TMP/second.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-duplicate-1' -H "X-Webhook-Signature: sha256=$sig" \
    --data-binary "$body")
  [ "$second" = "200" ] || { note "second expected 200, got $second"; return 1; }
  $PY - "$AC_TMP/first.json" "$AC_TMP/second.json" "$AC_DB" <<'PY'
import json, sqlite3, sys
first = json.load(open(sys.argv[1])); second = json.load(open(sys.argv[2]))
assert second["duplicate"] is True and first["received_at"] == second["received_at"]
assert sqlite3.connect(sys.argv[3]).execute(
    "SELECT count(*), duplicate_count, body_raw FROM events WHERE event_id=?",
    ("evt-duplicate-1",)).fetchone() == (1, 1, b'{"kind":"same","n":1}')
PY
  kill_server
  assert_concurrent_restart_durability
}

# 6. flow-conflict-different-body
flow_conflict_different_body() {
  boot_server || return 1
  note "same id with changed body returns 409, original row unchanged"
  local first='{"state":"first"}' second='{"state":"second"}'
  first_sig=$(sign "$first"); second_sig=$(sign "$second")
  code=$(curl -sS -o "$AC_TMP/first.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-conflict-1' -H "X-Webhook-Signature: sha256=$first_sig" \
    --data-binary "$first")
  [ "$code" = "201" ] || { note "first expected 201, got $code"; return 1; }
  code=$(curl -sS -o "$AC_TMP/conflict.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-conflict-1' -H "X-Webhook-Signature: sha256=$second_sig" \
    --data-binary "$second")
  [ "$code" = "409" ] || { note "conflict expected 409, got $code"; return 1; }
  $PY - "$AC_TMP/conflict.json" "$AC_DB" <<'PY'
import json, sqlite3, sys
body = json.load(open(sys.argv[1]))
assert body["error"] == "conflict" and isinstance(body["message"], str) and body["message"]
assert sqlite3.connect(sys.argv[2]).execute(
    "SELECT count(*), body_raw, duplicate_count FROM events WHERE event_id=?",
    ("evt-conflict-1",)).fetchone() == (1, b'{"state":"first"}', 0)
PY
}

# 7. flow-body-too-large
flow_body_too_large() {
  boot_server || return 1
  note "exactly 1 MiB accepted (201), 1 MiB+1 rejected (413)"
  $PY - "$AC_TMP/exact.json" "$AC_TMP/over.json" <<'PY'
import sys
prefix, suffix = b'{"data":"', b'"}'
for path, size in zip(sys.argv[1:], (1048576, 1048577)):
    payload = prefix + b'x' * (size - len(prefix) - len(suffix)) + suffix
    assert len(payload) == size
    open(path, 'wb').write(payload)
PY
  exact_sig=$(openssl dgst -sha256 -hmac test-secret -hex "$AC_TMP/exact.json" | sed 's/^.* //')
  over_sig=$(openssl dgst -sha256 -hmac test-secret -hex "$AC_TMP/over.json" | sed 's/^.* //')
  code=$(curl -sS -o "$AC_TMP/exact-response.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-exact-limit' -H "X-Webhook-Signature: sha256=$exact_sig" \
    --data-binary @"$AC_TMP/exact.json")
  [ "$code" = "201" ] || { note "exact limit expected 201, got $code"; return 1; }
  code=$(curl -sS -o "$AC_TMP/over-response.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-over-limit' -H "X-Webhook-Signature: sha256=$over_sig" \
    --data-binary @"$AC_TMP/over.json")
  [ "$code" = "413" ] || { note "over limit expected 413, got $code"; return 1; }
  $PY - "$AC_TMP/over-response.json" "$AC_DB" <<'PY'
import json, sqlite3, sys
body = json.load(open(sys.argv[1]))
assert body["error"] == "body_too_large" and isinstance(body["message"], str) and body["message"]
db = sqlite3.connect(sys.argv[2])
assert db.execute("SELECT length(body_raw) FROM events WHERE event_id=?",
    ("evt-exact-limit",)).fetchone() == (1048576,)
assert db.execute("SELECT count(*) FROM events WHERE event_id=?",
    ("evt-over-limit",)).fetchone()[0] == 0
PY
}

# 8. flow-get-event
flow_get_event() {
  boot_server || return 1
  note "stored event retrieved with original fields"
  local body='{"order":123,"status":"paid"}' sig
  sig=$(sign "$body")
  code=$(curl -sS -o "$AC_TMP/created.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$AC_PORT/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-get-1' -H "X-Webhook-Signature: sha256=$sig" \
    --data-binary "$body")
  [ "$code" = "201" ] || { note "create expected 201, got $code"; return 1; }
  code=$(curl -sS -o "$AC_TMP/fetched.json" -w '%{http_code}' \
    "http://127.0.0.1:$AC_PORT/events/evt-get-1")
  [ "$code" = "200" ] || { note "get expected 200, got $code"; return 1; }
  $PY - "$AC_TMP/created.json" "$AC_TMP/fetched.json" <<'PY'
import json, sys
created = json.load(open(sys.argv[1])); fetched = json.load(open(sys.argv[2]))
assert fetched["event_id"] == "evt-get-1"
assert fetched["payload"] == {"order": 123, "status": "paid"}
assert fetched["received_at"] == created["received_at"]
PY
}

# flow-idempotent-duplicate: concurrent-and-restart durability sub-probe
assert_concurrent_restart_durability() (
  set -euo pipefail
  note "concurrent same-id yields one 201/one 200; row survives restart"
  local tmp port db pid body sig one two
  tmp="$AC_TMP/concurrent"
  mkdir -p "$tmp"
  port="$($PY -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
  db="$tmp/inbox.db"
  _sr() { WEBHOOK_SECRET=test-secret DATABASE_PATH="$db" $PY -m uvicorn compose:app --host 127.0.0.1 --port "$port" >"$tmp/server.log" 2>&1 & pid=$!; }
  _up() { for _ in $(seq 1 50); do curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0; sleep 0.1; done; return 1; }
  _down() { [ -z "${pid:-}" ] || { kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; pid=""; }; }
  _sr
  trap _down EXIT
  _up || { note "server failed to start"; return 1; }
  body='{"kind":"concurrent"}'
  sig=$(sign "$body")
  curl -sS -o "$tmp/one.json" -w '%{http_code}' -X POST "http://127.0.0.1:$port/webhooks" \
    -H 'Content-Type: application/json' -H 'X-Event-ID: evt-concurrent-1' \
    -H "X-Webhook-Signature: sha256=$sig" --data-binary "$body" >"$tmp/one.status" & one=$!
  curl -sS -o "$tmp/two.json" -w '%{http_code}' -X POST "http://127.0.0.1:$port/webhooks" \
    -H 'Content-Type: application/json' -H 'X-Event-ID: evt-concurrent-1' \
    -H "X-Webhook-Signature: sha256=$sig" --data-binary "$body" >"$tmp/two.status" & two=$!
  wait "$one"; wait "$two"
  [ "$(sort "$tmp/one.status" "$tmp/two.status" | tr '\n' ' ')" = "200 201 " ] || {
    note "concurrent statuses were $(cat "$tmp/one.status")$(cat "$tmp/two.status")"; return 1; }
  received_at=$($PY - "$db" <<'PY'
import sqlite3, sys
row = sqlite3.connect(sys.argv[1]).execute(
    "SELECT count(*), duplicate_count, received_at FROM events WHERE event_id=?",
    ("evt-concurrent-1",)).fetchone()
assert row[0:2] == (1, 1), row; print(row[2])
PY
)
  _down; _sr && _up || { note "restart failed"; return 1; }
  code=$(curl -sS -o "$tmp/restart.json" -w '%{http_code}' -X POST \
    "http://127.0.0.1:$port/webhooks" -H 'Content-Type: application/json' \
    -H 'X-Event-ID: evt-concurrent-1' -H "X-Webhook-Signature: sha256=$sig" \
    --data-binary "$body")
  [ "$code" = "200" ] || { note "post-restart expected 200, got $code"; return 1; }
  $PY - "$tmp/restart.json" "$db" "$received_at" <<'PY'
import json, sqlite3, sys
body = json.load(open(sys.argv[1])); assert body["duplicate"] is True and body["received_at"] == sys.argv[3]
assert sqlite3.connect(sys.argv[2]).execute(
    "SELECT count(*), duplicate_count, received_at FROM events WHERE event_id=?",
    ("evt-concurrent-1",)).fetchone() == (1, 2, sys.argv[3])
PY
)

# 9. flow-get-event-not-found
flow_get_event_not_found() {
  boot_server || return 1
  note "unknown event returns 404 not_found"
  code=$(curl -sS -o "$AC_TMP/response.json" -w '%{http_code}' \
    "http://127.0.0.1:$AC_PORT/events/evt-unknown-1")
  [ "$code" = "404" ] || { note "expected 404, got $code"; return 1; }
  $PY - "$AC_TMP/response.json" <<'PY'
import json, sys
body = json.load(open(sys.argv[1]))
assert body["error"] == "not_found" and isinstance(body["message"], str) and body["message"]
PY
}

# 10. flow-health (healthy db + absent/empty secret startup failure)
flow_health() {
  boot_server || return 1
  note "healthy service reports database health, logs no secret"
  code=$(curl -sS -o "$AC_TMP/response.json" -w '%{http_code}' \
    "http://127.0.0.1:$AC_PORT/health")
  [ "$code" = "200" ] || { note "health expected 200, got $code"; return 1; }
  $PY - "$AC_TMP/response.json" "$AC_LOG" <<'PY'
import json, sys
body = json.load(open(sys.argv[1]))
assert isinstance(body, dict) and "error" not in body
observed = [str(v).lower() for k, v in body.items() if "database" in str(k).lower()]
assert observed and any(v in {"true", "ok", "healthy", "reachable"} for v in observed), body
assert "test-secret" not in open(sys.argv[2]).read()
PY
  note "absent and empty WEBHOOK_SECRET fail startup with StartupError"
  local tmp log_absent log_empty pid_a pid_e rc_a rc_e
  tmp="$AC_TMP/startup"; mkdir -p "$tmp"
  log_absent="$tmp/absent.log"; log_empty="$tmp/empty.log"
  DATABASE_PATH="$tmp/absent.db" $PY -m uvicorn compose:app --host 127.0.0.1 --port 0 >"$log_absent" 2>&1 & pid_a=$!
  DATABASE_PATH="$tmp/empty.db" WEBHOOK_SECRET='' $PY -m uvicorn compose:app --host 127.0.0.1 --port 0 >"$log_empty" 2>&1 & pid_e=$!
  if wait_bounded "$pid_a"; then rc_a=0; else rc_a=$?; fi
  if wait_bounded "$pid_e"; then rc_e=0; else rc_e=$?; fi
  [ "$rc_a" -ne 0 ] && [ "$rc_a" -ne 124 ] || { note "absent secret exited $rc_a"; return 1; }
  [ "$rc_e" -ne 0 ] && [ "$rc_e" -ne 124 ] || { note "empty secret exited $rc_e"; return 1; }
  grep -q 'StartupError' "$log_absent" || { note "no StartupError in absent log"; return 1; }
  grep -q 'StartupError' "$log_empty" || { note "no StartupError in empty log"; return 1; }
}

# 11. flow-health-db-unhealthy (delegated deterministic test per acceptance doc)
flow_health_db_unhealthy() {
  note "health returns 503 db_unhealthy when readiness query fails"
  $PY -m pytest -q "tests/test_api.py::test_health_db_unhealthy"
}

echo ""
echo "---"
run_flow "flow-receive-valid-webhook"       flow_receive_valid_webhook
run_flow "flow-reject-missing-signature"     flow_reject_missing_signature
run_flow "flow-reject-invalid-signature"     flow_reject_invalid_signature
run_flow "flow-reject-missing-event-id"      flow_reject_missing_event_id
run_flow "flow-idempotent-duplicate"         flow_idempotent_duplicate
run_flow "flow-conflict-different-body"      flow_conflict_different_body
run_flow "flow-body-too-large"               flow_body_too_large
run_flow "flow-get-event"                    flow_get_event
run_flow "flow-get-event-not-found"          flow_get_event_not_found
run_flow "flow-health"                       flow_health
run_flow "flow-health-db-unhealthy"          flow_health_db_unhealthy

echo ""
echo "====================================="
echo "PASS: $pass   FAIL: $fail"
[ "$fail" -eq 0 ] || { echo "ACCEPTANCE FAILED"; exit 1; }
echo "ALL AITEAM-788 ACCEPTANCE FLOWS PASSED"
