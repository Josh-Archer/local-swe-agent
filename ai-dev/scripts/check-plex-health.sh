#!/bin/bash
# Check Plex health after GPU operations
# CRITICAL: Always run this after deploying GPU workloads
#
# Hard failures (non-zero exit) — deploy scripts must not treat as advisory:
#   1  - Plex pod not Running / web UI unreachable
# Soft warnings (exit 0 still possible if core checks pass):
#   - log error count, GPU device visibility
#
# See GPU_CONSTRAINTS.md for hard vs soft coexistence rules.
# Pre-deploy capacity gate: check-gpu-admission.sh

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

HARD_FAIL=0

echo -e "${BLUE}=== Plex Health Check ===${NC}"
echo ""

# Check if Plex pods are running
echo "Checking Plex pod status..."
if kubectl get pods -n media -l app=plex &>/dev/null; then
    POD_STATUS=$(kubectl get pods -n media -l app=plex -o jsonpath='{.items[0].status.phase}')
    POD_NAME=$(kubectl get pods -n media -l app=plex -o jsonpath='{.items[0].metadata.name}')

    if [ "$POD_STATUS" = "Running" ]; then
        echo -e "${GREEN}✓ Plex pod is Running${NC}"
    else
        echo -e "${RED}✗ HARD FAIL: Plex pod status: $POD_STATUS${NC}"
        echo -e "${RED}Failure mode: Plex not Running after/with GPU workload — coexistence violated${NC}"
        kubectl describe pod -n media -l app=plex | tail -20
        HARD_FAIL=1
    fi
else
    echo -e "${YELLOW}Warning: Could not find Plex pods in 'media' namespace${NC}"
    echo "  Checking other namespaces..."
    kubectl get pods --all-namespaces -l app=plex || true
    echo "  Soft: no Plex in cluster (dev/CI) — core pod check skipped"
fi

# Check Plex logs for errors (soft — log noise is not always fatal)
if [ -n "${POD_NAME:-}" ]; then
    echo ""
    echo "Checking Plex logs for recent errors..."
    ERROR_COUNT=$(kubectl logs -n media "$POD_NAME" --tail=100 2>/dev/null | grep -i "error" | wc -l || echo 0)
    if [ "$ERROR_COUNT" -gt 0 ]; then
        echo -e "${YELLOW}Warning (soft): Found $ERROR_COUNT error messages in logs${NC}"
        echo "Recent errors:"
        kubectl logs -n media "$POD_NAME" --tail=100 2>/dev/null | grep -i "error" | tail -5 || true
    else
        echo -e "${GREEN}✓ No recent errors in Plex logs${NC}"
    fi
fi

# Check GPU usage
echo ""
echo "Checking GPU allocation..."
kubectl describe node homelabai | grep -A 10 "Allocated resources:" || echo "Could not get GPU info"

# Try to access Plex web UI
echo ""
echo "Checking Plex web UI accessibility..."
if command -v curl &>/dev/null; then
    if curl -I -s -k https://plex.archer.casa --max-time 10 | head -1 | grep -q "200\|301\|302"; then
        echo -e "${GREEN}✓ Plex web UI is accessible${NC}"
    else
        echo -e "${RED}✗ HARD FAIL: Plex web UI is not accessible${NC}"
        echo -e "${RED}Failure mode: media frontend down — treat as GPU coexistence incident${NC}"
        HARD_FAIL=1
    fi
else
    echo -e "${YELLOW}Warning: curl not found, skipping web UI check${NC}"
fi

# Check GPU device visibility inside Plex pod (soft if exec fails)
if [ -n "${POD_NAME:-}" ]; then
    echo ""
    echo "Checking GPU device visibility in Plex pod..."
    if kubectl exec -n media "$POD_NAME" -- ls -l /dev/nvidia* &>/dev/null; then
        echo -e "${GREEN}✓ GPU devices are visible in Plex pod${NC}"
        kubectl exec -n media "$POD_NAME" -- ls -l /dev/nvidia* || true
    else
        echo -e "${YELLOW}Warning (soft): GPU devices may not be visible in Plex pod${NC}"
    fi
fi

# Summary
echo ""
echo -e "${BLUE}=== Health Check Summary ===${NC}"
if [ "$HARD_FAIL" -ne 0 ]; then
    echo -e "${RED}✗ Plex health HARD FAIL — deploy must not continue (see GPU_CONSTRAINTS.md)${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Plex appears healthy${NC}"
echo ""
echo "If transcoding issues occur (soft / operational):"
echo "  1. Check GPU time-slicing configuration"
echo "  2. Monitor GPU memory: kubectl exec -n ai-dev -l app=vllm -- nvidia-smi"
echo "  3. Reduce vLLM GPU_MEMORY_UTILIZATION if needed"
echo "  4. Run pre-deploy gate: bash scripts/check-gpu-admission.sh"
echo "  5. Consider scheduling vLLM training during off-peak hours"
echo ""
