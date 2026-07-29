#!/bin/bash
# Deploy AI-dev system to Kubernetes cluster

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_DEV_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=== AI-Dev Deployment Script ===${NC}"
echo ""

# Check kubectl connection
if ! kubectl cluster-info &>/dev/null; then
    echo -e "${RED}Error: Cannot connect to Kubernetes cluster${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Connected to Kubernetes cluster${NC}"
echo ""

# Function to apply manifests
apply_manifests() {
    local dir=$1
    local description=$2

    echo -e "${BLUE}Deploying ${description}...${NC}"

    if [ ! -d "$AI_DEV_DIR/$dir" ]; then
        echo -e "${YELLOW}Warning: Directory $dir not found, skipping${NC}"
        return
    fi

    for file in "$AI_DEV_DIR/$dir"/*.yaml; do
        base=$(basename "$file")
        # Never apply templates / example secrets (placeholders only)
        if [ -f "$file" ] && [[ ! "$base" =~ template ]] && [[ ! "$base" =~ example-secret ]] && [[ ! "$base" =~ example- ]]; then
            echo "  Applying $base..."
            kubectl apply -f "$file"
        fi
    done

    echo -e "${GREEN}✓ ${description} deployed${NC}"
    echo ""
}

# Validate manifests first (includes placeholder credential refusal)
echo -e "${BLUE}Step 1: Validating manifests...${NC}"
bash "$SCRIPT_DIR/validate-manifests.sh"
echo ""

# Pre-flight: refuse deploy if secrets still look like placeholders / defaults
require_real_secret() {
    local name=$1
    local ns=${2:-ai-dev}
    if ! kubectl get secret "$name" -n "$ns" &>/dev/null; then
        echo -e "${RED}✗ Required secret ${name} not found in namespace ${ns}${NC}"
        return 1
    fi
    # Decode all data values and reject known placeholders / defaults
    local default_b64="dXNlcjokYXByMSRQN0RnOUNuMyRXeUE3QzdyWEF6S1FYVG5xVkxVdTcwCg=="
    local decoded
    decoded=$(kubectl get secret "$name" -n "$ns" -o jsonpath='{.data}' 2>/dev/null || true)
    if echo "$decoded" | grep -qF "$default_b64"; then
        echo -e "${RED}✗ Secret ${name} still uses the insecure default user/password hash${NC}"
        return 1
    fi
    local keys values key val plain
    keys=$(kubectl get secret "$name" -n "$ns" -o jsonpath='{.data}' 2>/dev/null | tr ',' '\n' | sed -n 's/.*"\([^"]*\)":.*/\1/p' || true)
    # Portable check: dump secret data keys and base64-decode each
    for key in $(kubectl get secret "$name" -n "$ns" -o go-template='{{range $k,$v := .data}}{{printf "%s\n" $k}}{{end}}' 2>/dev/null); do
        val=$(kubectl get secret "$name" -n "$ns" -o "jsonpath={.data.$key}" 2>/dev/null)
        plain=$(printf '%s' "$val" | base64 -d 2>/dev/null || printf '%s' "$val" | base64 -D 2>/dev/null || true)
        if echo "$plain" | grep -qE 'REPLACE_WITH_|CHANGE_ME|CHANGE_THIS|yourpassword|YourToken|ghp_Your|ghp_YOUR|PLACEHOLDER|user:password'; then
            echo -e "${RED}✗ Secret ${name} key '${key}' contains a placeholder value${NC}"
            return 1
        fi
        if [ "$val" = "$default_b64" ] || echo "$plain" | grep -qF '$apr1$P7Dg9Cn3$WyA7C7rXAzKQXTnqVLUu70'; then
            echo -e "${RED}✗ Secret ${name} uses the documented insecure default password hash${NC}"
            return 1
        fi
    done
    echo -e "${GREEN}✓ Secret ${name} present and not a known placeholder${NC}"
    return 0
}

# Apply in order
echo -e "${BLUE}Step 2: Creating namespace...${NC}"
kubectl apply -f "$AI_DEV_DIR/namespace/namespace.yaml"
echo ""

echo -e "${BLUE}Step 2b: PriorityClasses (Plex > AI GPU)...${NC}"
if [ -f "$AI_DEV_DIR/scheduling/priority-classes.yaml" ]; then
    kubectl apply -f "$AI_DEV_DIR/scheduling/priority-classes.yaml"
else
    echo -e "${YELLOW}Warning: scheduling/priority-classes.yaml not found${NC}"
fi
echo ""

echo -e "${BLUE}Step 3: Creating storage...${NC}"
apply_manifests "storage" "Persistent Volume Claims"

echo -e "${BLUE}Step 4: Deploying Qdrant...${NC}"
apply_manifests "qdrant" "Qdrant Vector Database"

echo -e "${BLUE}Step 4b: Hard GPU admission (coexistence with Plex)...${NC}"
if [ -f "$SCRIPT_DIR/check-gpu-admission.sh" ]; then
    set +e
    bash "$SCRIPT_DIR/check-gpu-admission.sh"
    ADMIT_RC=$?
    set -e
    if [ "$ADMIT_RC" -ne 0 ]; then
        echo -e "${RED}GPU admission denied (exit ${ADMIT_RC}) — see ai-dev/GPU_CONSTRAINTS.md${NC}"
        exit "$ADMIT_RC"
    fi
else
    echo -e "${RED}check-gpu-admission.sh missing${NC}"
    exit 31
fi
echo ""

echo -e "${BLUE}Step 5: Deploying vLLM server...${NC}"
apply_manifests "vllm" "vLLM Inference Server"

echo -e "${BLUE}Step 6: Deploying code indexer...${NC}"
apply_manifests "code-indexer" "Code Indexer"

echo -e "${BLUE}Step 7: Deploying SWE-agent...${NC}"
if kubectl get secret swe-agent-secrets -n ai-dev &>/dev/null; then
    if ! require_real_secret swe-agent-secrets ai-dev; then
        echo -e "${RED}Fix swe-agent-secrets before deploying SWE-agent.${NC}"
        echo "  See: swe-agent/secret-template.yaml"
        exit 1
    fi
    apply_manifests "swe-agent" "SWE-agent"
else
    echo -e "${YELLOW}Warning: swe-agent-secrets missing — applying SWE-agent RBAC/config only if present${NC}"
    echo "  Create with: kubectl create secret generic swe-agent-secrets --from-literal=github-token=... -n ai-dev"
    apply_manifests "swe-agent" "SWE-agent"
fi

echo -e "${BLUE}Step 8: Configuring ingress...${NC}"
# Ingress basicAuth middleware requires a real secret; refuse placeholders / absence
if ! require_real_secret api-auth-secret ai-dev; then
    echo -e "${RED}Refusing to deploy ingress: create api-auth-secret with real credentials first.${NC}"
    echo "  htpasswd -nbB admin 'your-strong-password' > /tmp/auth"
    echo "  kubectl create secret generic api-auth-secret --from-file=users=/tmp/auth -n ai-dev"
    echo "  rm -f /tmp/auth"
    echo "  See: ingress/example-secret.yaml"
    exit 1
fi
apply_manifests "ingress" "Traefik Ingress"

echo ""
echo -e "${GREEN}=== Deployment Complete ===${NC}"
echo ""

# Check pod status
echo -e "${BLUE}Checking pod status...${NC}"
kubectl get pods -n ai-dev

echo ""
echo -e "${YELLOW}Note: It may take several minutes for all pods to become ready${NC}"
echo -e "${YELLOW}Monitor progress with: kubectl get pods -n ai-dev -w${NC}"
echo ""

# Wait for critical pods
echo -e "${BLUE}Waiting for critical pods to be ready...${NC}"
echo "  - Qdrant"
kubectl wait --for=condition=ready pod -l app=qdrant -n ai-dev --timeout=300s || true

echo "  - vLLM Server (this may take 5-10 minutes to download model)"
kubectl wait --for=condition=ready pod -l app=vllm -n ai-dev --timeout=600s || true

echo ""
echo -e "${GREEN}=== Deployment Summary ===${NC}"
kubectl get all -n ai-dev

echo ""
echo -e "${BLUE}Next Steps:${NC}"
echo "1. Check logs: kubectl logs -n ai-dev -l app=vllm -f"
echo "2. Test API: python3 scripts/test-vllm-api.py --url http://localhost:8000"
echo "3. Port forward: kubectl port-forward -n ai-dev svc/vllm-server 8000:8000"
echo "4. Configure code indexer repositories in: code-indexer/configmap.yaml"
echo "5. Run manual indexing: kubectl create job --from=cronjob/code-indexer manual-index -n ai-dev"
echo ""
