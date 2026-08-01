#!/bin/bash
# Config-driven model hot-swap for vLLM (no full stack redeploy).
#
# Changes only:
#   1) ConfigMap vllm-config (model id / path / served name / runtime knobs)
#   2) Rollout of deployment/vllm-server
#
# Does NOT touch: Qdrant, code-indexer, SWE-agent, ingress, PVCs (contents
# persist; init container reuses cached MODEL_PATH directories on the PVC).
#
# Usage:
#   # Apply repo manifests as-is and restart vLLM
#   bash ai-dev/scripts/swap-model.sh
#
#   # Override model identity from CLI (updates live ConfigMap keys, then rolls out)
#   bash ai-dev/scripts/swap-model.sh \
#     --model-name "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ" \
#     --model-path "/models/qwen2.5-coder-7b-instruct-awq" \
#     --served-name "qwen2.5-coder-7b-instruct" \
#     --quantization "awq"
#
#   # Dry-run (print plan only)
#   bash ai-dev/scripts/swap-model.sh --dry-run
#
# See ai-dev/MODEL_HOT_SWAP.md for full procedure and PVC cache notes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_DEV_DIR="$(dirname "$SCRIPT_DIR")"
NAMESPACE="${NAMESPACE:-ai-dev}"
CONFIGMAP_FILE="${CONFIGMAP_FILE:-$AI_DEV_DIR/vllm/vllm-configmap.yaml}"
DEPLOYMENT="${DEPLOYMENT:-vllm-server}"
TIMEOUT="${TIMEOUT:-600}"
DRY_RUN=0
SKIP_HEALTH=0
SKIP_PLEX=0
APPLY_MANIFEST=1

# Optional CLI overrides (empty = leave ConfigMap key unchanged after apply)
MODEL_NAME=""
MODEL_PATH=""
MODEL_REVISION=""
SERVED_MODEL_NAME=""
QUANTIZATION=""
GPU_MEMORY_UTILIZATION=""
MAX_MODEL_LEN=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \?//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-health) SKIP_HEALTH=1; shift ;;
    --skip-plex) SKIP_PLEX=1; shift ;;
    --no-apply)
      # Only patch keys / restart; do not re-apply YAML from git
      APPLY_MANIFEST=0
      shift
      ;;
    --model-name) MODEL_NAME="$2"; shift 2 ;;
    --model-path) MODEL_PATH="$2"; shift 2 ;;
    --model-revision) MODEL_REVISION="$2"; shift 2 ;;
    --served-name) SERVED_MODEL_NAME="$2"; shift 2 ;;
    --quantization) QUANTIZATION="$2"; shift 2 ;;
    --gpu-memory) GPU_MEMORY_UTILIZATION="$2"; shift 2 ;;
    --max-model-len) MAX_MODEL_LEN="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    *)
      echo -e "${RED}Unknown argument: $1${NC}" >&2
      usage
      ;;
  esac
done

echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║     vLLM Config-Driven Model Hot-Swap (vLLM only)              ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}Scope:${NC} ConfigMap vllm-config + Deployment $DEPLOYMENT"
echo -e "${CYAN}Out of scope:${NC} full stack redeploy (qdrant, indexer, swe-agent, ingress)"
echo ""

if ! kubectl cluster-info &>/dev/null; then
  echo -e "${RED}✗ Cannot connect to Kubernetes cluster${NC}"
  exit 1
fi
echo -e "${GREEN}✓${NC} Cluster reachable"

if [ ! -f "$CONFIGMAP_FILE" ]; then
  echo -e "${RED}✗ ConfigMap file not found: $CONFIGMAP_FILE${NC}"
  exit 1
fi

# Show current live config (if present)
echo ""
echo -e "${BLUE}═══ Current live model config ═══${NC}"
if kubectl get configmap vllm-config -n "$NAMESPACE" &>/dev/null; then
  for key in MODEL_NAME MODEL_PATH MODEL_REVISION SERVED_MODEL_NAME QUANTIZATION GPU_MEMORY_UTILIZATION MAX_MODEL_LEN; do
    val=$(kubectl get configmap vllm-config -n "$NAMESPACE" -o "jsonpath={.data.${key}}" 2>/dev/null || echo "")
    printf "  %-28s %s\n" "$key:" "${val:-<missing>}"
  done
else
  echo -e "${YELLOW}  ConfigMap vllm-config not found in $NAMESPACE (will be created)${NC}"
fi

echo ""
echo -e "${BLUE}═══ Planned changes ═══${NC}"
echo "  Apply manifest: $([ "$APPLY_MANIFEST" = "1" ] && echo "yes ($CONFIGMAP_FILE)" || echo "no")"
[ -n "$MODEL_NAME" ] && echo "  MODEL_NAME              → $MODEL_NAME"
[ -n "$MODEL_PATH" ] && echo "  MODEL_PATH              → $MODEL_PATH"
[ -n "$MODEL_REVISION" ] && echo "  MODEL_REVISION          → $MODEL_REVISION"
[ -n "$SERVED_MODEL_NAME" ] && echo "  SERVED_MODEL_NAME       → $SERVED_MODEL_NAME"
[ -n "$QUANTIZATION" ] && echo "  QUANTIZATION            → $QUANTIZATION"
[ -n "$GPU_MEMORY_UTILIZATION" ] && echo "  GPU_MEMORY_UTILIZATION  → $GPU_MEMORY_UTILIZATION"
[ -n "$MAX_MODEL_LEN" ] && echo "  MAX_MODEL_LEN           → $MAX_MODEL_LEN"
echo "  Rollout: deployment/$DEPLOYMENT (Recreate strategy — brief downtime)"
echo "  Health:  $([ "$SKIP_HEALTH" = "1" ] && echo "skipped" || echo "check-model-health.sh after ready")"
echo ""

if [ "$DRY_RUN" = "1" ]; then
  echo -e "${YELLOW}Dry-run only — no changes applied.${NC}"
  exit 0
fi

# 1) Apply ConfigMap from repo (source of truth) unless --no-apply
if [ "$APPLY_MANIFEST" = "1" ]; then
  echo -e "${BLUE}═══ Applying ConfigMap ═══${NC}"
  kubectl apply -f "$CONFIGMAP_FILE"
  echo -e "${GREEN}✓${NC} Applied $CONFIGMAP_FILE"
fi

# 2) Patch individual keys when CLI overrides provided
patch_key() {
  local key="$1"
  local value="$2"
  if [ -n "$value" ]; then
    # Escape for JSON string
    local escaped
    escaped=$(printf '%s' "$value" | sed 's/\\/\\\\/g; s/"/\\"/g')
    kubectl patch configmap vllm-config -n "$NAMESPACE" --type merge \
      -p "{\"data\":{\"${key}\":\"${escaped}\"}}"
    echo -e "${GREEN}✓${NC} Patched $key"
  fi
}

if [ -n "$MODEL_NAME$MODEL_PATH$MODEL_REVISION$SERVED_MODEL_NAME$QUANTIZATION$GPU_MEMORY_UTILIZATION$MAX_MODEL_LEN" ]; then
  echo -e "${BLUE}═══ Applying CLI overrides ═══${NC}"
  patch_key MODEL_NAME "$MODEL_NAME"
  patch_key MODEL_PATH "$MODEL_PATH"
  patch_key MODEL_REVISION "$MODEL_REVISION"
  patch_key SERVED_MODEL_NAME "$SERVED_MODEL_NAME"
  patch_key QUANTIZATION "$QUANTIZATION"
  patch_key GPU_MEMORY_UTILIZATION "$GPU_MEMORY_UTILIZATION"
  patch_key MAX_MODEL_LEN "$MAX_MODEL_LEN"
fi

# 3) Roll out only vLLM (ConfigMap env is read at pod start)
echo ""
echo -e "${BLUE}═══ Rolling out vLLM only ═══${NC}"
kubectl rollout restart "deployment/$DEPLOYMENT" -n "$NAMESPACE"
echo -e "${GREEN}✓${NC} Restarted deployment/$DEPLOYMENT"
echo "  Waiting for rollout (timeout ${TIMEOUT}s)..."
kubectl rollout status "deployment/$DEPLOYMENT" -n "$NAMESPACE" --timeout="${TIMEOUT}s"
echo -e "${GREEN}✓${NC} Rollout complete"

# 4) Health checks
if [ "$SKIP_HEALTH" = "1" ]; then
  echo -e "${YELLOW}Skipping post-swap health checks (--skip-health)${NC}"
else
  echo ""
  echo -e "${BLUE}═══ Post-swap health checks ═══${NC}"
  EXPECTED=$(kubectl get configmap vllm-config -n "$NAMESPACE" \
    -o jsonpath='{.data.SERVED_MODEL_NAME}' 2>/dev/null || true)
  export EXPECTED_MODEL="$EXPECTED"
  export NAMESPACE TIMEOUT
  if [ "$SKIP_PLEX" = "1" ]; then
    export CHECK_PLEX=0
  fi
  bash "$SCRIPT_DIR/check-model-health.sh"
fi

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Model hot-swap complete (stack components left untouched)     ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Next steps / notes:"
echo "  • Model weights cached on PVC model-storage under MODEL_PATH"
echo "  • Switching back to a previous MODEL_PATH is instant (no re-download)"
echo "  • Free space: list with kubectl exec … -- ls /models ; delete unused dirs carefully"
echo "  • If clients use SERVED_MODEL_NAME, update SWE-agent config if it changed"
echo "  • Docs: ai-dev/MODEL_HOT_SWAP.md"
echo ""
