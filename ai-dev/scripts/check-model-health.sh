#!/bin/bash
# Post-model-change health checks for vLLM
# Run after ConfigMap model swap + vllm-server rollout.
# Exit 0 only when pod is Ready, /health OK, and /v1/models lists the expected id.

set -euo pipefail

NAMESPACE="${NAMESPACE:-ai-dev}"
DEPLOYMENT="${DEPLOYMENT:-vllm-server}"
LABEL="${LABEL:-app=vllm}"
SERVICE="${SERVICE:-vllm-server}"
PORT="${PORT:-8000}"
TIMEOUT="${TIMEOUT:-600}"
EXPECTED_MODEL="${EXPECTED_MODEL:-}"
CHECK_PLEX="${CHECK_PLEX:-1}"
SKIP_COMPLETION="${SKIP_COMPLETION:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
info() { echo -e "${BLUE}→${NC} $1"; }

echo -e "${BLUE}=== vLLM Model Health Check ===${NC}"
echo "  namespace:  $NAMESPACE"
echo "  deployment: $DEPLOYMENT"
echo "  timeout:    ${TIMEOUT}s"
echo ""

if ! kubectl cluster-info &>/dev/null; then
  fail "Cannot connect to Kubernetes cluster"
fi

# Resolve expected served model name from live ConfigMap when not set
if [ -z "$EXPECTED_MODEL" ]; then
  EXPECTED_MODEL=$(kubectl get configmap vllm-config -n "$NAMESPACE" \
    -o jsonpath='{.data.SERVED_MODEL_NAME}' 2>/dev/null || true)
fi
info "Expected SERVED_MODEL_NAME: ${EXPECTED_MODEL:-<any>}"

# 1) Deployment available / rollout complete
info "Waiting for deployment/$DEPLOYMENT rollout (timeout ${TIMEOUT}s)..."
if ! kubectl rollout status "deployment/$DEPLOYMENT" -n "$NAMESPACE" --timeout="${TIMEOUT}s"; then
  fail "Deployment rollout did not complete within ${TIMEOUT}s"
fi
pass "Deployment rollout complete"

# 2) Pod Ready
info "Waiting for pod Ready ($LABEL)..."
if ! kubectl wait --for=condition=ready "pod" -l "$LABEL" -n "$NAMESPACE" --timeout="${TIMEOUT}s"; then
  fail "Pod not Ready within ${TIMEOUT}s"
fi
POD=$(kubectl get pods -n "$NAMESPACE" -l "$LABEL" -o jsonpath='{.items[0].metadata.name}')
pass "Pod Ready: $POD"

# 3) HTTP /health via port-forward
PF_PID=""
cleanup() {
  if [ -n "${PF_PID}" ] && kill -0 "$PF_PID" 2>/dev/null; then
    kill "$PF_PID" 2>/dev/null || true
    wait "$PF_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

LOCAL_PORT=18000
info "Port-forwarding svc/$SERVICE → localhost:${LOCAL_PORT}..."
kubectl port-forward -n "$NAMESPACE" "svc/$SERVICE" "${LOCAL_PORT}:${PORT}" >/dev/null 2>&1 &
PF_PID=$!
sleep 2

BASE_URL="http://127.0.0.1:${LOCAL_PORT}"

info "GET /health..."
HEALTH_OK=0
for i in $(seq 1 30); do
  if curl -sf --max-time 5 "${BASE_URL}/health" >/dev/null 2>&1; then
    HEALTH_OK=1
    break
  fi
  sleep 2
done
if [ "$HEALTH_OK" -ne 1 ]; then
  fail "/health did not return success"
fi
pass "/health OK"

# 4) /v1/models lists expected model
info "GET /v1/models..."
MODELS_JSON=$(curl -sf --max-time 10 "${BASE_URL}/v1/models" || true)
if [ -z "$MODELS_JSON" ]; then
  fail "/v1/models failed or empty"
fi
echo "$MODELS_JSON" | head -c 500
echo ""

if [ -n "$EXPECTED_MODEL" ]; then
  if echo "$MODELS_JSON" | grep -q "$EXPECTED_MODEL"; then
    pass "Served model present: $EXPECTED_MODEL"
  else
    fail "Expected model '$EXPECTED_MODEL' not found in /v1/models"
  fi
else
  warn "No EXPECTED_MODEL set; skipped model id assertion"
fi

# 5) Optional lightweight completion (verifies weights loaded)
if [ "$SKIP_COMPLETION" != "1" ] && [ -n "$EXPECTED_MODEL" ]; then
  info "POST /v1/completions (smoke)..."
  COMPLETION=$(curl -sf --max-time 60 "${BASE_URL}/v1/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${EXPECTED_MODEL}\",\"prompt\":\"def hello():\",\"max_tokens\":8,\"temperature\":0}" \
    || true)
  if [ -n "$COMPLETION" ] && echo "$COMPLETION" | grep -q "choices"; then
    pass "Completion smoke test OK"
  else
    fail "Completion smoke test failed"
  fi
fi

# 6) Optional Plex co-tenant check (GPU time-slicing)
if [ "$CHECK_PLEX" = "1" ] && [ -x "$SCRIPT_DIR/check-plex-health.sh" ]; then
  info "Running Plex co-tenant health check..."
  if bash "$SCRIPT_DIR/check-plex-health.sh"; then
    pass "Plex health OK"
  else
    warn "Plex health check reported issues (non-fatal for model swap)"
  fi
elif [ "$CHECK_PLEX" = "1" ]; then
  warn "check-plex-health.sh not found; skipping Plex check"
fi

echo ""
echo -e "${GREEN}=== All model health checks passed ===${NC}"
echo "  pod:   $POD"
echo "  model: ${EXPECTED_MODEL:-unknown}"
echo ""
exit 0
