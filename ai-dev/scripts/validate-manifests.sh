#!/bin/bash
# Validate all Kubernetes manifests

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_DEV_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Validating Kubernetes Manifests ==="
echo "AI-Dev directory: $AI_DEV_DIR"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

FAILED=0
PASSED=0

# Function to validate a YAML file
validate_file() {
    local file=$1
    echo -n "Validating $(basename $file)... "

    # Check YAML syntax
    if ! kubectl apply --dry-run=client -f "$file" &>/dev/null; then
        echo -e "${RED}FAILED${NC} (syntax error)"
        kubectl apply --dry-run=client -f "$file" 2>&1 | head -5
        ((FAILED++))
        return 1
    fi

    # Server-side dry-run (if cluster is available)
    if kubectl cluster-info &>/dev/null; then
        if ! kubectl apply --dry-run=server -f "$file" &>/dev/null; then
            echo -e "${YELLOW}WARNING${NC} (server-side validation failed)"
            ((FAILED++))
            return 1
        fi
    fi

    echo -e "${GREEN}PASSED${NC}"
    ((PASSED++))
    return 0
}

# Find and validate all YAML files
echo "Finding YAML files..."
find "$AI_DEV_DIR" -name "*.yaml" -not -path "*/secret-template.yaml" | while read -r file; do
    validate_file "$file"
done

echo ""
echo "=== Validation Summary ==="
echo -e "Passed: ${GREEN}${PASSED}${NC}"
echo -e "Failed: ${RED}${FAILED}${NC}"

if [ $FAILED -gt 0 ]; then
    echo ""
    echo -e "${RED}Validation failed!${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}All validations passed!${NC}"
exit 0
