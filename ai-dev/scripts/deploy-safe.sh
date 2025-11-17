#!/bin/bash
# Safe Incremental Deployment Script for AI-Dev System
# Follows SAFE_DEPLOYMENT_GUIDE.md with validation gates

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_DEV_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Validation mode - set to 1 to pause at each gate
INTERACTIVE=${INTERACTIVE:-1}

echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║     AI-Dev Safe Deployment Script - Incremental Rollout       ║${NC}"
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo ""
echo -e "${YELLOW}⚠️  This script deploys components incrementally with validation gates${NC}"
echo -e "${YELLOW}⚠️  Plex health is checked after GPU workload deployment${NC}"
echo -e "${YELLOW}⚠️  Press Ctrl+C at any time to abort${NC}"
echo ""

# Check kubectl connection
if ! kubectl cluster-info &>/dev/null; then
    echo -e "${RED}✗ Error: Cannot connect to Kubernetes cluster${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Connected to Kubernetes cluster${NC}"
echo ""

# Function to prompt for continuation
prompt_continue() {
    local phase=$1
    local message=$2

    if [ "$INTERACTIVE" = "1" ]; then
        echo ""
        echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
        echo -e "${CYAN}Validation Gate: ${phase}${NC}"
        echo -e "${CYAN}${message}${NC}"
        echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
        read -p "Continue to next phase? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo -e "${YELLOW}Deployment paused by user${NC}"
            exit 0
        fi
    else
        echo -e "${GREEN}✓ ${phase} complete${NC}"
    fi
}

# Function to validate pods
wait_for_pod() {
    local namespace=$1
    local label=$2
    local timeout=${3:-300}

    echo "  Waiting for pod with label ${label} to be ready (timeout: ${timeout}s)..."
    if kubectl wait --for=condition=ready pod -l "$label" -n "$namespace" --timeout="${timeout}s" 2>/dev/null; then
        echo -e "  ${GREEN}✓ Pod ready${NC}"
        return 0
    else
        echo -e "  ${RED}✗ Pod not ready within timeout${NC}"
        return 1
    fi
}

# Pre-deployment checks
echo -e "${BLUE}═══ Pre-Deployment Checks ═══${NC}"
echo ""

echo -n "Checking GPU node availability... "
if kubectl get node homelabai &>/dev/null; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗ Node homelabai not found${NC}"
    exit 1
fi

echo -n "Checking GPU resources... "
GPU_AVAILABLE=$(kubectl describe node homelabai | grep "nvidia.com/gpu.shared" | grep Allocatable | awk '{print $2}')
if [ "$GPU_AVAILABLE" = "8" ]; then
    echo -e "${GREEN}✓ 8 GPU shares available${NC}"
else
    echo -e "${YELLOW}⚠ GPU shares: ${GPU_AVAILABLE} (expected 8)${NC}"
fi

echo -n "Checking Plex baseline health... "
if kubectl get pods -n media -l app=plex &>/dev/null; then
    PLEX_STATUS=$(kubectl get pods -n media -l app=plex -o jsonpath='{.items[0].status.phase}')
    if [ "$PLEX_STATUS" = "Running" ]; then
        echo -e "${GREEN}✓ Plex is Running${NC}"
    else
        echo -e "${YELLOW}⚠ Plex status: ${PLEX_STATUS}${NC}"
    fi
else
    echo -e "${YELLOW}⚠ Plex not found (may be okay)${NC}"
fi

echo ""

# Phase 1: Namespace + Storage
echo -e "${BLUE}═══ Phase 1: Namespace + Storage ═══${NC}"
echo -e "${CYAN}Creating namespace and PVCs (no pods, no GPU impact)${NC}"
echo ""

echo "Deploying namespace..."
kubectl apply -f "$AI_DEV_DIR/namespace/namespace.yaml"

echo "Deploying PVCs..."
kubectl apply -f "$AI_DEV_DIR/storage/pvcs.yaml"

echo ""
echo "Waiting for PVCs to bind..."
sleep 5

echo "PVC Status:"
kubectl get pvc -n ai-dev

echo ""
PVC_BOUND=$(kubectl get pvc -n ai-dev -o jsonpath='{.items[*].status.phase}' | tr ' ' '\n' | grep -c "Bound" || true)
PVC_TOTAL=$(kubectl get pvc -n ai-dev --no-headers | wc -l)

if [ "$PVC_BOUND" = "$PVC_TOTAL" ]; then
    echo -e "${GREEN}✓ All PVCs bound (${PVC_BOUND}/${PVC_TOTAL})${NC}"
else
    echo -e "${YELLOW}⚠ PVCs bound: ${PVC_BOUND}/${PVC_TOTAL}${NC}"
    echo "Check Longhorn dashboard if PVCs are not binding"
fi

prompt_continue "Phase 1" "Namespace and storage created. All PVCs should be Bound."

# Phase 2: Qdrant
echo ""
echo -e "${BLUE}═══ Phase 2: Qdrant Vector Database ═══${NC}"
echo -e "${CYAN}Deploying Qdrant (no GPU, low risk)${NC}"
echo ""

kubectl apply -f "$AI_DEV_DIR/qdrant/qdrant-deployment.yaml"

if wait_for_pod "ai-dev" "app=qdrant" 300; then
    echo ""
    echo "Testing Qdrant API..."
    if kubectl exec -n ai-dev -l app=qdrant -- curl -s localhost:6333 | grep -q "qdrant"; then
        echo -e "${GREEN}✓ Qdrant API responding${NC}"
    else
        echo -e "${YELLOW}⚠ Qdrant API may not be responding correctly${NC}"
    fi
else
    echo -e "${RED}✗ Qdrant pod failed to start${NC}"
    exit 1
fi

prompt_continue "Phase 2" "Qdrant deployed and responding."

# Phase 3: vLLM (CRITICAL - GPU WORKLOAD)
echo ""
echo -e "${RED}═══ Phase 3: vLLM Inference Server (GPU WORKLOAD) ═══${NC}"
echo -e "${YELLOW}⚠️  CRITICAL: This will allocate GPU resources${NC}"
echo -e "${YELLOW}⚠️  Plex health will be checked immediately after${NC}"
echo ""

if [ "$INTERACTIVE" = "1" ]; then
    echo -e "${CYAN}Pre-GPU Deployment Check:${NC}"
    echo "- Current GPU allocation will be shown"
    echo "- Plex health will be verified"
    echo "- You can abort now if needed"
    echo ""
    read -p "Deploy vLLM with GPU? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}Deployment aborted before GPU allocation${NC}"
        echo "Namespace ai-dev exists with Qdrant deployed"
        echo "To cleanup: kubectl delete namespace ai-dev"
        exit 0
    fi
fi

echo "Current GPU allocation:"
kubectl describe node homelabai | grep -A 5 "Allocated resources:" | grep nvidia || echo "No GPU resources allocated yet"
echo ""

echo "Deploying vLLM ConfigMap..."
kubectl apply -f "$AI_DEV_DIR/vllm/vllm-configmap.yaml"

echo "Deploying vLLM server..."
kubectl apply -f "$AI_DEV_DIR/vllm/vllm-deployment.yaml"

echo ""
echo -e "${CYAN}Waiting for vLLM to start (this may take 5-10 minutes for model download)...${NC}"
echo "You can monitor logs in another terminal:"
echo "  kubectl logs -n ai-dev -l app=vllm -f"
echo ""

# Wait for vLLM with longer timeout (model download)
if wait_for_pod "ai-dev" "app=vllm" 600; then
    echo -e "${GREEN}✓ vLLM pod is running${NC}"

    echo ""
    echo "Checking GPU allocation in vLLM pod..."
    if kubectl exec -n ai-dev -l app=vllm -- nvidia-smi &>/dev/null; then
        echo -e "${GREEN}✓ GPU accessible in vLLM pod${NC}"
        kubectl exec -n ai-dev -l app=vllm -- nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
    else
        echo -e "${RED}✗ GPU not accessible in vLLM pod${NC}"
        exit 1
    fi

    echo ""
    echo "Testing vLLM health endpoint..."
    sleep 10  # Give server time to fully start
    if kubectl exec -n ai-dev -l app=vllm -- curl -s http://localhost:8000/health | grep -q "ok\|healthy"; then
        echo -e "${GREEN}✓ vLLM API responding${NC}"
    else
        echo -e "${YELLOW}⚠ vLLM health check inconclusive${NC}"
    fi
else
    echo -e "${RED}✗ vLLM pod failed to start${NC}"
    echo "Check logs: kubectl logs -n ai-dev -l app=vllm"
    exit 1
fi

# CRITICAL: Plex Health Check
echo ""
echo -e "${RED}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║          CRITICAL: Plex Health Check                          ║${NC}"
echo -e "${RED}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

if [ -f "$SCRIPT_DIR/check-plex-health.sh" ]; then
    if bash "$SCRIPT_DIR/check-plex-health.sh"; then
        echo -e "${GREEN}✓✓✓ Plex health check PASSED ✓✓✓${NC}"
    else
        echo -e "${RED}✗✗✗ Plex health check FAILED ✗✗✗${NC}"
        echo ""
        echo -e "${YELLOW}EMERGENCY ROLLBACK OPTIONS:${NC}"
        echo "1. Scale down vLLM: kubectl scale deployment -n ai-dev vllm-server --replicas=0"
        echo "2. Delete vLLM: kubectl delete deployment -n ai-dev vllm-server"
        echo "3. Full rollback: kubectl delete namespace ai-dev"
        echo ""
        read -p "Continue anyway? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo -e "${YELLOW}Deployment aborted due to Plex health check failure${NC}"
            exit 1
        fi
    fi
else
    echo -e "${YELLOW}⚠ Plex health check script not found${NC}"
    echo "Manually verify Plex is still working!"
    echo ""
    read -p "Continue? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

prompt_continue "Phase 3" "vLLM deployed with GPU. Plex health verified."

# Phase 4: Code Indexer
echo ""
echo -e "${BLUE}═══ Phase 4: Code Indexer ═══${NC}"
echo -e "${CYAN}Deploying code indexer CronJob (no GPU)${NC}"
echo ""

kubectl apply -f "$AI_DEV_DIR/code-indexer/configmap.yaml"
kubectl apply -f "$AI_DEV_DIR/code-indexer/cronjob.yaml"

echo -e "${GREEN}✓ Code indexer CronJob created${NC}"
echo "CronJob will run daily at 2 AM"
echo ""
echo "To trigger manual indexing:"
echo "  kubectl create job --from=cronjob/code-indexer manual-index -n ai-dev"

prompt_continue "Phase 4" "Code indexer deployed. Trigger manual job if desired."

# Phase 5: SWE-agent (Optional)
echo ""
echo -e "${BLUE}═══ Phase 5: SWE-agent (Optional) ═══${NC}"
echo -e "${CYAN}Deploy SWE-agent for autonomous issue resolution${NC}"
echo ""

read -p "Deploy SWE-agent? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Check if secret exists
    if kubectl get secret -n ai-dev swe-agent-secrets &>/dev/null; then
        echo -e "${GREEN}✓ SWE-agent secret already exists${NC}"
    else
        echo -e "${YELLOW}⚠ SWE-agent secret not found${NC}"
        echo "Create secret first:"
        echo "  kubectl create secret generic swe-agent-secrets \\"
        echo "    --from-literal=github-token=ghp_YourToken \\"
        echo "    -n ai-dev"
        echo ""
        read -p "Skip SWE-agent deployment? (Y/n): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            echo "Skipping SWE-agent deployment"
        else
            exit 1
        fi
    fi

    if kubectl get secret -n ai-dev swe-agent-secrets &>/dev/null; then
        kubectl apply -f "$AI_DEV_DIR/swe-agent/configmap.yaml"
        kubectl apply -f "$AI_DEV_DIR/swe-agent/deployment.yaml"

        echo -e "${GREEN}✓ SWE-agent deployed${NC}"
    fi
else
    echo "Skipping SWE-agent deployment"
fi

# Phase 6: Ingress
echo ""
echo -e "${BLUE}═══ Phase 6: Ingress (External Access) ═══${NC}"
echo -e "${CYAN}Deploy Traefik IngressRoute for external API access${NC}"
echo ""

read -p "Deploy ingress? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    kubectl apply -f "$AI_DEV_DIR/ingress/ingressroute.yaml"

    echo -e "${GREEN}✓ Ingress deployed${NC}"
    echo ""
    echo "IngressRoute created for: code-llm.archer.casa"
    echo "Update DNS to point to your cluster IP"
    echo "Test: curl https://code-llm.archer.casa/health"
else
    echo "Skipping ingress deployment"
    echo "Access vLLM via port-forward:"
    echo "  kubectl port-forward -n ai-dev svc/vllm-server 8000:8000"
fi

# Final validation
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              Deployment Complete!                              ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

echo -e "${BLUE}═══ Final System Status ═══${NC}"
echo ""

echo "All pods in ai-dev namespace:"
kubectl get pods -n ai-dev

echo ""
echo "GPU allocation:"
kubectl describe node homelabai | grep -A 10 "Allocated resources:" | grep nvidia

echo ""
echo -e "${CYAN}═══ Next Steps ═══${NC}"
echo ""
echo "1. Run comprehensive tests:"
echo "   kubectl port-forward -n ai-dev svc/vllm-server 8000:8000 &"
echo "   python3 $SCRIPT_DIR/test-vllm-api.py --url http://localhost:8000"
echo ""
echo "2. Configure your IDE (Cline/Claude Code):"
echo "   API URL: http://localhost:8000/v1"
echo "   Or: https://code-llm.archer.casa/v1 (if ingress deployed)"
echo ""
echo "3. Trigger code indexing (if desired):"
echo "   kubectl create job --from=cronjob/code-indexer manual-index -n ai-dev"
echo ""
echo "4. Monitor for 24 hours:"
echo "   - Check Plex transcoding works"
echo "   - Watch GPU memory: kubectl exec -n ai-dev -l app=vllm -- nvidia-smi"
echo "   - Check vLLM logs: kubectl logs -n ai-dev -l app=vllm"
echo ""
echo -e "${GREEN}Deployment complete! 🚀${NC}"
echo ""
