# GPU Time-Slicing Setup - Summary

## What I Found

Your K3s cluster has an **excellent GPU time-slicing setup** already configured! Here's what I discovered:

### Existing GPU Infrastructure

1. **NVIDIA Device Plugin with Time-Slicing**
   - Configured with **8 time-sliced replicas**
   - Resource: `nvidia.com/gpu.shared` (original `nvidia.com/gpu` renamed)
   - Location: `kube-system/nvidia-device-plugin-config`

2. **GPU Webhook System**
   - MutatingWebhookConfiguration: `gpu-directory-injector`
   - Service: `gpu-webhook` in `gpu-system` namespace
   - Automatically injects GPU device mounts and runtime class
   - Triggered by annotation: `gpu-directories: nvidia`

3. **NVIDIA Runtime**
   - RuntimeClass: `nvidia` configured
   - Used by Plex and AI workloads

### Current GPU Usage (Before AI-dev)

| Workload | Namespace | GPU Shares | Method |
|----------|-----------|------------|--------|
| Plex | media | N/A | Webhook only (no resource limits) |
| Ollama | ai | 1/8 (12.5%) | Hybrid (webhook + resources) |
| TTS | ai | 1/8 (12.5%) | Time-sliced resources |
| Whisper | ai | 2/8 (25%) | Time-sliced resources |
| **Available** | - | **4/8 (50%)** | - |

## What I Updated

### 1. vLLM Deployment Configuration

**File**: `ai-dev/vllm/vllm-deployment.yaml`

**Changes Made**:

✅ **Node Selector** (line 29-30)
```yaml
# OLD:
nodeSelector:
  gpu.vendor: nvidia

# NEW:
nodeSelector:
  kubernetes.io/hostname: homelabai
```
*Reason*: Your cluster uses hostname-based selection, not vendor labels

✅ **Runtime Class** (line 33)
```yaml
# ADDED:
runtimeClassName: nvidia
```
*Reason*: Ensures NVIDIA container runtime (may be set by webhook, but explicit is safer)

✅ **Removed Manual Tolerations** (lines 34-36)
```yaml
# REMOVED (no longer needed):
tolerations:
- key: nvidia.com/gpu
  operator: Exists
  effect: NoSchedule
```
*Reason*: Your cluster doesn't use GPU taints; webhook/runtime handles scheduling

✅ **NVIDIA Environment Variables** (lines 136-139)
```yaml
# ADDED:
- name: NVIDIA_VISIBLE_DEVICES
  value: "all"
- name: NVIDIA_DRIVER_CAPABILITIES
  value: "compute,utility"
```
*Reason*: Required for NVIDIA container runtime to expose GPU devices

✅ **GPU Resource Allocation** (lines 153, 157)
```yaml
# OLD:
nvidia.com/gpu.shared: "1"  # Time-sliced GPU sharing with Plex

# NEW:
nvidia.com/gpu.shared: "2"  # Time-sliced GPU (2/8 shares, others: Ollama=1, TTS=1, Whisper=2)
```
*Reason*: vLLM is more demanding; allocated 2 shares (25% GPU time) instead of 1

✅ **Kept GPU Webhook Annotation** (line 26)
```yaml
annotations:
  gpu-directories: nvidia
```
*Reason*: Already correct! Triggers webhook to inject GPU device mounts

### 2. New Documentation

**File**: `ai-dev/GPU_TIMESLICING.md`

Created comprehensive documentation covering:
- How GPU time-slicing works in your cluster
- Three GPU access methods (webhook, resources, hybrid)
- Current and projected GPU allocation
- Memory vs. compute time considerations
- Monitoring commands
- Tuning guide
- Troubleshooting

**File**: `ai-dev/README.md`

Updated vLLM component description to reference GPU time-slicing docs

## GPU Allocation After AI-dev Deployment

| Workload | Namespace | GPU Shares | Percentage | Notes |
|----------|-----------|------------|------------|-------|
| Plex | media | N/A | Variable | Transcoding (no resource limits) |
| Ollama | ai | 1 | 12.5% | LLM inference |
| TTS | ai | 1 | 12.5% | Text-to-speech |
| Whisper | ai | 2 | 25% | Speech recognition |
| **vLLM (AI-dev)** | **ai-dev** | **2** | **25%** | **Code LLM** |
| **Available** | - | **2** | **25%** | For future workloads |
| **Total** | - | **8** | **100%** | - |

## Why This Works

Your setup uses a **hybrid approach** (same as Ollama):

1. **GPU Webhook** (`gpu-directories: nvidia`)
   - Automatically injects `/dev/nvidia*` device mounts
   - Sets `runtimeClassName: nvidia`
   - No manual device configuration needed

2. **Time-Sliced Resources** (`nvidia.com/gpu.shared: "2"`)
   - Scheduler enforces fair GPU time allocation
   - Prevents over-subscription
   - vLLM gets guaranteed 25% GPU time

3. **NVIDIA Container Runtime**
   - Exposes GPU to containers
   - Handles GPU device access

## Memory Considerations

**IMPORTANT**: Time-slicing shares **compute time**, not **memory**!

- **Total VRAM**: 12GB (3080 Ti)
- **vLLM Setting**: `GPU_MEMORY_UTILIZATION: "0.70"` limits to ~8.4GB
- **Leaves**: ~3.6GB for Plex, Ollama, Whisper, TTS

**If all run simultaneously**:
- Potential: 18-24GB needed
- Available: 12GB
- **Mitigation**: Not all active at once, conservative vLLM limit

**If Plex has issues**: Reduce vLLM to `0.60` or `0.50`

## Verification Steps

After deployment, verify GPU setup:

```bash
# 1. Check vLLM pod GPU allocation
kubectl describe pod -n ai-dev -l app=vllm | grep nvidia.com/gpu.shared

# 2. Verify GPU devices are mounted
kubectl exec -n ai-dev -l app=vllm -- ls -l /dev/nvidia*

# 3. Check nvidia-smi works
kubectl exec -n ai-dev -l app=vllm -- nvidia-smi

# 4. Verify node GPU allocation
kubectl describe node homelabai | grep -A 10 "Allocated resources"

# 5. CRITICAL: Check Plex health
bash scripts/check-plex-health.sh
```

## Expected Behavior

✅ **Working Correctly**:
- vLLM pod starts successfully
- `nvidia-smi` shows GPU in vLLM pod
- vLLM API responds to inference requests
- Plex can still transcode
- No CUDA OOM errors
- GPU shares show 6/8 allocated (Ollama=1, TTS=1, Whisper=2, vLLM=2)

❌ **Problems to Watch For**:
- vLLM pod stuck in Pending (not enough GPU shares)
- CUDA OOM errors (reduce memory utilization)
- Plex transcoding fails (reduce vLLM memory)
- GPU not visible (webhook issue)

## Key Files Changed

1. `ai-dev/vllm/vllm-deployment.yaml` - Updated GPU configuration
2. `ai-dev/GPU_TIMESLICING.md` - New comprehensive GPU documentation
3. `ai-dev/README.md` - Added GPU sharing details

## Configuration Philosophy

Your AI-dev system now follows the **same pattern as Ollama**:
- Hybrid GPU access (webhook + resources)
- Time-sliced sharing with fair allocation
- Conservative memory limits
- Plex-aware configuration

This ensures:
- ✅ Fair GPU time allocation
- ✅ Scheduler enforcement
- ✅ Automatic device mounting
- ✅ Plex compatibility
- ✅ Room for growth (2 shares still available)

## Next Steps

1. **Deploy** the system using the updated manifests
2. **Verify** GPU allocation after deployment
3. **Monitor** Plex health (run `scripts/check-plex-health.sh`)
4. **Tune** if needed:
   - Increase vLLM shares if performance is poor (and shares available)
   - Decrease memory utilization if Plex has issues
   - Adjust based on actual usage patterns

## Questions?

See the detailed documentation:
- **GPU Time-Slicing**: [GPU_TIMESLICING.md](GPU_TIMESLICING.md)
- **Full Setup**: [README.md](README.md)
- **Quick Start**: [QUICKSTART.md](QUICKSTART.md)
- **Deployment**: [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)

Your GPU infrastructure is **production-ready** and **well-designed**! The AI-dev system integrates seamlessly with your existing setup. 🚀
