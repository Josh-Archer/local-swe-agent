#!/bin/bash
# Validate all Kubernetes manifests, including code-indexer image readiness.
#
# Code-indexer is build-yourself: manifests must not reference placeholder
# tags or use a pull policy that cannot resolve the image at deploy time.

set -euo pipefail

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

# Known public single-name base images (not build-yourself).
is_known_public_base_image() {
    local image=$1
    case "$image" in
        python:*|ubuntu:*|debian:*|alpine:*|busybox:*|nginx:*|redis:*|postgres:*|node:*|golang:*)
            return 0
            ;;
    esac
    return 1
}

# Registry-qualified (contains /) or known public base → not treated as local-only.
is_public_or_registry_image() {
    local image=$1
    if [[ "$image" == *"/"* ]]; then
        return 0
    fi
    if is_known_public_base_image "$image"; then
        return 0
    fi
    return 1
}

is_placeholder_image() {
    local image=$1
    if echo "$image" | grep -Eiq \
        'CHANGE_ME|YOUR_ORG|YOUR_REGISTRY|your-registry|REPLACE_ME|REPLACEME|TODO_IMAGE|example\.com|<registry>|<tag>|<owner>'; then
        return 0
    fi
    return 1
}

is_local_only_image() {
    local image=$1
    if is_public_or_registry_image "$image"; then
        return 1
    fi
    return 0
}

# Resolve effective pull policy (Kubernetes defaults).
effective_pull_policy() {
    local image=$1
    local policy=$2
    if [[ "$policy" != "DEFAULT" && -n "$policy" ]]; then
        echo "$policy"
        return
    fi
    # Default: Always for :latest or untagged; IfNotPresent otherwise
    if [[ "$image" == *":latest" || "$image" != *":"* ]]; then
        echo "Always"
    else
        echo "IfNotPresent"
    fi
}

# Check container images / pull policies in a manifest. Returns 1 on hard failure.
check_images_in_file() {
    local file=$1
    local basename_file
    basename_file=$(basename "$file")
    local file_failed=0

    # Skip non-workload / non-K8s resource files
    case "$basename_file" in
        config.yaml|kustomization.yaml) return 0 ;;
    esac

    # Best-effort parse of image: / imagePullPolicy: pairs
    local results
    results=$(awk '
        BEGIN { image=""; policy=""; img_line=0 }
        /^[[:space:]]*image:[[:space:]]*/ {
            line=$0
            sub(/#.*/,"",line)
            sub(/^[[:space:]]*image:[[:space:]]*/,"",line)
            gsub(/["\047]/,"",line)
            gsub(/[[:space:]]+$/,"",line)
            if (line != "") {
                image=line
                img_line=NR
                policy="DEFAULT"
            }
        }
        /^[[:space:]]*imagePullPolicy:[[:space:]]*/ {
            line=$0
            sub(/#.*/,"",line)
            sub(/^[[:space:]]*imagePullPolicy:[[:space:]]*/,"",line)
            gsub(/["\047]/,"",line)
            gsub(/[[:space:]]+$/,"",line)
            if (image != "") {
                print image "|" line "|" img_line
                image=""
                policy=""
            }
        }
        # Flush image when a new list item name appears without an intervening policy
        /^[[:space:]]*-[[:space:]]+name:[[:space:]]*/ {
            if (image != "") {
                print image "|" policy "|" img_line
                image=""
                policy=""
            }
        }
        END {
            if (image != "") {
                print image "|" policy "|" img_line
            }
        }
    ' "$file")

    local line image policy eff
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        image="${line%%|*}"
        rest="${line#*|}"
        policy="${rest%%|*}"

        [[ -z "$image" ]] && continue

        # 1) Placeholder tags must never ship into a deploy
        if is_placeholder_image "$image"; then
            echo -e "  ${RED}FAILED${NC}: placeholder image in ${basename_file}: ${image}"
            echo "         Replace with a built+pushed tag (or local image + IfNotPresent/Never)."
            echo "         See code-indexer/cronjob.yaml header and README build steps."
            file_failed=1
            continue
        fi

        eff=$(effective_pull_policy "$image" "$policy")

        # 2) Local-only / build-yourself image + Always → pull will break deploy
        if is_local_only_image "$image" && [[ "$eff" == "Always" ]]; then
            echo -e "  ${RED}FAILED${NC}: image pull policy would break deploy in ${basename_file}"
            echo "         image=$image  effectivePullPolicy=$eff"
            echo "         Local/unpublished images cannot use Always (kubelet will try to pull)."
            echo "         Fix: push to a registry, or set imagePullPolicy: IfNotPresent (or Never)."
            file_failed=1
            continue
        fi

        # 3) Bare code-indexer:* is never public — Always always breaks
        if [[ "$image" == code-indexer || "$image" == code-indexer:* ]]; then
            if [[ "$eff" == "Always" ]]; then
                echo -e "  ${RED}FAILED${NC}: code-indexer image is build-yourself; Always will fail pull"
                echo "         image=$image in ${basename_file}"
                file_failed=1
                continue
            fi
            echo -e "  ${YELLOW}NOTE${NC}: local code-indexer image ($image); ensure it exists on every node."
        fi
    done <<< "$results"

    return "$file_failed"
}

# Function to validate a YAML file
validate_file() {
    local file=$1
    local basename_file
    basename_file=$(basename "$file")
    local issues=0

    echo -n "Validating ${basename_file}... "

    # kustomization is not a cluster resource — skip kubectl apply
    if [[ "$basename_file" == "kustomization.yaml" ]]; then
        echo -e "${GREEN}SKIPPED${NC} (kustomize file)"
        PASSED=$((PASSED + 1))
        return 0
    fi

    # Client-side dry-run when kubectl is available
    if command -v kubectl &>/dev/null; then
        if ! kubectl apply --dry-run=client -f "$file" &>/dev/null; then
            echo -e "${RED}FAILED${NC} (syntax / client dry-run error)"
            kubectl apply --dry-run=client -f "$file" 2>&1 | head -5
            issues=1
        else
            # Server-side is advisory only (CRDs / cluster may be incomplete)
            if kubectl cluster-info &>/dev/null; then
                if ! kubectl apply --dry-run=server -f "$file" &>/dev/null; then
                    echo -ne "${YELLOW}server-warn${NC} "
                fi
            fi
        fi
    elif command -v python3 &>/dev/null; then
        if ! python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))" "$file" 2>/dev/null; then
            echo -e "${RED}FAILED${NC} (YAML parse error)"
            issues=1
        fi
    fi

    # Image / pull-policy readiness — always enforced
    if ! check_images_in_file "$file"; then
        issues=1
    fi

    if [[ "$issues" -ne 0 ]]; then
        echo -e "${RED}FAILED${NC}"
        FAILED=$((FAILED + 1))
        return 1
    fi

    echo -e "${GREEN}PASSED${NC}"
    PASSED=$((PASSED + 1))
    return 0
}

# Find and validate all YAML files (current shell so counters stick)
echo "Finding YAML files..."
while IFS= read -r -d '' file; do
    validate_file "$file" || true
done < <(find "$AI_DEV_DIR" -name "*.yaml" \
    -not -path "*/secret-template.yaml" \
    -not -name "config.yaml" \
    -print0)

echo ""
echo "=== Code-indexer image prerequisite ==="
echo "Code-indexer is build-yourself. Before deploy you MUST:"
echo "  1. docker build -t <registry>/local-swe-agent/code-indexer:<tag> ai-dev/code-indexer"
echo "  2. docker push <registry>/local-swe-agent/code-indexer:<tag>"
echo "  3. Set that image (no CHANGE_ME/YOUR_*) in code-indexer/cronjob.yaml"
echo "  4. Use imagePullPolicy: Always only for published registry tags;"
echo "     use IfNotPresent or Never for node-local images."
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
