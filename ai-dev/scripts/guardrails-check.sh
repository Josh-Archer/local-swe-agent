#!/bin/bash
# Validate a working tree / range against guarded issue-to-PR policy.
# Usage:
#   guardrails-check.sh [--repo-dir DIR] [--base REF]
# Env:
#   PATH_ALLOWLIST       comma-separated prefixes/globs (default: ai-dev/,scripts/,docs/,*.md)
#   MAX_CHANGED_FILES    integer (default: 10)
#   ALLOW_FORCE_PUSH     true|false (default: false)
#   ENFORCE_GUARDRAILS   true|false (default: true) — when false, warn only
set -euo pipefail

REPO_DIR="${REPO_DIR:-.}"
BASE_REF="${BASE_REF:-}"
PATH_ALLOWLIST="${PATH_ALLOWLIST:-ai-dev/,scripts/,docs/,*.md}"
MAX_CHANGED_FILES="${MAX_CHANGED_FILES:-10}"
ALLOW_FORCE_PUSH="${ALLOW_FORCE_PUSH:-false}"
ENFORCE_GUARDRAILS="${ENFORCE_GUARDRAILS:-true}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="$2"; shift 2 ;;
    --base) BASE_REF="$2"; shift 2 ;;
    --allowlist) PATH_ALLOWLIST="$2"; shift 2 ;;
    --max-files) MAX_CHANGED_FILES="$2"; shift 2 ;;
    --allow-force-push) ALLOW_FORCE_PUSH="$2"; shift 2 ;;
    --enforce) ENFORCE_GUARDRAILS="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: guardrails-check.sh [--repo-dir DIR] [--base REF] [--allowlist LIST] [--max-files N]"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

cd "$REPO_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: $REPO_DIR is not a git repository" >&2
  exit 1
fi

if [[ -z "$BASE_REF" ]]; then
  if git rev-parse --verify origin/master >/dev/null 2>&1; then
    BASE_REF="origin/master"
  elif git rev-parse --verify origin/main >/dev/null 2>&1; then
    BASE_REF="origin/main"
  elif git rev-parse --verify HEAD >/dev/null 2>&1; then
    BASE_REF="HEAD"
  else
    echo "ERROR: could not determine base ref" >&2
    exit 1
  fi
fi

# Collect changed files (committed + unstaged vs base when possible)
CHANGED=()
while IFS= read -r line; do
  [[ -n "$line" ]] && CHANGED+=("$line")
done < <(
  {
    git diff --name-only "${BASE_REF}...HEAD" 2>/dev/null || true
    git diff --name-only "$BASE_REF" 2>/dev/null || true
    git diff --name-only --cached 2>/dev/null || true
    git ls-files --others --exclude-standard 2>/dev/null || true
  } | sed '/^$/d' | sort -u
)

FAIL=0
COUNT=${#CHANGED[@]}

echo "=== Guardrails check ==="
echo "Repo:              $(pwd)"
echo "Base:              $BASE_REF"
echo "Changed files:     $COUNT (max $MAX_CHANGED_FILES)"
echo "Path allowlist:    $PATH_ALLOWLIST"
echo "Allow force-push:  $ALLOW_FORCE_PUSH"
echo ""

# Max files
if [[ "$COUNT" -gt "$MAX_CHANGED_FILES" ]]; then
  echo "FAIL: changed file count $COUNT exceeds MAX_CHANGED_FILES=$MAX_CHANGED_FILES"
  FAIL=1
else
  echo "OK:   file count within limit"
fi

# Path allowlist: each changed path must match at least one rule
IFS=',' read -r -a RULES <<< "$PATH_ALLOWLIST"
path_allowed() {
  local f="$1" rule
  for rule in "${RULES[@]}"; do
    # trim whitespace
    rule="${rule#"${rule%%[![:space:]]*}"}"
    rule="${rule%"${rule##*[![:space:]]}"}"
    [[ -z "$rule" ]] && continue
    if [[ "$rule" == */ ]]; then
      [[ "$f" == "$rule"* ]] && return 0
    elif [[ "$rule" == *\** || "$rule" == *\?* ]]; then
      # shellcheck disable=SC2254
      case "$f" in
        $rule) return 0 ;;
      esac
      local base
      base="$(basename "$f")"
      # shellcheck disable=SC2254
      case "$base" in
        $rule) return 0 ;;
      esac
    else
      [[ "$f" == "$rule" || "$f" == "$rule"/* ]] && return 0
    fi
  done
  return 1
}

if [[ "$COUNT" -eq 0 ]]; then
  echo "OK:   no changed files to path-check"
else
  for f in "${CHANGED[@]}"; do
    if path_allowed "$f"; then
      echo "OK:   allowlisted: $f"
    else
      echo "FAIL: path not allowlisted: $f"
      FAIL=1
    fi
  done
fi

# No force-push unless explicitly allowed
allow_fp="$(echo "$ALLOW_FORCE_PUSH" | tr '[:upper:]' '[:lower:]')"
if [[ "$allow_fp" != "true" ]]; then
  if [[ "${GIT_PUSH_FORCE:-false}" == "true" || "${FORCE_PUSH:-false}" == "true" ]]; then
    echo "FAIL: force-push requested but ALLOW_FORCE_PUSH=false"
    FAIL=1
  else
    echo "OK:   force-push disabled"
  fi
  if [[ "${GIT_PUSH_OPTS:-}" == *"--force"* || "${GIT_PUSH_OPTS:-}" == *"-f"* ]]; then
    echo "FAIL: GIT_PUSH_OPTS contains force flag"
    FAIL=1
  fi
else
  echo "WARN: force-push is allowed (not recommended for demo)"
fi

echo ""
enforce="$(echo "$ENFORCE_GUARDRAILS" | tr '[:upper:]' '[:lower:]')"
if [[ "$FAIL" -ne 0 ]]; then
  echo "=== GUARDRAILS FAILED ==="
  if [[ "$enforce" == "true" ]]; then
    exit 1
  fi
  echo "(ENFORCE_GUARDRAILS=false — continuing with warnings)"
  exit 0
fi

echo "=== GUARDRAILS PASSED ==="
exit 0
