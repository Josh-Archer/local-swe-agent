#!/bin/sh
# Best-effort retrieval of indexed code into /rag/retrieved_code_context.md.
# Runs in the code-indexer image (init container) before sweagent. Always exits 0
# so a Qdrant outage does not block the agent job.
set +e

echo "=== RAG init retrieval ==="
enable=$(printf '%s' "${ENABLE_RAG:-true}" | tr '[:upper:]' '[:lower:]')
if [ "$enable" != "true" ] && [ "$enable" != "1" ]; then
  echo "RAG disabled (ENABLE_RAG=$ENABLE_RAG)"
  exit 0
fi

QUERY="${RETRIEVAL_QUERY:-}"
if [ -z "$QUERY" ] && [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_OWNER:-}" ] && [ -n "${GITHUB_REPO:-}" ] && [ -n "${ISSUE_NUMBER:-}" ]; then
  echo "Fetching issue #${ISSUE_NUMBER} for the retrieval query..."
  issue_json=$(curl -fsSL \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/issues/${ISSUE_NUMBER}" 2>/dev/null)
  if [ -n "$issue_json" ]; then
    parsed=$(printf '%s' "$issue_json" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
title=data.get("title") or ""
body=data.get("body") or ""
text=(title + "\n" + body).strip()
sys.stdout.write(text)
')
    if [ -n "$parsed" ]; then
      QUERY=$parsed
    fi
  fi
fi

if [ -z "$QUERY" ]; then
  QUERY="issue ${ISSUE_NUMBER:-} ${ISSUE_LABEL:-} ${GITHUB_REPO:-}"
  QUERY=$(printf '%s' "$QUERY" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
fi

if [ -z "$QUERY" ]; then
  echo "Retrieval query is empty; skipping."
  exit 0
fi

OUT="/rag/retrieved_code_context.md"
mkdir -p /rag
rm -f "$OUT"

QURL="${QDRANT_URL:-http://qdrant.ai-dev.svc.cluster.local:6333}"
LIMIT="${RAG_TOP_K:-5}"
REPO_FILTER="${RAG_REPO:-${GITHUB_REPO:-}}"

echo "Querying $QURL (top_k=$LIMIT, repo=${REPO_FILTER:-all})..."
if [ -n "$REPO_FILTER" ]; then
  python3 /app/index_code.py \
    --query "$QUERY" \
    --qdrant-url "$QURL" \
    --limit "$LIMIT" \
    --repo "$REPO_FILTER" \
    --output "$OUT"
else
  python3 /app/index_code.py \
    --query "$QUERY" \
    --qdrant-url "$QURL" \
    --limit "$LIMIT" \
    --output "$OUT"
fi

if [ -s "$OUT" ]; then
  echo "Wrote retrieved context to $OUT"
else
  echo "No code context retrieved (continuing without RAG)."
  rm -f "$OUT"
fi
exit 0
