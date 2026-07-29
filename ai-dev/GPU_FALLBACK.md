# Non-GPU Fallback (homelabai drained / Plex priority)

When the GPU node (`homelabai`) is unavailable, cordoned, or must prioritize media workloads (Plex transcoding), AI-dev can **disable the local GPU path** and route SWE-agent (and other OpenAI-compatible clients) to a **remote or CPU-side OpenAI-compatible endpoint**.

## Why this exists

- **Plex is sacred** — media GPU time and VRAM take priority over coding LLM inference.
- **Draining `homelabai`** (maintenance, cordon, or emergency GPU release) would otherwise leave SWE-agent pointing at a dead `vllm-server`.
- **Config-driven fallback** keeps a single source of truth (`llm-endpoint-config`) instead of hand-editing multiple Deployments.

Related docs: [GPU_TIMESLICING.md](GPU_TIMESLICING.md), [SAFE_DEPLOYMENT_GUIDE.md](SAFE_DEPLOYMENT_GUIDE.md).

## Architecture

```
                    ┌─────────────────────────────┐
  LLM_MODE=gpu      │  vLLM on homelabai (GPU)    │
  ─────────────────▶│  vllm-server:8000/v1        │
                    └─────────────────────────────┘
  SWE-agent / clients
  read ACTIVE_BASE_URL
  from llm-endpoint-config
                    ┌─────────────────────────────┐
  LLM_MODE=fallback │  Remote OpenAI-compatible   │
  ─────────────────▶│  e.g. api.openai.com/v1     │
                    │  or any CPU/gateway base URL│
                    └─────────────────────────────┘
```

| Mode | `LLM_MODE` | Active endpoint | vLLM replicas (default) | GPU path status |
|------|------------|-----------------|-------------------------|-----------------|
| Local GPU | `gpu` | `GPU_BASE_URL` (in-cluster vLLM) | 1 | **ENABLED** |
| Non-GPU fallback | `fallback` | `FALLBACK_BASE_URL` | 0 | **DISABLED** |

## ConfigMap: `llm-endpoint-config`

File: [`vllm/llm-endpoint-configmap.yaml`](vllm/llm-endpoint-configmap.yaml)

| Key | Purpose |
|-----|---------|
| `LLM_MODE` | `gpu` or `fallback` |
| `STATUS_MESSAGE` | Human-readable status (printed by `llm-mode.sh status` and SWE-agent jobs) |
| `ACTIVE_BASE_URL` / `ACTIVE_MODEL` | What clients should use **now** |
| `GPU_BASE_URL` / `GPU_MODEL` | Local vLLM defaults |
| `FALLBACK_BASE_URL` / `FALLBACK_MODEL` | Remote/CPU OpenAI-compatible defaults |
| `SCALE_DOWN_VLLM_ON_FALLBACK` | `true` → scale `vllm-server` to 0 on fallback |
| `LAST_REASON` / `LAST_CHANGED_AT` | Audit trail for the last mode switch |

SWE-agent Deployment and Job template inject `LM_BASE_URL`, `LM_MODEL`, `LLM_MODE`, and `LLM_STATUS_MESSAGE` from this ConfigMap. The `run-swe-agent.sh` wrapper rewrites `config.yaml` at runtime and prints clear GPU-path status.

## Operator commands

Script: [`scripts/llm-mode.sh`](scripts/llm-mode.sh)

```bash
# Inspect mode, active URL, vLLM replicas, homelabai cordon state
bash ai-dev/scripts/llm-mode.sh status

# Free GPU for Plex / when homelabai is drained
bash ai-dev/scripts/llm-mode.sh fallback \
  --reason "Plex priority / homelabai drained" \
  --url "https://api.openai.com/v1" \
  --model "gpt-4o-mini"

# Persist fallback URL without switching yet
bash ai-dev/scripts/llm-mode.sh set-fallback-url \
  --url "https://your-gateway.example/v1" --model "coder-cpu"

# Restore local GPU path after media load drops
bash ai-dev/scripts/llm-mode.sh gpu --reason "homelabai available"
```

Makefile shortcuts (if using repo root Makefile):

```bash
make llm-status
make llm-fallback REASON="Plex priority"
make llm-gpu
```

## Coexistence with media GPU workloads

### Normal sharing (both active)

Prefer **time-slicing** first (see [GPU_TIMESLICING.md](GPU_TIMESLICING.md)):

1. Keep vLLM at `nvidia.com/gpu.shared: "2"` and `GPU_MEMORY_UTILIZATION: "0.70"` (or lower).
2. Monitor Plex with `bash ai-dev/scripts/check-plex-health.sh`.
3. If Plex stutters, reduce memory utilization before full fallback.

### When to switch to fallback

Use **fallback** when any of these apply:

- `homelabai` is cordoned/drained for maintenance.
- Plex needs exclusive GPU (live event, heavy 4K transcode).
- Emergency: `kubectl scale deployment vllm-server --replicas=0 -n ai-dev` alone is not enough because clients still need a working base URL.

Recommended sequence for media priority:

```bash
# 1. Point AI clients at remote/CPU endpoint and scale down vLLM
bash ai-dev/scripts/llm-mode.sh fallback --reason "Plex exclusive"

# 2. Verify GPU path status is DISABLED
bash ai-dev/scripts/llm-mode.sh status

# 3. Confirm Plex health
bash ai-dev/scripts/check-plex-health.sh

# 4. Later: restore GPU path
bash ai-dev/scripts/llm-mode.sh gpu --reason "media load normal"
```

### Secrets for remote APIs

Local vLLM accepts a dummy key. Remote providers need a real key:

```bash
kubectl -n ai-dev create secret generic swe-agent-secrets \
  --from-literal=github-token="ghp_..." \
  --from-literal=openai-api-key="sk-..." \
  --dry-run=client -o yaml | kubectl apply -f -
```

SWE-agent mounts `openai-api-key` from `swe-agent-secrets` (optional). If the key is missing, local/dummy usage still works for pure vLLM.

### Ingress / IDE clients (Cline, Claude Code)

External clients using `https://code-llm.archer.casa` hit the **in-cluster Ingress → vLLM Service**. In fallback mode vLLM is scaled to 0, so that external path will fail health checks unless you:

- Point the IDE at the **remote** base URL directly, or
- Front a separate gateway Service that already implements fallback (out of scope here).

SWE-agent Jobs always follow `ACTIVE_BASE_URL` from the ConfigMap.

## Clear status when GPU path is disabled

`llm-mode.sh status` prints:

- `LLM_MODE`, `STATUS_MESSAGE`, active/fallback URLs
- **GPU path: DISABLED** (yellow) or **ENABLED** (green)
- vLLM ready/desired replicas and mismatch warnings
- Whether `homelabai` is unschedulable

SWE-agent job logs include the same status block at startup via `run-swe-agent.sh`.

## Acceptance mapping (issue #3)

| Criterion | Implementation |
|-----------|----------------|
| Config for remote/base URL fallback | `llm-endpoint-config` (`FALLBACK_BASE_URL`, `ACTIVE_BASE_URL`, …) |
| Clear status when GPU path disabled | `STATUS_MESSAGE` + `llm-mode.sh status` + job log banner |
| Docs for coexistence with media GPU | This document + emergency links in GPU_TIMESLICING / checklist |

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Status says fallback but vLLM still running | `SCALE_DOWN_VLLM_ON_FALLBACK` false, or scale failed — re-run `fallback` |
| Status says gpu but replicas 0 | Run `llm-mode.sh gpu` or `kubectl scale deployment vllm-server --replicas=1 -n ai-dev` |
| SWE-agent still hits old URL | Ensure ConfigMap applied; `rollout restart deployment/swe-agent-server -n ai-dev` |
| Remote 401 | Set `openai-api-key` on `swe-agent-secrets` |
| ConfigMap missing | `kubectl apply -f ai-dev/vllm/llm-endpoint-configmap.yaml` |
