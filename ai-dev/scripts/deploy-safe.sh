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

# Refuse deployable manifests that still embed default/placeholder auth
echo -n "Checking for auth placeholders in manifests... "
if ! bash "$SCRIPT_DIR/validate-manifests.sh" >/tmp/ai-dev-validate.out 2>&1; then
    echo -e "${RED}FAILED${NC}"
    cat /tmp/ai-dev-validate.out
    echo -e "${RED}Deploy refused: fix placeholder credentials / manifest errors first.${NC}"
    exit 1
fi
echo -e "${GREEN}✓${NC}"

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
if kubectl get pods -n media -l app=plex --no-headers 2>/dev/null | grep -q .; then
    PLEX_STATUS=$(kubectl get pods -n media -l app=plex -o jsonpath='{.items[0].status.phase}')
    if [ "$PLEX_STATUS" = "Running" ]; then
        echo -e "${GREEN}✓ Plex is Running${NC}"
    else
        echo -e "${RED}✗ Plex status: ${PLEX_STATUS}${NC}"
        echo -e "${RED}Hard constraint: refuse deploy while Plex is unhealthy (see GPU_CONSTRAINTS.md)${NC}"
        if [ "${FORCE_GPU_ADMISSION:-0}" != "1" ] || [ "${ALLOW_GPU_OVERRIDE:-0}" != "1" ]; then
            exit 20
        fi
        echo -e "${YELLOW}⚠ Break-glass override active — continuing despite Plex status${NC}"
    fi
else
    echo -e "${YELLOW}⚠ Plex not found (may be okay in non-prod)${NC}"
fi

echo ""
echo -e "${BLUE}Applying PriorityClasses (Plex > AI GPU)...${NC}"
if [ -f "$AI_DEV_DIR/scheduling/priority-classes.yaml" ]; then
    kubectl apply -f "$AI_DEV_DIR/scheduling/priority-classes.yaml"
    echo -e "${GREEN}✓ PriorityClasses applied (plex-media-critical, ai-dev-gpu)${NC}"
else
    echo -e "${YELLOW}⚠ priority-classes.yaml missing — vLLM may fail if priorityClassName is set${NC}"
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
echo -e "${YELLOW}⚠️  HARD admission gate runs before apply (not advisory-only)${NC}"
echo -e "${YELLOW}⚠️  Plex health will be checked immediately after${NC}"
echo ""

if [ "$INTERACTIVE" = "1" ]; then
    echo -e "${CYAN}Pre-GPU Deployment Check:${NC}"
    echo "- Hard GPU admission (free shares, Plex, node Ready)"
    echo "- Current GPU allocation will be shown"
    echo "- Plex health will be verified after deploy"
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

# HARD ENFORCE: refuse Phase 3 when GPU is busy or Plex unhealthy
echo -e "${RED}Running hard GPU admission gate...${NC}"
if [ -f "$SCRIPT_DIR/check-gpu-admission.sh" ]; then
    set +e
    bash "$SCRIPT_DIR/check-gpu-admission.sh"
    ADMIT_RC=$?
    set -e
    if [ "$ADMIT_RC" -ne 0 ]; then
        echo ""
        echo -e "${RED}✗ GPU admission DENIED (exit ${ADMIT_RC})${NC}"
        echo -e "${RED}Failure mode: GPU busy or coexistence hard constraint failed.${NC}"
        echo "See: ai-dev/GPU_CONSTRAINTS.md"
        echo "Break-glass (both required): ALLOW_GPU_OVERRIDE=1 FORCE_GPU_ADMISSION=1"
        exit "$ADMIT_RC"
    fi
else
    echo -e "${RED}✗ check-gpu-admission.sh missing — cannot hard-enforce GPU coexistence${NC}"
    exit 31
fi

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
        echo -e "${RED}Hard constraint: post-GPU Plex failure aborts deploy (not advisory).${NC}"
        echo -e "${YELLOW}EMERGENCY ROLLBACK OPTIONS:${NC}"
        echo "1. Scale down vLLM: kubectl scale deployment -n ai-dev vllm-server --replicas=0"
        echo "2. Delete vLLM: kubectl delete deployment -n ai-dev vllm-server"
        echo "3. Full rollback: kubectl delete namespace ai-dev"
        echo ""
        if [ "${FORCE_GPU_ADMISSION:-0}" = "1" ] && [ "${ALLOW_GPU_OVERRIDE:-0}" = "1" ]; then
            echo -e "${YELLOW}⚠ Break-glass override — continuing despite Plex health failure${NC}"
        else
            echo -e "${YELLOW}Deployment aborted due to Plex health check failure${NC}"
            echo "Break-glass (both required): ALLOW_GPU_OVERRIDE=1 FORCE_GPU_ADMISSION=1"
            exit 20
        fi
    fi
else
    echo -e "${RED}✗ Plex health check script not found — hard gate required${NC}"
    exit 31
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
    # Check if secret exists and is not a placeholder
    if kubectl get secret -n ai-dev swe-agent-secrets &>/dev/null; then
        token_b64=$(kubectl get secret swe-agent-secrets -n ai-dev -o jsonpath='{.data.github-token}' 2>/dev/null || true)
        token_plain=$(printf '%s' "$token_b64" | base64 -d 2>/dev/null || printf '%s' "$token_b64" | base64 -D 2>/dev/null || true)
        if echo "$token_plain" | grep -qE 'REPLACE_WITH_|CHANGE_ME|YourToken|YourGitHubToken|ghp_Your|ghp_YOUR|PLACEHOLDER'; then
            echo -e "${RED}✗ swe-agent-secrets contains a placeholder token — refusing deploy${NC}"
            echo "  See: swe-agent/secret-template.yaml"
            exit 1
        fi
        echo -e "${GREEN}✓ SWE-agent secret already exists${NC}"
    else
        echo -e "${YELLOW}⚠ SWE-agent secret not found${NC}"
        echo "Create secret first (real token, not a placeholder):"
        echo "  kubectl create secret generic swe-agent-secrets \\"
        echo "    --from-literal=github-token=\"\$GITHUB_TOKEN\" \\"
        echo "    -n ai-dev"
        echo "  See: swe-agent/secret-template.yaml"
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
echo -e "${YELLOW}Requires api-auth-secret with real credentials (no placeholders)${NC}"
echo ""

# Refuse default/placeholder credentials before exposing the API
require_api_auth_secret() {
    local default_b64="dXNlcjokYXByMSRQN0RnOUNuMyRXeUE3QzdyWEF6S1FYVG5xVkxVdTcwCg=="
    if ! kubectl get secret -n ai-dev api-auth-secret &>/dev/null; then
        echo -e "${RED}✗ api-auth-secret not found${NC}"
        echo "Create real credentials first (placeholders are refused):"
        echo "  htpasswd -nbB admin 'your-strong-password' > /tmp/auth"
        echo "  kubectl create secret generic api-auth-secret --from-file=users=/tmp/auth -n ai-dev"
        echo "  rm -f /tmp/auth"
        echo "See: ingress/example-secret.yaml"
        return 1
    fi
    local key val plain
    for key in $(kubectl get secret api-auth-secret -n ai-dev -o go-template='{{range $k,$v := .data}}{{printf "%s\n" $k}}{{end}}' 2>/dev/null); do
        val=$(kubectl get secret api-auth-secret -n ai-dev -o "jsonpath={.data.$key}" 2>/dev/null)
        plain=$(printf '%s' "$val" | base64 -d 2>/dev/null || printf '%s' "$val" | base64 -D 2>/dev/null || true)
        if [ "$val" = "$default_b64" ] || echo "$plain" | grep -qF '$apr1$P7Dg9Cn3$WyA7C7rXAzKQXTnqVLUu70'; then
            echo -e "${RED}✗ api-auth-secret uses the insecure default user/password hash${NC}"
            return 1
        fi
        if echo "$plain" | grep -qE 'REPLACE_WITH_|CHANGE_ME|CHANGE_THIS|yourpassword|PLACEHOLDER|user:password'; then
            echo -e "${RED}✗ api-auth-secret contains a placeholder value${NC}"
            return 1
        fi
    done
    echo -e "${GREEN}✓ api-auth-secret present and not a known placeholder/default${NC}"
    return 0
}

# Manifest-level placeholder check (same gate as validate-manifests.sh)
if ! bash "$SCRIPT_DIR/validate-manifests.sh"; then
    echo -e "${RED}Manifest validation failed (placeholder credentials or syntax). Aborting ingress phase.${NC}"
    exit 1
fi

read -p "Deploy ingress? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if ! require_api_auth_secret; then
        echo -e "${RED}Refusing to deploy ingress with missing/placeholder auth — API would be open or broken.${NC}"
        exit 1
    fi

    kubectl apply -f "$AI_DEV_DIR/ingress/ingressroute.yaml"

    echo -e "${GREEN}✓ Ingress deployed${NC}"
    echo ""
    echo "IngressRoute created for: code-llm.archer.casa"
    echo "Update DNS to point to your cluster IP"
    echo "Test: curl -u admin:YOUR_PASSWORD https://code-llm.archer.casa/health"
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
