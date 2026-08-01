# Config-Driven Model Hot-Swap

Change the vLLM model (or runtime knobs) **without redeploying the full AI-Dev stack**.

Only the `vllm-config` ConfigMap and the `vllm-server` Deployment are updated. Qdrant, code-indexer, SWE-agent, ingress, and PVC *objects* stay as-is. Model weights on the `model-storage` PVC are reused when the path already exists.

| Acceptance item | Where |
|-----------------|--------|
| Model id/config in ConfigMap | [`vllm/vllm-configmap.yaml`](vllm/vllm-configmap.yaml) |
| Documented rollout procedure | This doc + [`scripts/swap-model.sh`](scripts/swap-model.sh) |
| Health checks after model change | [`scripts/check-model-health.sh`](scripts/check-model-health.sh) |
| Notes on disk/PVC model cache | [PVC model cache](#pvc-model-cache) below |

---

## Architecture (why a restart is still required)

Kubernetes injects ConfigMap keys as environment variables **only at pod start**. vLLM also loads weights at process start. Therefore a model swap is:

1. Update ConfigMap keys (`MODEL_NAME`, `MODEL_PATH`, `SERVED_MODEL_NAME`, …)
2. Restart **only** `deployment/vllm-server` (Recreate strategy — short downtime; single GPU + RWO PVC)
3. Init container downloads into PVC if `MODEL_PATH` is empty, otherwise **cache hit**
4. Run post-swap health checks

This is a **config-driven hot-swap of the inference workload**, not a full namespace/`kubectl apply -k` redeploy.

```
┌────────────────────┐     apply/patch      ┌─────────────────┐
│ vllm-config        │ ──────────────────▶  │ vllm-server     │
│  MODEL_NAME        │   rollout restart    │  init: download │
│  MODEL_PATH        │                      │  main: vLLM     │
│  SERVED_MODEL_NAME │                      └────────┬────────┘
│  QUANTIZATION …    │                               │
└────────────────────┘                               ▼
                                          PVC model-storage
                                          /models/<model-path>/
```

---

## ConfigMap keys (model identity)

Edit [`vllm/vllm-configmap.yaml`](vllm/vllm-configmap.yaml) (git source of truth) or patch live:

| Key | Purpose |
|-----|---------|
| `MODEL_NAME` | HuggingFace repo id for the downloader (e.g. `TheBloke/deepseek-coder-6.7B-instruct-AWQ`) |
| `MODEL_PATH` | Absolute path on the PVC (e.g. `/models/deepseek-coder-6.7b-instruct-awq`) |
| `MODEL_REVISION` | HF revision/tag/commit (default `main`) |
| `SERVED_MODEL_NAME` | OpenAI API model id clients must send |
| `QUANTIZATION` | `awq`, `gptq`, or adjust with model type |
| `GPU_MEMORY_UTILIZATION` | Fraction of VRAM for vLLM (leave headroom for Plex) |
| `MAX_MODEL_LEN` | Context length |
| `DTYPE`, `MAX_NUM_SEQS`, … | Other runtime settings |

The Deployment already wires these keys via `configMapKeyRef` — no Deployment YAML edits are required for a normal model swap.

---

## Rollout procedure

### Option A — Script (recommended)

From the repo root (cluster credentials required):

```bash
# Use git-tracked ConfigMap as-is and restart vLLM only
bash ai-dev/scripts/swap-model.sh

# Or override identity from CLI (patches ConfigMap after apply)
bash ai-dev/scripts/swap-model.sh \
  --model-name "TheBloke/Qwen2.5-Coder-7B-Instruct-AWQ" \
  --model-path "/models/qwen2.5-coder-7b-instruct-awq" \
  --served-name "qwen2.5-coder-7b-instruct" \
  --quantization "awq"

# Plan only
bash ai-dev/scripts/swap-model.sh --dry-run

# Patch live keys without re-applying the YAML file
bash ai-dev/scripts/swap-model.sh --no-apply \
  --model-name "..." --model-path "..." --served-name "..."
```

The script:

1. Applies `vllm/vllm-configmap.yaml` (unless `--no-apply`)
2. Patches any CLI overrides
3. `kubectl rollout restart deployment/vllm-server -n ai-dev`
4. Waits for rollout
5. Runs `check-model-health.sh`

### Option B — Manual

```bash
# 1. Edit source of truth
$EDITOR ai-dev/vllm/vllm-configmap.yaml

# 2. Apply ConfigMap only (not the whole stack)
kubectl apply -f ai-dev/vllm/vllm-configmap.yaml

# 3. Restart only vLLM (ConfigMap env is loaded at pod start)
kubectl rollout restart deployment/vllm-server -n ai-dev
kubectl rollout status deployment/vllm-server -n ai-dev --timeout=600s

# 4. Health checks
bash ai-dev/scripts/check-model-health.sh
```

### Option C — Live edit (emergency / lab)

```bash
kubectl edit configmap vllm-config -n ai-dev
kubectl rollout restart deployment/vllm-server -n ai-dev
bash ai-dev/scripts/check-model-health.sh
```

Prefer Option A/B so git remains the source of truth.

---

## Health checks after model change

`scripts/check-model-health.sh` verifies:

| Check | Pass criteria |
|-------|----------------|
| Deployment rollout | `kubectl rollout status` succeeds |
| Pod Ready | `kubectl wait --for=condition=ready` |
| `GET /health` | HTTP success via port-forward |
| `GET /v1/models` | Includes `SERVED_MODEL_NAME` from ConfigMap |
| `POST /v1/completions` | Smoke generation (weights loaded) |
| Plex co-tenant | Optional; runs `check-plex-health.sh` (non-fatal) |

```bash
# Defaults: namespace=ai-dev, expects SERVED_MODEL_NAME from live ConfigMap
bash ai-dev/scripts/check-model-health.sh

# Custom
EXPECTED_MODEL=deepseek-coder-6.7b-instruct TIMEOUT=900 \
  bash ai-dev/scripts/check-model-health.sh

# Skip completion or Plex
SKIP_COMPLETION=1 CHECK_PLEX=0 bash ai-dev/scripts/check-model-health.sh
```

Also useful:

```bash
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000
python3 ai-dev/scripts/test-vllm-api.py --url http://localhost:8000 \
  --model deepseek-coder-6.7b-instruct
```

---

## PVC model cache

| Item | Detail |
|------|--------|
| PVC | `model-storage` (see [`storage/pvcs.yaml`](storage/pvcs.yaml)) |
| Size | 50Gi (default) — size for **multiple** quantized models |
| Mount | `/models` on init + main containers |
| HF cache | `HF_HOME=/models/.cache` |
| Layout | One directory per `MODEL_PATH`, e.g. `/models/deepseek-coder-6.7b-instruct-awq` |

### Cache behavior

- Init container **skips download** if `MODEL_PATH` exists and is non-empty (**cache hit**).
- Switching to a **new** `MODEL_PATH` downloads only that path; previous model dirs remain.
- Switching **back** to a previous `MODEL_PATH` is fast (no re-download).
- Old models are **never deleted automatically** — free space is operator-managed.

### Inspect and reclaim space

```bash
# List cached model directories
kubectl exec -n ai-dev deploy/vllm-server -c vllm -- ls -lah /models

# Disk usage on the volume
kubectl exec -n ai-dev deploy/vllm-server -c vllm -- df -h /models

# Remove an unused model tree (irreversible — next swap to that path re-downloads)
kubectl exec -n ai-dev deploy/vllm-server -c vllm -- \
  rm -rf /models/some-old-model-dir
```

### Capacity planning

| Model class (approx.) | Disk per copy |
|------------------------|---------------|
| 7B 4-bit AWQ/GPTQ | ~4–6 GiB |
| 7B FP16 | ~14 GiB |
| HF hub cache (partial) | extra under `/models/.cache` |

Keep at least one free slot for the next download. If the PVC fills during init, the pod stays in `Init:Error` / `CrashLoop` until space is freed or the PVC is expanded.

### Expanding the PVC

```bash
kubectl patch pvc model-storage -n ai-dev \
  --type merge -p '{"spec":{"resources":{"requests":{"storage":"80Gi"}}}}'
# Requires StorageClass that allows volume expansion (Longhorn does).
```

Also update `storage/pvcs.yaml` in git so future applies match.

---

## Client / SWE-agent alignment

After changing `SERVED_MODEL_NAME`, update consumers:

- Cline / OpenAI clients: set `model` to the new served name
- SWE-agent: `swe-agent/configmap.yaml` → `model.model_name`, then restart SWE-agent if needed

```bash
kubectl apply -f ai-dev/swe-agent/configmap.yaml
kubectl rollout restart deployment -n ai-dev -l app=swe-agent  # if deployed
```

---

## Rollback

```bash
# Git-based: restore previous ConfigMap values and swap again
git checkout HEAD~1 -- ai-dev/vllm/vllm-configmap.yaml
bash ai-dev/scripts/swap-model.sh

# Or point MODEL_PATH back at a still-cached directory (fastest)
bash ai-dev/scripts/swap-model.sh --no-apply \
  --model-name "TheBloke/deepseek-coder-6.7B-instruct-AWQ" \
  --model-path "/models/deepseek-coder-6.7b-instruct-awq" \
  --served-name "deepseek-coder-6.7b-instruct" \
  --quantization "awq"
```

Deployment history (pod template only — does not restore ConfigMap):

```bash
kubectl rollout undo deployment/vllm-server -n ai-dev
```

Always re-align ConfigMap after a Deployment-only undo.

---

## What this does *not* do

- Does **not** live-reload weights inside a running vLLM process
- Does **not** RollingUpdate with zero downtime (GPU + RWO → `Recreate`)
- Does **not** redeploy Qdrant, indexer, ingress, or recreate PVCs
- Does **not** auto-prune old model directories on the PVC

---

## Makefile helpers

```bash
make swap-model          # bash ai-dev/scripts/swap-model.sh
make check-model-health  # bash ai-dev/scripts/check-model-health.sh
```
