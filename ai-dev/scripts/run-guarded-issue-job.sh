#!/bin/bash
# Launch a guarded SWE-agent Job for a GitHub issue (by number or label).
#
# Examples:
#   ./run-guarded-issue-job.sh --repo Josh-Archer/local-swe-agent --issue 1
#   ./run-guarded-issue-job.sh --repo Josh-Archer/local-swe-agent --label ai-fix
#   ./run-guarded-issue-job.sh --repo Josh-Archer/local-swe-agent --issue 1 --open-pr --approved
#
# Guardrails (defaults):
#   PATH_ALLOWLIST=ai-dev/,scripts/,docs/,*.md
#   MAX_CHANGED_FILES=10
#   ALLOW_FORCE_PUSH=false
#   OPEN_PR=false  (human approval gate)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_DEV_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="${AI_DEV_DIR}/swe-agent/job-template.yaml"
NAMESPACE="${NAMESPACE:-ai-dev}"

REPO=""
ISSUE_NUMBER=""
ISSUE_LABEL=""
ISSUE_URL=""
OPEN_PR="false"
HUMAN_APPROVED="false"
PATH_ALLOWLIST="${PATH_ALLOWLIST:-ai-dev/,scripts/,docs/,*.md}"
MAX_CHANGED_FILES="${MAX_CHANGED_FILES:-10}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: run-guarded-issue-job.sh --repo OWNER/REPO (--issue N | --label NAME | --url URL) [options]

Required:
  --repo OWNER/REPO     GitHub repository
  One of:
    --issue N           Issue number
    --label NAME        First open issue with this label
    --url URL           Full GitHub issue URL

Options:
  --open-pr             Request PR creation (still blocked unless --approved)
  --approved            Human has reviewed; allow OPEN_PR when combined with --open-pr
  --allowlist LIST      Comma-separated path allowlist
  --max-files N         Max changed files (default: 10)
  --namespace NS        Kubernetes namespace (default: ai-dev)
  --dry-run             Print rendered manifest only
  -h, --help            Show this help

Human approval gate:
  By default OPEN_PR=false. After the agent finishes and you review the branch,
  re-run with --open-pr --approved, or open the PR manually via gh.
  See: ai-dev/swe-agent/GUARDED_ISSUE_TO_PR.md
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --issue) ISSUE_NUMBER="$2"; shift 2 ;;
    --label) ISSUE_LABEL="$2"; shift 2 ;;
    --url) ISSUE_URL="$2"; shift 2 ;;
    --open-pr) OPEN_PR="true"; shift ;;
    --approved) HUMAN_APPROVED="true"; shift ;;
    --allowlist) PATH_ALLOWLIST="$2"; shift 2 ;;
    --max-files) MAX_CHANGED_FILES="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --dry-run|--no-apply) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: template not found: $TEMPLATE" >&2
  exit 1
fi

if [[ -z "$REPO" && -z "$ISSUE_URL" ]]; then
  echo "ERROR: --repo OWNER/REPO is required (or pass a full --url)" >&2
  usage >&2
  exit 1
fi

if [[ -z "$ISSUE_NUMBER" && -z "$ISSUE_LABEL" && -z "$ISSUE_URL" ]]; then
  echo "ERROR: provide --issue, --label, or --url" >&2
  usage >&2
  exit 1
fi

GITHUB_OWNER=""
GITHUB_REPO=""
if [[ -n "$REPO" ]]; then
  GITHUB_OWNER="${REPO%%/*}"
  GITHUB_REPO="${REPO#*/}"
  if [[ -z "$GITHUB_OWNER" || -z "$GITHUB_REPO" || "$GITHUB_OWNER" == "$REPO" ]]; then
    echo "ERROR: --repo must be OWNER/REPO" >&2
    exit 1
  fi
fi

if [[ -n "$ISSUE_URL" && -z "$ISSUE_NUMBER" ]]; then
  if [[ "$ISSUE_URL" =~ /issues/([0-9]+) ]]; then
    ISSUE_NUMBER="${BASH_REMATCH[1]}"
  fi
  if [[ -z "$GITHUB_OWNER" && "$ISSUE_URL" =~ github.com/([^/]+)/([^/]+) ]]; then
    GITHUB_OWNER="${BASH_REMATCH[1]}"
    GITHUB_REPO="${BASH_REMATCH[2]}"
  fi
fi

if [[ -z "$ISSUE_URL" && -n "$ISSUE_NUMBER" && -n "$GITHUB_OWNER" && -n "$GITHUB_REPO" ]]; then
  ISSUE_URL="https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/issues/${ISSUE_NUMBER}"
fi

if [[ -n "$ISSUE_NUMBER" ]]; then
  JOB_SUFFIX="$(echo "$ISSUE_NUMBER" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g')"
elif [[ -n "$ISSUE_LABEL" ]]; then
  JOB_SUFFIX="label-$(echo "$ISSUE_LABEL" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g')"
else
  JOB_SUFFIX="manual"
fi
JOB_NAME="swe-agent-issue-${JOB_SUFFIX}"

# Human approval: OPEN_PR only if both flags set
if [[ "$OPEN_PR" == "true" && "$HUMAN_APPROVED" != "true" ]]; then
  echo "NOTE: --open-pr without --approved; forcing OPEN_PR=false (human approval gate)."
  OPEN_PR="false"
fi

# Escape sed replacement specials: \ & |
escape_sed() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

render() {
  local owner repo url num label allow max open approved ns job
  owner="$(escape_sed "$GITHUB_OWNER")"
  repo="$(escape_sed "$GITHUB_REPO")"
  url="$(escape_sed "$ISSUE_URL")"
  num="$(escape_sed "${ISSUE_NUMBER}")"
  label="$(escape_sed "${ISSUE_LABEL}")"
  allow="$(escape_sed "$PATH_ALLOWLIST")"
  max="$(escape_sed "$MAX_CHANGED_FILES")"
  open="$(escape_sed "$OPEN_PR")"
  approved="$(escape_sed "$HUMAN_APPROVED")"
  ns="$(escape_sed "$NAMESPACE")"
  job="$(escape_sed "$JOB_NAME")"

  # shellcheck disable=SC2002
  cat "$TEMPLATE" | sed \
    -e "s|name: swe-agent-issue-ISSUE_NUMBER_PLACEHOLDER|name: ${job}|" \
    -e "s|ISSUE_URL_PLACEHOLDER|${url}|g" \
    -e "s|ISSUE_NUMBER_PLACEHOLDER|${num}|g" \
    -e "s|ISSUE_LABEL_PLACEHOLDER|${label}|g" \
    -e "s|GITHUB_OWNER_PLACEHOLDER|${owner}|g" \
    -e "s|GITHUB_REPO_PLACEHOLDER|${repo}|g" \
    -e "s|namespace: ai-dev|namespace: ${ns}|g" \
    -e "s|value: \"ai-dev/,scripts/,docs/,\\*.md\"|value: \"${allow}\"|" \
    -e "s|value: \"10\"|value: \"${max}\"|" \
    | awk -v open_pr="$OPEN_PR" -v approved="$HUMAN_APPROVED" '
      /name: OPEN_PR$/ {
        print
        if (getline > 0) {
          sub(/value: ".*"/, "value: \"" open_pr "\"")
          print
        }
        next
      }
      /name: REQUIRE_HUMAN_APPROVAL$/ {
        print
        if (getline > 0) { print }
        print "        - name: HUMAN_APPROVED"
        print "          value: \"" approved "\""
        next
      }
      { print }
    '
}

log() { echo "$*" >&2; }

log "=== Guarded issue-to-PR job ==="
log "Repo:       ${GITHUB_OWNER}/${GITHUB_REPO}"
log "Issue #:    ${ISSUE_NUMBER:-"(via label)"}"
log "Label:      ${ISSUE_LABEL:-"(none)"}"
log "Issue URL:  ${ISSUE_URL:-"(resolve in-cluster)"}"
log "Job name:   $JOB_NAME"
log "Namespace:  $NAMESPACE"
log "OPEN_PR:    $OPEN_PR (approved=$HUMAN_APPROVED)"
log "Allowlist:  $PATH_ALLOWLIST"
log "Max files:  $MAX_CHANGED_FILES"
log "Force-push: forbidden"
log ""

RENDERED="$(render)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf '%s\n' "$RENDERED"
  exit 0
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl not found" >&2
  exit 1
fi

if kubectl get job -n "$NAMESPACE" "$JOB_NAME" >/dev/null 2>&1; then
  echo "Deleting existing job $JOB_NAME..."
  kubectl delete job -n "$NAMESPACE" "$JOB_NAME" --wait=true
fi

printf '%s\n' "$RENDERED" | kubectl apply -f -

echo ""
echo "Job created. Monitor with:"
echo "  kubectl logs -n $NAMESPACE -f job/$JOB_NAME"
echo ""
echo "After success, review the branch and open a PR only if guardrails passed."
echo "Docs: ai-dev/swe-agent/GUARDED_ISSUE_TO_PR.md"
