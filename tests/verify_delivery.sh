#!/usr/bin/env bash
# Verifies the production-constrained delivery surface:
#   - clean hashed-dependency install
#   - CI structure present
#   - README has the required operator sections
#   - non-root container with a working /health endpoint
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "OK:   $*"; }

# --- 1. Hashed dependencies install cleanly in an isolated venv ---------------------------
VENV="$(mktemp -d)/venv"
python3 -m venv "$VENV"
PATH="$VENV/bin:$PATH" PIP_CERT=REQUESTS_CA_BUNDLE=""
"$VENV/bin/pip" install --require-hashes --no-deps --no-cache-dir -r requirements.txt >/tmp/hashed-install.log 2>&1 \
  || fail "pip install with --require-hashes failed; see /tmp/hashed-install.log"
ok "installs from pinned, hashed requirements.txt"

# --- 2. Required CI file exists with the right matrix -----------------------------------
test -f .github/workflows/ci.yml || fail "missing .github/workflows/ci.yml"
for token in "branches: \[main\]" '"3.10"' '"3.11"' '"3.12"' '"3.13"' "docker build"; do
  grep -q "$token" .github/workflows/ci.yml || fail ".github/workflows/ci.yml missing \"$token\""
done
ok "ci.yml has push/PR triggers, Python 3.10-3.13 matrix, and docker build"

# --- 3. README contains architecture / setup / test / docker / env / signed example ----
test -f README.md || fail "missing README.md"
for section in "Architecture" "Local setup" "Tests" "Docker" "Environment variables" "Signed webhook example"; do
  grep -qF "### $section" README.md || fail "README missing section \"### $section\""
done
# signed example must contain an openssl sha256 surface + curl usage
grep -q 'openssl dgst -sha256 -hmac' README.md || fail "README signed example missing openssl sha256 hmac"
grep -q 'curl -sS' README.md || fail "README signed example missing curl -sS"
ok "README covers architecture/setup/test/docker/env/signed-example sections"

# --- 4. Dockerfile: non-root user + healthcheck ------------------------------------------
test -f Dockerfile || fail "missing Dockerfile"
grep -qE 'USER app|USER 1001' Dockerfile || fail "Dockerfile does not set a non-root USER"
grep -qE 'HEALTHCHECK' Dockerfile || fail "Dockerfile missing HEALTHCHECK"
ok "Dockerfile is non-root and has a healthcheck"

# --- 5. docker runtime smoke test (only if docker is available) -------------------------
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  WEBHOOK_SECRET="${WEBHOOK_SECRET:-delivery-test-secret}"
  img="webhook-inbox:verify"
  db_dir="$(mktemp -d)"
  trap 'docker rm -f verify-inbox >/dev/null 2>&1 || true; rm -rf "$db_dir"' EXIT
  docker build -t "$img" . >/tmp/docker-build.log 2>&1 || { cat /tmp/docker-build.log; fail "docker build failed"; }
  container_user_id="$(docker run --rm --entrypoint id "$img" | grep -oE 'uid=[0-9]+' | head -1)"
  test "$container_user_id" = "uid=1001" || fail "container did not run as non-root: $container_user_id"
  docker run -d --name verify-inbox -p 127.0.0.1::8000 \
    -e WEBHOOK_SECRET -v "$db_dir:/data" "$img" >/dev/null
  port="$(docker port verify-inbox 8000/tcp | sed 's/.*://')"
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$port/health" >/tmp/health.json 2>/dev/null && break
    sleep 0.2
  done
  test -s /tmp/health.json || fail "container /health did not answer up"
  body='{"type":"verify"}'; sig="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | sed 's/^.* //')"
  test "$(curl -sS -o /tmp/new.json -w '%{http_code}' -X POST "http://127.0.0.1:$port/webhooks" \
    -H "X-Event-ID: evt-verify-1" -H "X-Webhook-Signature: sha256=$sig" \
    -H 'Content-Type: application/json' --data-binary "$body")" = "201"
  test "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$port/webhooks" \
    -H "X-Event-ID: evt-verify-1" -H "X-Webhook-Signature: sha256=$sig" \
    -H 'Content-Type: application/json' --data-binary "$body")" = "200"
  docker rm -f verify-inbox >/dev/null 2>&1 || true
  docker rmi "$img" >/dev/null 2>&1 || true
  ok "container runs as non-root, answers /health, accepts a valid signed webhook"
else
  echo "SKIP: docker not available; runtime smoke test skipped"
fi

echo ""
echo "ALL DELIVERY CHECKS PASSED"
