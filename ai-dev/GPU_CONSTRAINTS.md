# GPU Coexistence Constraints: Hard vs Soft

This document defines how AI-dev shares the NVIDIA GPU with **Plex** and other
workloads, and which rules are **hard-enforced** vs **advisory (soft)**.

Related:

- [GPU_TIMESLICING.md](GPU_TIMESLICING.md) — time-slicing setup and tuning
- [scripts/check-gpu-admission.sh](scripts/check-gpu-admission.sh) — pre-deploy hard gate
- [scripts/check-plex-health.sh](scripts/check-plex-health.sh) — post-deploy Plex health
- [scheduling/priority-classes.yaml](scheduling/priority-classes.yaml) — PriorityClasses

## Why this exists

Historically, scripts only *advised* operators when Plex/GPU looked unhealthy.
The scheduler and deploy path did not hard-stop AI workloads when the GPU was
busy or Plex was down. That made coexistence failures easy to miss until
transcoding broke.

Issue #4: **GPU conflict with Plex is advisory only** — resolved by documenting
and enforcing the split below.

## Hard constraints (enforced)

These **must** pass. Deploy and admission **fail closed** (non-zero exit) unless
an explicit break-glass override is used.

| Constraint | Enforcement | Failure mode when violated |
|------------|-------------|----------------------------|
| GPU node exists and is Ready | `check-gpu-admission.sh` (exit **30**) | Deploy aborts; no new GPU consumers while node is unhealthy |
| Requested `nvidia.com/gpu.shared` ≤ allocatable | Scheduler + admission (exit **11**) | Pod unschedulable / admission denied |
| Free GPU shares ≥ required (default 2 for vLLM) | `check-gpu-admission.sh` (exit **10**) when free shares are measurable | **GPU busy** — explicit deny before apply |
| Plex Running when present | `check-gpu-admission.sh` (exit **20**); post-deploy `check-plex-health.sh` fails deploy | No new AI GPU load while media is broken |
| `priorityClassName: ai-dev-gpu` on vLLM | Manifest + PriorityClass objects | Pod rejected if class missing (apply scheduling first) |
| Priority ordering Plex > AI | PriorityClasses `plex-media-critical` (1_000_000) vs `ai-dev-gpu` (1000) | Under preemption/pressure, Plex is preferred |
| Explicit resource requests on AI GPU pods | vLLM `nvidia.com/gpu.shared: "2"` | Scheduler will not place AI pods without free shares |

### Hard gate: pre-deploy

```bash
# Default: hard fail if GPU busy or Plex unhealthy
bash ai-dev/scripts/check-gpu-admission.sh

# Used automatically by deploy-safe.sh before Phase 3 (vLLM)
```

### Hard gate: post-deploy Plex

```bash
bash ai-dev/scripts/check-plex-health.sh
# deploy-safe.sh treats failure as deploy failure (no silent continue)
```

### Break-glass (opt-in only)

Silent override is **not** supported. Both flags are required:

```bash
ALLOW_GPU_OVERRIDE=1 FORCE_GPU_ADMISSION=1 bash ai-dev/scripts/check-gpu-admission.sh
```

Use only during maintenance when you accept risk to Plex.

## Soft constraints (advisory)

These improve safety and quality but are **not** fully automatable or are
intentionally non-blocking.

| Constraint | Why soft | Operator action |
|------------|----------|-----------------|
| VRAM headroom (`GPU_MEMORY_UTILIZATION` ≤ 0.70) | Memory is not virtualized by time-slicing; runtime OOM is workload-dependent | Lower utilization if Plex stutters |
| Peak Plex viewing windows | No cluster signal for “someone started a movie” | Deploy AI off-peak; scale vLLM down if needed |
| Transcode quality / stutter | Subjective; logs may not show “bad stream” | Manual smoke test after GPU deploys |
| Free-share parse uncertainty | Some clusters make allocated shares hard to parse | Admission still enforces node/Plex/capacity; inspect node manually |
| Non-AI GPU users (Ollama, Whisper, TTS) outside ai-dev | Managed in other namespaces | Coordinate share budgets cluster-wide |

Soft checks may print **warnings** (yellow) without failing admission.

## Priority classes and node rules (optional but recommended)

Manifests: `ai-dev/scheduling/priority-classes.yaml` (cluster-scoped).

| Class | Value | Preemption | Use on |
|-------|-------|------------|--------|
| `plex-media-critical` | 1000000 | Never | Plex / critical media pods (`media` namespace) |
| `ai-dev-gpu` | 1000 | PreemptLowerPriority | vLLM and other AI-dev GPU consumers |

**Plex** is managed outside this repo. To fully protect it:

```yaml
# On the Plex Deployment pod template:
spec:
  priorityClassName: plex-media-critical
```

**Node rules already used (hard for placement):**

- `nodeSelector: kubernetes.io/hostname: homelabai` — AI GPU only on the GPU node
- `runtimeClassName: nvidia` + `gpu-directories: nvidia` — device access
- Time-sliced resource name `nvidia.com/gpu.shared` — share accounting

No GPU taints are assumed on this cluster (see GPU_SETUP_SUMMARY.md).

## Explicit failure modes when GPU is busy

| Situation | What you see | Exit / effect |
|-----------|--------------|---------------|
| Not enough free shares | `ADMISSION DENIED (10): Only N free GPU share(s)...` | Exit **10**, deploy stops |
| Request > capacity | `ADMISSION DENIED (11): Required shares exceed allocatable` | Exit **11** |
| Plex not Running | `ADMISSION DENIED (20): Plex phase is '...'` | Exit **20** |
| Node missing / NotReady | `ADMISSION DENIED (30): GPU node ...` | Exit **30** |
| Cannot read capacity | `ADMISSION DENIED (31): Could not read nvidia.com/gpu.shared...` | Exit **31** |
| Pod scheduled but pending | `kubectl describe pod` → Insufficient nvidia.com/gpu.shared | Scheduler hard reject |
| Post-deploy Plex broken | `check-plex-health.sh` fails | deploy-safe aborts; rollback hints printed |

### Recovery when GPU is busy

```bash
# 1. See who holds shares
kubectl describe node homelabai | sed -n '/Allocated resources:/,/Events:/p'

# 2. Free AI GPU (do not scale Plex)
kubectl scale deployment -n ai-dev vllm-server --replicas=0

# 3. Re-run admission
bash ai-dev/scripts/check-gpu-admission.sh

# 4. Redeploy when free shares >= 2
```

## Mapping to deploy path

```
deploy-safe.sh
  ├── Pre-checks (node present)
  ├── Phases 1–2 (no GPU)
  ├── check-gpu-admission.sh     ← HARD: refuse Phase 3 if GPU busy / Plex down
  ├── Apply PriorityClasses
  ├── Apply vLLM (priorityClassName + gpu.shared requests)
  └── check-plex-health.sh       ← HARD: fail deploy if Plex unhealthy after GPU
```

## Summary

- **Hard**: capacity, free shares, node readiness, Plex Running, PriorityClass + scheduler resources, fail-closed deploy.
- **Soft**: VRAM tuning, peak hours, subjective quality, cross-namespace AI share etiquette.
- **GPU busy** always surfaces as an **explicit** admission or scheduler failure — never a silent warning-only path in the default deploy.
