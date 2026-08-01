#!/bin/bash
# Validate all Kubernetes manifests and refuse insecure auth placeholders

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
    echo -n "Validating $(basename "$file")... "

    # Check YAML syntax
    if ! kubectl apply --dry-run=client -f "$file" &>/dev/null; then
        echo -e "${RED}FAILED${NC} (syntax error)"
        kubectl apply --dry-run=client -f "$file" 2>&1 | head -5
        FAILED=$((FAILED + 1))
        return 1
    fi

    # Server-side dry-run (if cluster is available)
    if kubectl cluster-info &>/dev/null; then
        if ! kubectl apply --dry-run=server -f "$file" &>/dev/null; then
            echo -e "${YELLOW}WARNING${NC} (server-side validation failed)"
            FAILED=$((FAILED + 1))
            return 1
        fi
    fi

    echo -e "${GREEN}PASSED${NC}"
    PASSED=$((PASSED + 1))
    return 0
}

# ---------------------------------------------------------------------------
# Refuse default / placeholder credentials that leave the API open or broken.
# Templates (*template*, example-secret.yaml) are allowed to contain markers;
# real manifests and any locally copied secret files must not.
# ---------------------------------------------------------------------------
check_auth_placeholders() {
    echo "Checking for insecure auth placeholders / default credentials..."
    local found=0

    # Known insecure default from older commits (user/password APR1 hash, base64)
    local default_b64="dXNlcjokYXByMSRQN0RnOUNuMyRXeUE3QzdyWEF6S1FYVG5xVkxVdTcwCg=="
    local default_apr1='$apr1$P7Dg9Cn3$WyA7C7rXAzKQXTnqVLUu70'

    # Patterns that must never appear in deployable (non-template) manifests
    local patterns=(
        "$default_b64"
        "$default_apr1"
        "CHANGE_ME"
        "CHANGE THIS"
        "CHANGE_THIS"
        "REPLACE_ME"
        "REPLACE_WITH_"
        "yourpassword"
        "YourPassword"
        "YourToken"
        "YourGitHubToken"
        "ghp_Your"
        "ghp_YOUR"
        "PLACEHOLDER_PASSWORD"
        "user/password"
        "user:password"
    )

    # Scan YAML under ai-dev, skip templates and example secrets
    while IFS= read -r -d '' file; do
        local base
        base=$(basename "$file")
        local rel="${file#$AI_DEV_DIR/}"

        # Skip committed templates / examples (placeholders are intentional)
        case "$base" in
            *template*|*example-secret*|*example*.yaml)
                continue
                ;;
        esac
        case "$rel" in
            *template*|*example-secret*)
                continue
                ;;
        esac

        for pat in "${patterns[@]}"; do
            if grep -qF -- "$pat" "$file" 2>/dev/null; then
                echo -e "${RED}✗ Forbidden placeholder/default in ${rel}:${NC}"
                grep -nF -- "$pat" "$file" | head -3 | sed 's/^/    /'
                found=1
            fi
        done
    done < <(find "$AI_DEV_DIR" -type f \( -name "*.yaml" -o -name "*.yml" \) \
        ! -path "*/tests/*" -print0 2>/dev/null)

    # Also refuse applying example-secret.yaml / secret-template.yaml as-is
    # if they still contain REPLACE_ markers when someone copies them to a
    # non-template name — handled above when filename loses "template".

    if [ "$found" -ne 0 ]; then
        echo ""
        echo -e "${RED}Deploy refused: replace placeholder credentials before deploy.${NC}"
        echo "Generate API auth:"
        echo "  htpasswd -nbB admin 'your-strong-password' > /tmp/auth"
        echo "  kubectl create secret generic api-auth-secret --from-file=users=/tmp/auth -n ai-dev"
        echo "  rm -f /tmp/auth"
        echo "See: ingress/example-secret.yaml and swe-agent/secret-template.yaml"
        FAILED=$((FAILED + 1))
        return 1
    fi

    echo -e "${GREEN}✓ No insecure auth placeholders in deployable manifests${NC}"
    PASSED=$((PASSED + 1))
    return 0
}

# Ensure ingress middleware still points at a secret that must be created out-of-band
check_ingress_auth_wiring() {
    local ingress="$AI_DEV_DIR/ingress/ingressroute.yaml"
    if [ ! -f "$ingress" ]; then
        return 0
    fi
    echo -n "Checking ingress auth wiring... "
    if ! grep -q "api-auth-secret" "$ingress"; then
        echo -e "${YELLOW}WARNING${NC} (api-auth-secret not referenced)"
        return 0
    fi
    # Secret must not be embedded with real data in the route file
    if grep -qE "^\s*users:\s+\S+" "$ingress" 2>/dev/null; then
        echo -e "${RED}FAILED${NC} (embedded users credentials in ingressroute.yaml)"
        FAILED=$((FAILED + 1))
        return 1
    fi
    echo -e "${GREEN}PASSED${NC} (secret expected out-of-band)"
    PASSED=$((PASSED + 1))
    return 0
}

# Find and validate all YAML files (skip secret templates / examples)
echo "Finding YAML files..."
while IFS= read -r -d '' file; do
    base=$(basename "$file")
    case "$base" in
        *template*|*example-secret*)
            echo "Skipping template/example: $base"
            continue
            ;;
    esac
    # Skip application config that is not a k8s manifest
    if [[ "$file" == *"/code-indexer/config.yaml" ]]; then
        continue
    fi
    validate_file "$file" || true
done < <(find "$AI_DEV_DIR" -name "*.yaml" -print0 2>/dev/null)

echo ""
check_auth_placeholders || true
check_ingress_auth_wiring || true

echo ""
echo "=== Validation Summary ==="
echo -e "Passed: ${GREEN}${PASSED}${NC}"
echo -e "Failed: ${RED}${FAILED}${NC}"

if [ "$FAILED" -gt 0 ]; then
    echo ""
    echo -e "${RED}Validation failed!${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}All validations passed!${NC}"
exit 0
