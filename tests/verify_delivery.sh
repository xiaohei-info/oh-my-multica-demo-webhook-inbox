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

run_dir="$(mktemp -d)"
container_id=""
image_name=""
cleanup() {
  if test -n "$container_id" && command -v docker >/dev/null 2>&1; then
    docker rm -f "$container_id" >/dev/null 2>&1 || true
  fi
  if test -n "$image_name" && command -v docker >/dev/null 2>&1; then
    docker rmi "$image_name" >/dev/null 2>&1 || true
  fi
  rm -rf "$run_dir"
}
trap cleanup EXIT

# --- 1. Hashed dependencies install cleanly in an isolated venv ---------------------------
VENV="$run_dir/venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --require-hashes --no-deps --no-cache-dir -r requirements.txt >"$run_dir/hashed-install.log" 2>&1 \
  || { cat "$run_dir/hashed-install.log"; fail "pip install with --require-hashes failed"; }
ok "installs from pinned, hashed requirements.txt"
for package in backports-asyncio-runner exceptiongroup tomli; do
  grep -Eq "^${package}==.+python_version < \"3\\.11\"" requirements.txt \
    || fail "requirements.txt is missing the Python 3.10 compatibility pin for ${package}"
done
ok "hashed lock retains Python 3.10 compatibility dependencies"

# --- 2. Required CI file exists with the right matrix -----------------------------------
test -f .github/workflows/ci.yml || fail "missing .github/workflows/ci.yml"
for token in "branches: \[main\]" '"3.10"' '"3.11"' '"3.12"' '"3.13"' "docker build"; do
  grep -q "$token" .github/workflows/ci.yml || fail ".github/workflows/ci.yml missing \"$token\""
done
quality_job="$(sed -n '/^  quality:/,/^  test:/p' .github/workflows/ci.yml)"
grep -q -- '--require-hashes.*requirements.txt' <<<"$quality_job" \
  || fail "quality job does not install hash-pinned dev tools before lint"
grep -q 'python -m mypy' <<<"$quality_job" \
  || fail "quality job does not run the repository typecheck"
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
  run_id="$(date +%s)-$$"
  image_name="webhook-inbox:verify-$run_id"
  container_name="verify-inbox-$run_id"
  db_dir="$run_dir/data"
  mkdir -p "$db_dir"
  docker build -t "$image_name" . >"$run_dir/docker-build.log" 2>&1 \
    || { cat "$run_dir/docker-build.log"; fail "docker build failed"; }
  container_user_id="$(docker run --rm --entrypoint id "$image_name" | grep -oE 'uid=[0-9]+' | head -1)"
  test "$container_user_id" = "uid=1001" || fail "container did not run as non-root: $container_user_id"
  container_id="$(docker run -d --name "$container_name" -p 127.0.0.1::8000 \
    -e WEBHOOK_SECRET="$WEBHOOK_SECRET" -v "$db_dir:/data" "$image_name")" \
    || fail "docker run failed"
  port="$(docker port "$container_id" 8000/tcp | sed 's/.*://')"
  test -n "$port" || fail "could not resolve mapped host port"
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$port/health" >"$run_dir/health.json" 2>/dev/null && break
    sleep 0.2
  done
  test -s "$run_dir/health.json" || fail "container /health did not answer up"
  health="starting"
  for _ in $(seq 1 150); do
    health="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id")"
    test "$health" = "healthy" && break
    sleep 0.5
  done
  test "$health" = "healthy" || fail "container did not reach healthy state (was: ${health:-unknown})"
  body='{"type":"verify"}'; sig="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | sed 's/^.* //')"
  test "$(curl -sS -o "$run_dir/new.json" -w '%{http_code}' -X POST "http://127.0.0.1:$port/webhooks" \
    -H "X-Event-ID: evt-verify-1" -H "X-Webhook-Signature: sha256=$sig" \
    -H 'Content-Type: application/json' --data-binary "$body")" = "201"
  test "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$port/webhooks" \
    -H "X-Event-ID: evt-verify-1" -H "X-Webhook-Signature: sha256=$sig" \
    -H 'Content-Type: application/json' --data-binary "$body")" = "200"
  ok "container runs as non-root, answers /health, reaches healthy, accepts a valid signed webhook"
else
  echo "SKIP: docker not available; runtime smoke test skipped"
fi

echo ""
echo "ALL DELIVERY CHECKS PASSED"
