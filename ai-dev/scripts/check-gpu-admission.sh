#!/bin/bash
# Hard GPU admission gate for AI-dev workloads sharing the GPU with Plex.
#
# Unlike check-plex-health.sh (runtime soft/hard health after deploy), this script
# ENFORCES coexistence rules BEFORE scheduling GPU work.
#
# Exit codes (explicit failure modes when GPU is busy / unsafe):
#   0  - Admission allowed
#   10 - Insufficient free nvidia.com/gpu.shared shares (GPU capacity busy)
#   11 - Requested shares exceed node allocatable capacity
#   20 - Plex not healthy (hard coexistence rule: do not take GPU while Plex down)
#   30 - GPU node missing or NotReady
#   31 - Cannot determine GPU capacity (cluster/API error)
#   40 - Forced override used (warning path only when ALLOW_GPU_OVERRIDE=1 and still fails checks)
#
# Environment:
#   GPU_NODE              Node name (default: homelabai)
#   GPU_SHARES_REQUIRED   Shares needed for this deploy (default: 2, matches vLLM)
#   PLEX_NAMESPACE        Namespace for Plex (default: media)
#   PLEX_LABEL            Label selector for Plex pods (default: app=plex)
#   REQUIRE_PLEX_HEALTHY  If 1 (default), Plex must be Running before GPU admit
#   ALLOW_GPU_OVERRIDE    If 1, print override instructions only; does NOT bypass
#                         unless FORCE_GPU_ADMISSION=1 is also set
#   FORCE_GPU_ADMISSION   If 1 with ALLOW_GPU_OVERRIDE=1, admit despite failures
#                         (operator break-glass; logged explicitly)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

GPU_NODE="${GPU_NODE:-homelabai}"
GPU_SHARES_REQUIRED="${GPU_SHARES_REQUIRED:-2}"
PLEX_NAMESPACE="${PLEX_NAMESPACE:-media}"
PLEX_LABEL="${PLEX_LABEL:-app=plex}"
REQUIRE_PLEX_HEALTHY="${REQUIRE_PLEX_HEALTHY:-1}"
ALLOW_GPU_OVERRIDE="${ALLOW_GPU_OVERRIDE:-0}"
FORCE_GPU_ADMISSION="${FORCE_GPU_ADMISSION:-0}"

FAILURES=0
PRIMARY_EXIT=0

record_failure() {
    local code=$1
    local msg=$2
    echo -e "${RED}✗ ADMISSION DENIED (${code}): ${msg}${NC}"
    FAILURES=$((FAILURES + 1))
    if [ "$PRIMARY_EXIT" -eq 0 ]; then
        PRIMARY_EXIT=$code
    fi
}

echo -e "${BLUE}=== GPU Admission Gate (hard enforce) ===${NC}"
echo "Node:              ${GPU_NODE}"
echo "Shares required:   ${GPU_SHARES_REQUIRED}"
echo "Plex check:        REQUIRE_PLEX_HEALTHY=${REQUIRE_PLEX_HEALTHY}"
echo ""

# --- Cluster connectivity ---
if ! kubectl cluster-info &>/dev/null; then
    record_failure 31 "Cannot connect to Kubernetes cluster"
    echo -e "${RED}Failure mode: cluster API unreachable — cannot prove GPU free for Plex coexistence.${NC}"
    exit 31
fi

# --- Node readiness ---
if ! kubectl get node "$GPU_NODE" &>/dev/null; then
    record_failure 30 "GPU node '${GPU_NODE}' not found"
    echo -e "${RED}Failure mode: GPU node missing — AI GPU pods cannot schedule; Plex node may differ.${NC}"
else
    NODE_READY=$(kubectl get node "$GPU_NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "Unknown")
    if [ "$NODE_READY" != "True" ]; then
        record_failure 30 "GPU node '${GPU_NODE}' Ready=${NODE_READY}"
        echo -e "${RED}Failure mode: GPU node NotReady — refuse new GPU consumers while node is unhealthy.${NC}"
    else
        echo -e "${GREEN}✓ GPU node ${GPU_NODE} is Ready${NC}"
    fi
fi

# --- GPU share capacity (hard scheduler resource) ---
# Prefer Allocatable; fall back to Capacity. Allocated = capacity - free is derived
# from node status allocated resources when available.
ALLOCATABLE=""
if kubectl get node "$GPU_NODE" -o json &>/dev/null; then
    ALLOCATABLE=$(kubectl get node "$GPU_NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu\.shared}' 2>/dev/null || true)
    if [ -z "$ALLOCATABLE" ]; then
        # Some clusters report only via describe / different key
        ALLOCATABLE=$(kubectl get node "$GPU_NODE" -o json | \
            python3 -c "import sys,json; n=json.load(sys.stdin); print(n.get('status',{}).get('allocatable',{}).get('nvidia.com/gpu.shared',''))" 2>/dev/null || true)
    fi
fi

if [ -z "$ALLOCATABLE" ]; then
    # Fallback: parse describe (works without python jsonpath quirks)
    ALLOCATABLE=$(kubectl describe node "$GPU_NODE" 2>/dev/null | \
        awk '/Allocatable:/,/System Info:/ { if ($1 ~ /nvidia.com\/gpu.shared/) print $2 }' | head -1)
fi

if [ -z "$ALLOCATABLE" ]; then
    record_failure 31 "Could not read nvidia.com/gpu.shared allocatable on ${GPU_NODE}"
    echo -e "${RED}Failure mode: unknown GPU capacity — admission cannot hard-enforce share limits.${NC}"
    echo "  Hint: kubectl describe node ${GPU_NODE} | grep -A20 Allocatable"
else
    echo -e "${GREEN}✓ Allocatable GPU shares: ${ALLOCATABLE}${NC}"
    if [ "$GPU_SHARES_REQUIRED" -gt "$ALLOCATABLE" ] 2>/dev/null; then
        record_failure 11 "Required shares (${GPU_SHARES_REQUIRED}) exceed allocatable (${ALLOCATABLE})"
        echo -e "${RED}Failure mode: request larger than node GPU capacity — reduce GPU_SHARES_REQUIRED or free capacity.${NC}"
    fi
fi

# Free shares: allocatable - currently allocated requests for the resource.
# Prefer describe "Allocated resources" block (stable across k3s versions).
ALLOCATED=$(kubectl describe node "$GPU_NODE" 2>/dev/null | \
    awk '/Allocated resources:/,/Events:/ {
        if ($0 ~ /nvidia.com\/gpu.shared/) {
            for (i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) { print $i; exit }
        }
    }')

if [ -z "$ALLOCATED" ]; then
    # Secondary parse: "nvidia.com/gpu.shared  4 (50%)  4 (50%)" style lines
    ALLOCATED=$(kubectl describe node "$GPU_NODE" 2>/dev/null | \
        sed -n '/Allocated resources:/,/Events:/p' | \
        grep -E 'nvidia\.com/gpu\.shared' | \
        awk '{print $2}' | head -1 || true)
fi

if [ -n "$ALLOCATABLE" ] && [ -n "$ALLOCATED" ] && [[ "$ALLOCATED" =~ ^[0-9]+$ ]]; then
    FREE=$((ALLOCATABLE - ALLOCATED))
    if [ "$FREE" -lt 0 ]; then
        FREE=0
    fi
    echo "  Allocated GPU shares: ${ALLOCATED}"
    echo "  Free GPU shares:      ${FREE}"
    if [ "$FREE" -lt "$GPU_SHARES_REQUIRED" ]; then
        record_failure 10 "Only ${FREE} free GPU share(s); need ${GPU_SHARES_REQUIRED}"
        echo -e "${RED}Failure mode: GPU busy — insufficient free time-sliced shares for coexistence.${NC}"
        echo "  Actions:"
        echo "    1. Scale down non-critical GPU consumers (not Plex)"
        echo "    2. Wait for AI jobs to finish"
        echo "    3. Lower vLLM shares only if documented and safe"
        echo "    4. Emergency: kubectl scale deployment -n ai-dev vllm-server --replicas=0"
    else
        echo -e "${GREEN}✓ Sufficient free GPU shares (${FREE} >= ${GPU_SHARES_REQUIRED})${NC}"
    fi
elif [ -n "$ALLOCATABLE" ]; then
    echo -e "${YELLOW}⚠ Could not parse allocated GPU shares; capacity known but free count uncertain${NC}"
    echo "  Soft check only for free shares; hard checks (node, capacity, Plex) still apply."
    echo "  Inspect: kubectl describe node ${GPU_NODE} | sed -n '/Allocated resources:/,/Events:/p'"
fi

# --- Plex hard coexistence rule ---
if [ "$REQUIRE_PLEX_HEALTHY" = "1" ]; then
    if kubectl get pods -n "$PLEX_NAMESPACE" -l "$PLEX_LABEL" --no-headers 2>/dev/null | grep -q .; then
        PLEX_PHASE=$(kubectl get pods -n "$PLEX_NAMESPACE" -l "$PLEX_LABEL" \
            -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "Unknown")
        if [ "$PLEX_PHASE" = "Running" ]; then
            echo -e "${GREEN}✓ Plex is Running in ${PLEX_NAMESPACE}${NC}"
        else
            record_failure 20 "Plex phase is '${PLEX_PHASE}' (expected Running)"
            echo -e "${RED}Failure mode: Plex unhealthy — refuse new GPU load until media is stable.${NC}"
        fi
    else
        # If Plex is not installed in this environment, do not block (dev/CI).
        echo -e "${YELLOW}⚠ No Plex pods found (${PLEX_NAMESPACE}/${PLEX_LABEL}) — skipping hard Plex gate${NC}"
        echo "  (Treat as soft: coexistence rules still apply when Plex is present.)"
    fi
else
    echo -e "${YELLOW}⚠ REQUIRE_PLEX_HEALTHY=0 — Plex hard check disabled${NC}"
fi

echo ""
echo -e "${BLUE}=== Admission Summary ===${NC}"

if [ "$FAILURES" -eq 0 ]; then
    echo -e "${GREEN}✓ GPU admission ALLOWED (hard constraints satisfied)${NC}"
    exit 0
fi

echo -e "${RED}✗ GPU admission DENIED (${FAILURES} hard constraint failure(s))${NC}"
echo "Primary exit code: ${PRIMARY_EXIT}"
echo ""
echo "Hard constraints (must pass):"
echo "  - GPU node Ready"
echo "  - nvidia.com/gpu.shared capacity sufficient for request"
echo "  - Free shares >= GPU_SHARES_REQUIRED (when measurable)"
echo "  - Plex Running when present and REQUIRE_PLEX_HEALTHY=1"
echo ""
echo "Soft constraints (not enforced here — see GPU_CONSTRAINTS.md):"
echo "  - VRAM headroom / GPU_MEMORY_UTILIZATION"
echo "  - Peak Plex transcoding windows"
echo "  - Subjective transcode quality"
echo ""

if [ "$FORCE_GPU_ADMISSION" = "1" ] && [ "$ALLOW_GPU_OVERRIDE" = "1" ]; then
    echo -e "${YELLOW}⚠ FORCE_GPU_ADMISSION=1 with ALLOW_GPU_OVERRIDE=1 — break-glass admit${NC}"
    echo -e "${YELLOW}  Operator override: hard constraints failed but deploy may continue.${NC}"
    exit 0
fi

if [ "$ALLOW_GPU_OVERRIDE" = "1" ]; then
    echo "Break-glass (explicit only):"
    echo "  ALLOW_GPU_OVERRIDE=1 FORCE_GPU_ADMISSION=1 $0"
    echo "  (Both flags required; silent override is intentionally unsupported.)"
fi

exit "$PRIMARY_EXIT"
