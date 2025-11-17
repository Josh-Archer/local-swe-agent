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
        if [ -f "$file" ] && [[ ! "$file" =~ "template" ]]; then
            echo "  Applying $(basename $file)..."
            kubectl apply -f "$file"
        fi
    done

    echo -e "${GREEN}✓ ${description} deployed${NC}"
    echo ""
}

# Validate manifests first
echo -e "${BLUE}Step 1: Validating manifests...${NC}"
bash "$SCRIPT_DIR/validate-manifests.sh"
echo ""

# Apply in order
echo -e "${BLUE}Step 2: Creating namespace...${NC}"
kubectl apply -f "$AI_DEV_DIR/namespace/namespace.yaml"
echo ""

echo -e "${BLUE}Step 3: Creating storage...${NC}"
apply_manifests "storage" "Persistent Volume Claims"

echo -e "${BLUE}Step 4: Deploying Qdrant...${NC}"
apply_manifests "qdrant" "Qdrant Vector Database"

echo -e "${BLUE}Step 5: Deploying vLLM server...${NC}"
apply_manifests "vllm" "vLLM Inference Server"

echo -e "${BLUE}Step 6: Deploying code indexer...${NC}"
apply_manifests "code-indexer" "Code Indexer"

echo -e "${BLUE}Step 7: Deploying SWE-agent...${NC}"
apply_manifests "swe-agent" "SWE-agent"

echo -e "${BLUE}Step 8: Configuring ingress...${NC}"
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
