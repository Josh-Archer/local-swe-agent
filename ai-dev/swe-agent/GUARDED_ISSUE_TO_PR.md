# Guarded Issue → PR Demo Workflow

End-to-end demo: **labeled (or numbered) GitHub issue → SWE-agent Job → reviewed PR**, with guardrails:

| Guardrail | Default | Purpose |
|-----------|---------|---------|
| Path allowlist | `ai-dev/,scripts/,docs/,*.md` | Agent may only touch agreed paths |
| Max changed files | `10` | Keeps demos reviewable |
| No force-push | always off | Protects shared branches |
| Human approval gate | `OPEN_PR=false` until approved | No silent merges/PRs |

## Prerequisites

1. Cluster components deployed (`ai-dev` namespace, vLLM optional for dry demo of manifests).
2. GitHub token secret:

```bash
kubectl create secret generic swe-agent-secrets \
  --from-literal=github-token=ghp_YourTokenHere \
  -n ai-dev
```

3. SWE-agent ConfigMap applied (includes guardrail scripts):

```bash
kubectl apply -f ai-dev/swe-agent/configmap.yaml
```

## Human approval gate

The demo **never** opens a PR unless a human opts in after review.

1. **First run** — agent works the issue, may push a feature branch, **does not** open a PR:
   - `OPEN_PR=false` (default)
   - `REQUIRE_HUMAN_APPROVAL=true` (default)
2. **Review** — inspect logs, diff, and guardrail output (`GUARDRAILS PASSED`).
3. **Approve** — either:
   - Open the PR yourself (`gh pr create ...`), or
   - Re-run the job with `--open-pr --approved` (sets `OPEN_PR=true` and `HUMAN_APPROVED=true`).

If `OPEN_PR=true` without `HUMAN_APPROVED=true`, the wrapper **forces** `OPEN_PR=false` and prints a warning.

## Quick start (recommended)

From the repo root (or `ai-dev/`):

```bash
# Issue by number
bash ai-dev/scripts/run-guarded-issue-job.sh \
  --repo OWNER/REPO \
  --issue 123

# Issue by label (first open issue with that label)
bash ai-dev/scripts/run-guarded-issue-job.sh \
  --repo OWNER/REPO \
  --label ai-fix

# Preview manifest only
bash ai-dev/scripts/run-guarded-issue-job.sh \
  --repo OWNER/REPO \
  --issue 123 \
  --dry-run
```

Monitor:

```bash
kubectl logs -n ai-dev -f job/swe-agent-issue-123
```

After review, open a PR:

```bash
# Option A: manual
gh pr create --repo OWNER/REPO --fill

# Option B: re-run agent with approval to open PR
bash ai-dev/scripts/run-guarded-issue-job.sh \
  --repo OWNER/REPO \
  --issue 123 \
  --open-pr \
  --approved
```

## Job template inputs

`ai-dev/swe-agent/job-template.yaml` accepts:

| Env / placeholder | Description |
|-------------------|-------------|
| `ISSUE_NUMBER` | GitHub issue number |
| `ISSUE_LABEL` | Label used to pick an open issue |
| `GITHUB_OWNER` / `GITHUB_REPO` | Repository coordinates |
| `ISSUE_URL` | Full issue URL (optional if number+owner/repo set) |
| `PATH_ALLOWLIST` | Comma-separated path prefixes/globs |
| `MAX_CHANGED_FILES` | Integer cap on files touched |
| `ALLOW_FORCE_PUSH` | Must stay `false` for the demo |
| `OPEN_PR` | `true` only after human approval |
| `REQUIRE_HUMAN_APPROVAL` | When `true`, blocks `OPEN_PR` without approval |
| `HUMAN_APPROVED` | Set by launcher with `--approved` |

## Local guardrails check

You can validate any clone/worktree without the cluster:

```bash
# In a repo with changes vs origin/master
PATH_ALLOWLIST='ai-dev/,scripts/,docs/,*.md' \
MAX_CHANGED_FILES=10 \
ALLOW_FORCE_PUSH=false \
  bash ai-dev/scripts/guardrails-check.sh --repo-dir . --base origin/master
```

Exit code `0` = passed; `1` = failed (when `ENFORCE_GUARDRAILS=true`).

## Example walkthrough

### 1. Label an issue for the agent

On GitHub, add label `ai-fix` to a small, scoped issue (e.g. docs typo under `ai-dev/`).

### 2. Launch the guarded job

```bash
bash ai-dev/scripts/run-guarded-issue-job.sh \
  --repo Josh-Archer/local-swe-agent \
  --label ai-fix \
  --allowlist 'ai-dev/,*.md' \
  --max-files 5
```

### 3. Watch progress

```bash
kubectl get jobs -n ai-dev -l guarded=true
kubectl logs -n ai-dev -f job/swe-agent-issue-label-ai-fix
```

Look for:

```text
=== Guardrails check ===
OK:   file count within limit
OK:   allowlisted: ai-dev/...
OK:   force-push disabled
=== GUARDRAILS PASSED ===
=== Human approval gate ===
Agent completed without opening a PR ...
```

### 4. Review

- Confirm only allowlisted paths changed.
- Confirm file count ≤ max.
- Confirm the agent did not force-push.

### 5. Open the PR (human)

```bash
gh pr create --repo Josh-Archer/local-swe-agent \
  --title "fix: <summary>" \
  --body "Closes #<n>"
```

Or re-run with approval flags as shown above.

### 6. Do **not** merge from the agent

Merging remains a human action (branch protection / review). This demo only automates the **proposal** path under guardrails.

## Failure modes

| Symptom | Likely cause |
|---------|----------------|
| `path not allowlisted` | Change outside `PATH_ALLOWLIST` |
| `exceeds MAX_CHANGED_FILES` | Scope too large — split the issue |
| `force-push requested` | `ALLOW_FORCE_PUSH=false` blocked a force push |
| `OPEN_PR` forced false | Missing `HUMAN_APPROVED=true` |
| No issue for label | No **open** issues with that label |

## Related files

- `ai-dev/swe-agent/job-template.yaml` — Job template (number/label + guardrail env)
- `ai-dev/swe-agent/configmap.yaml` — Agent config, `run-swe-agent.sh`, embedded checker
- `ai-dev/scripts/run-guarded-issue-job.sh` — Launcher
- `ai-dev/scripts/guardrails-check.sh` — Standalone path/max-files/force-push checks
