#!/bin/bash
# Switch between local GPU (vLLM on homelabai) and non-GPU fallback (remote OpenAI-compatible).
# Used when the GPU node is drained or media (Plex) needs exclusive priority.
#
# Usage:
#   bash ai-dev/scripts/llm-mode.sh status
#   bash ai-dev/scripts/llm-mode.sh gpu [--reason "text"]
#   bash ai-dev/scripts/llm-mode.sh fallback [--reason "text"] [--url URL] [--model NAME]
#   bash ai-dev/scripts/llm-mode.sh set-fallback-url URL [--model NAME]
#
# Env overrides (optional):
#   NAMESPACE, FALLBACK_BASE_URL, FALLBACK_MODEL, REASON
#
# See: ai-dev/GPU_FALLBACK.md

set -euo pipefail

NAMESPACE="${NAMESPACE:-ai-dev}"
CONFIGMAP="llm-endpoint-config"
VLLM_DEPLOY="vllm-server"
SWE_DEPLOY="swe-agent-server"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

usage() {
    cat <<'EOF'
Usage: llm-mode.sh <command> [options]

Commands:
  status              Show active LLM mode and GPU path status
  gpu                 Enable local GPU path (scale up vLLM, point clients at vLLM)
  fallback            Disable GPU path (optional scale-down vLLM, use remote base URL)
  set-fallback-url    Persist FALLBACK_BASE_URL (and optional model) without switching mode

Options:
  --reason TEXT       Recorded on the ConfigMap (default: CLI / env REASON)
  --url URL           Fallback base URL (fallback / set-fallback-url)
  --model NAME        Fallback model name
  -h, --help          Show this help

Examples:
  # Free GPU for Plex and use a remote OpenAI-compatible API
  bash ai-dev/scripts/llm-mode.sh fallback --reason "Plex priority" \
    --url "https://api.openai.com/v1" --model "gpt-4o-mini"

  # Restore local vLLM after media load drops
  bash ai-dev/scripts/llm-mode.sh gpu --reason "homelabai available"

  # Inspect current mode
  bash ai-dev/scripts/llm-mode.sh status
EOF
}

require_kubectl() {
    if ! command -v kubectl &>/dev/null; then
        echo -e "${RED}Error: kubectl not found in PATH${NC}" >&2
        exit 1
    fi
}

cm_get() {
    local key="$1"
    kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" -o "jsonpath={.data.$key}" 2>/dev/null || true
}

cm_exists() {
    kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" &>/dev/null
}

iso_now() {
    date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%SZ"
}

# Patch multiple keys on llm-endpoint-config (key=value pairs) without dropping others
cm_patch_keys() {
    if ! cm_exists; then
        echo -e "${RED}Error: ConfigMap $CONFIGMAP not found in namespace $NAMESPACE${NC}" >&2
        echo "Apply manifests first: kubectl apply -f ai-dev/vllm/llm-endpoint-configmap.yaml" >&2
        exit 1
    fi

    # Build a JSON merge-patch for .data only (preserves unspecified keys)
    local pair key value
    local json_parts=()
    for pair in "$@"; do
        key="${pair%%=*}"
        value="${pair#*=}"
        # Escape for JSON string values
        value="${value//\\/\\\\}"
        value="${value//\"/\\\"}"
        value="${value//$'\n'/\\n}"
        json_parts+=("\"${key}\": \"${value}\"")
    done

    local data_json
    data_json=$(IFS=,; echo "${json_parts[*]}")
    local patch
    patch="{\"data\": {${data_json}}}"

    kubectl -n "$NAMESPACE" patch configmap "$CONFIGMAP" --type merge -p "$patch" >/dev/null
}

scale_vllm() {
    local replicas="$1"
    if kubectl -n "$NAMESPACE" get deployment "$VLLM_DEPLOY" &>/dev/null; then
        kubectl -n "$NAMESPACE" scale deployment "$VLLM_DEPLOY" --replicas="$replicas"
        echo -e "${BLUE}vLLM deployment ${VLLM_DEPLOY} scaled to ${replicas}${NC}"
    else
        echo -e "${YELLOW}Warning: deployment ${VLLM_DEPLOY} not found; skip scale${NC}"
    fi
}

restart_swe_agent() {
    if kubectl -n "$NAMESPACE" get deployment "$SWE_DEPLOY" &>/dev/null; then
        kubectl -n "$NAMESPACE" rollout restart deployment "$SWE_DEPLOY"
        echo -e "${BLUE}Restarted ${SWE_DEPLOY} to pick up ACTIVE_BASE_URL / ACTIVE_MODEL${NC}"
    else
        echo -e "${YELLOW}Warning: deployment ${SWE_DEPLOY} not found; skip restart${NC}"
    fi
}

cmd_status() {
    require_kubectl

    echo -e "${BLUE}=== LLM endpoint mode status ===${NC}"
    echo "Namespace: $NAMESPACE"
    echo ""

    if ! cm_exists; then
        echo -e "${RED}ConfigMap ${CONFIGMAP} is missing${NC}"
        echo "GPU path status: UNKNOWN (apply llm-endpoint-configmap.yaml)"
        exit 1
    fi

    local mode active_url active_model status_msg gpu_url fallback_url reason changed scale_flag
    mode="$(cm_get LLM_MODE)"
    active_url="$(cm_get ACTIVE_BASE_URL)"
    active_model="$(cm_get ACTIVE_MODEL)"
    status_msg="$(cm_get STATUS_MESSAGE)"
    gpu_url="$(cm_get GPU_BASE_URL)"
    fallback_url="$(cm_get FALLBACK_BASE_URL)"
    reason="$(cm_get LAST_REASON)"
    changed="$(cm_get LAST_CHANGED_AT)"
    scale_flag="$(cm_get SCALE_DOWN_VLLM_ON_FALLBACK)"

    echo "LLM_MODE:          ${mode:-<unset>}"
    echo "STATUS_MESSAGE:    ${status_msg:-<unset>}"
    echo "ACTIVE_BASE_URL:   ${active_url:-<unset>}"
    echo "ACTIVE_MODEL:      ${active_model:-<unset>}"
    echo "GPU_BASE_URL:      ${gpu_url:-<unset>}"
    echo "FALLBACK_BASE_URL: ${fallback_url:-<unset>}"
    echo "LAST_REASON:       ${reason:-<unset>}"
    echo "LAST_CHANGED_AT:   ${changed:-<unset>}"
    echo "SCALE_DOWN_VLLM:   ${scale_flag:-true}"
    echo ""

    if [ "${mode}" = "fallback" ]; then
        echo -e "${YELLOW}GPU path: DISABLED${NC}"
        echo "  Clients use the remote/CPU OpenAI-compatible endpoint."
        echo "  Local vLLM on homelabai should be scaled down (Plex / media priority)."
    elif [ "${mode}" = "gpu" ]; then
        echo -e "${GREEN}GPU path: ENABLED${NC}"
        echo "  Clients use local vLLM (${gpu_url})."
    else
        echo -e "${YELLOW}GPU path: UNKNOWN (LLM_MODE=${mode})${NC}"
    fi

    echo ""
    echo "Cluster view:"
    if kubectl -n "$NAMESPACE" get deployment "$VLLM_DEPLOY" &>/dev/null; then
        local ready desired
        ready="$(kubectl -n "$NAMESPACE" get deployment "$VLLM_DEPLOY" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
        desired="$(kubectl -n "$NAMESPACE" get deployment "$VLLM_DEPLOY" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
        ready="${ready:-0}"
        desired="${desired:-0}"
        echo "  vLLM replicas: ${ready}/${desired} ready"
        if [ "$mode" = "fallback" ] && [ "${desired}" != "0" ]; then
            echo -e "  ${YELLOW}Note: mode is fallback but vLLM still has replicas > 0${NC}"
        fi
        if [ "$mode" = "gpu" ] && [ "${desired}" = "0" ]; then
            echo -e "  ${YELLOW}Note: mode is gpu but vLLM replicas is 0${NC}"
        fi
    else
        echo "  vLLM deployment: not found"
    fi

    if kubectl get node homelabai &>/dev/null; then
        local unsched
        unsched="$(kubectl get node homelabai -o jsonpath='{.spec.unschedulable}' 2>/dev/null || true)"
        if [ "$unsched" = "true" ]; then
            echo -e "  homelabai: ${YELLOW}cordoned/unschedulable (drained or draining)${NC}"
        else
            echo -e "  homelabai: ${GREEN}schedulable${NC}"
        fi
    else
        echo "  homelabai: node not visible from this kube context"
    fi
}

cmd_set_fallback_url() {
    require_kubectl
    local url="${1:-}"
    local model="${2:-}"

    if [ -z "$url" ]; then
        echo -e "${RED}Error: --url is required for set-fallback-url${NC}" >&2
        exit 1
    fi

    local patches=("FALLBACK_BASE_URL=${url}")
    if [ -n "$model" ]; then
        patches+=("FALLBACK_MODEL=${model}")
    fi
    cm_patch_keys "${patches[@]}"
    echo -e "${GREEN}Updated FALLBACK_BASE_URL=${url}${NC}"
    [ -n "$model" ] && echo -e "${GREEN}Updated FALLBACK_MODEL=${model}${NC}"
}

cmd_gpu() {
    require_kubectl
    local reason="${1:-restored GPU path}"

    local gpu_url gpu_model
    gpu_url="$(cm_get GPU_BASE_URL)"
    gpu_model="$(cm_get GPU_MODEL)"
    gpu_url="${gpu_url:-http://vllm-server.ai-dev.svc.cluster.local:8000/v1}"
    gpu_model="${gpu_model:-deepseek-coder-6.7b-instruct}"

    local status_msg
    status_msg="GPU path ENABLED — local vLLM on homelabai (${gpu_url})"

    cm_patch_keys \
        "LLM_MODE=gpu" \
        "ACTIVE_BASE_URL=${gpu_url}" \
        "ACTIVE_MODEL=${gpu_model}" \
        "STATUS_MESSAGE=${status_msg}" \
        "LAST_REASON=${reason}" \
        "LAST_CHANGED_AT=$(iso_now)"

    scale_vllm 1
    restart_swe_agent

    echo ""
    echo -e "${GREEN}${status_msg}${NC}"
    echo "Reason: ${reason}"
    cmd_status
}

cmd_fallback() {
    require_kubectl
    local reason="${1:-non-GPU fallback}"
    local url_override="${2:-}"
    local model_override="${3:-}"

    local fallback_url fallback_model scale_flag
    fallback_url="${url_override:-${FALLBACK_BASE_URL:-$(cm_get FALLBACK_BASE_URL)}}"
    fallback_model="${model_override:-${FALLBACK_MODEL:-$(cm_get FALLBACK_MODEL)}}"
    scale_flag="$(cm_get SCALE_DOWN_VLLM_ON_FALLBACK)"
    scale_flag="${scale_flag:-true}"

    if [ -z "$fallback_url" ]; then
        echo -e "${RED}Error: FALLBACK_BASE_URL is empty. Set it with --url or set-fallback-url${NC}" >&2
        exit 1
    fi
    fallback_model="${fallback_model:-gpt-4o-mini}"

    local status_msg
    status_msg="GPU path DISABLED — vLLM scaled for media priority; clients use remote endpoint (${fallback_url}). Model: ${fallback_model}"

    local patches=(
        "LLM_MODE=fallback"
        "ACTIVE_BASE_URL=${fallback_url}"
        "ACTIVE_MODEL=${fallback_model}"
        "FALLBACK_BASE_URL=${fallback_url}"
        "FALLBACK_MODEL=${fallback_model}"
        "STATUS_MESSAGE=${status_msg}"
        "LAST_REASON=${reason}"
        "LAST_CHANGED_AT=$(iso_now)"
    )
    cm_patch_keys "${patches[@]}"

    if [ "$scale_flag" = "true" ] || [ "$scale_flag" = "True" ] || [ "$scale_flag" = "1" ]; then
        scale_vllm 0
    else
        echo -e "${YELLOW}SCALE_DOWN_VLLM_ON_FALLBACK is false; leaving vLLM replicas unchanged${NC}"
    fi
    restart_swe_agent

    echo ""
    echo -e "${YELLOW}${status_msg}${NC}"
    echo "Reason: ${reason}"
    echo "Tip: set OPENAI_API_KEY (or swe-agent-secrets openai-api-key) for authenticated remote APIs."
    cmd_status
}

main() {
    local cmd="${1:-}"
    shift || true

    local reason="${REASON:-}"
    local url=""
    local model=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --reason)
                reason="${2:-}"
                shift 2 || true
                ;;
            --url)
                url="${2:-}"
                shift 2 || true
                ;;
            --model)
                model="${2:-}"
                shift 2 || true
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                # positional leftovers (e.g. URL for set-fallback-url)
                if [ -z "$url" ] && [[ "$1" == http* ]]; then
                    url="$1"
                    shift
                else
                    echo -e "${RED}Unknown option: $1${NC}" >&2
                    usage
                    exit 1
                fi
                ;;
        esac
    done

    case "$cmd" in
        status)
            cmd_status
            ;;
        gpu|local)
            cmd_gpu "${reason:-restored GPU path}"
            ;;
        fallback|remote|drain)
            cmd_fallback "${reason:-non-GPU fallback (homelabai drained / Plex priority)}" "$url" "$model"
            ;;
        set-fallback-url)
            cmd_set_fallback_url "$url" "$model"
            ;;
        ""|-h|--help)
            usage
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown command: $cmd${NC}" >&2
            usage
            exit 1
            ;;
    esac
}

main "$@"
