# GPU Time-Slicing Configuration

This document explains how GPU time-slicing is configured in your cluster and how the AI-dev system uses it.

## Overview

Your K3s cluster uses **NVIDIA GPU time-slicing** to allow multiple workloads to share a single GPU (NVIDIA 3080 Ti on `homelabai` node). This enables Plex transcoding, Ollama, Whisper, TTS, and vLLM to all use the same GPU without conflicts.

## GPU Configuration

### NVIDIA Device Plugin

**Configuration**: `/kube-system/nvidia-device-plugin-config`

```yaml
version: v1
sharing:
  timeSlicing:
    renameByDefault: true
    failRequestsGreaterThanOne: false
    resources:
      - name: nvidia.com/gpu
        replicas: 8
```

**What this means**:
- The single physical GPU is divided into **8 time-sliced shares**
- Original resource `nvidia.com/gpu` is renamed to `nvidia.com/gpu.shared`
- Workloads can request 1, 2, or more shares (up to 8 total)
- Requests are scheduled using time-slicing (not MPS or vGPU)

### Available GPU Resources on homelabai

```
nvidia.com/gpu: 0          # Original resource (renamed)
nvidia.com/gpu.shared: 8   # Time-sliced shares
```

## GPU Access Methods

Your cluster supports **two complementary methods** for GPU access:

### Method 1: GPU Webhook + Runtime Class (Plex)

**Used by**: Plex transcoding

**How it works**:
1. Add annotation to pod template: `gpu-directories: nvidia`
2. GPU webhook (`gpu-directory-injector`) automatically:
   - Injects volume mounts for `/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm`
   - Sets `runtimeClassName: nvidia`
3. Pod gets full GPU access through NVIDIA runtime

**Pros**:
- Simple annotation-based configuration
- No manual device mounting needed
- Automatic runtime class setup

**Cons**:
- No time-slicing enforcement
- Relies on workloads being "well-behaved"

**Example** (Plex):
```yaml
metadata:
  annotations:
    gpu-directories: nvidia
spec:
  runtimeClassName: nvidia  # Set automatically by webhook
  containers:
  - name: plex
    env:
    - name: NVIDIA_VISIBLE_DEVICES
      value: "all"
    - name: NVIDIA_DRIVER_CAPABILITIES
      value: "compute,video,utility"
```

### Method 2: Time-Sliced Resource Requests (AI Workloads)

**Used by**: Ollama, Whisper, TTS, vLLM (AI-dev)

**How it works**:
1. Request GPU shares in resource limits/requests
2. Kubernetes scheduler enforces fair allocation
3. NVIDIA device plugin manages time-slicing

**Pros**:
- Scheduler-enforced allocation
- Fair sharing among workloads
- Prevents GPU over-subscription

**Cons**:
- Requires explicit resource requests
- Need to estimate share requirements

**Example** (Ollama):
```yaml
resources:
  requests:
    nvidia.com/gpu.shared: "1"
  limits:
    nvidia.com/gpu.shared: "1"
```

### Method 3: Hybrid Approach (Recommended)

**Used by**: Ollama, vLLM (AI-dev)

**Combines both methods** for best results:
- GPU webhook annotation for device mounting
- Time-sliced resource requests for fair scheduling
- Runtime class for NVIDIA container runtime

**Example** (vLLM in AI-dev):
```yaml
metadata:
  annotations:
    gpu-directories: nvidia  # Webhook injects devices
spec:
  nodeSelector:
    kubernetes.io/hostname: homelabai
  runtimeClassName: nvidia
  containers:
  - name: vllm
    env:
    - name: NVIDIA_VISIBLE_DEVICES
      value: "all"
    - name: NVIDIA_DRIVER_CAPABILITIES
      value: "compute,utility"
    resources:
      requests:
        nvidia.com/gpu.shared: "2"  # Time-sliced allocation
      limits:
        nvidia.com/gpu.shared: "2"
```

## Current GPU Allocation

### Before AI-dev Deployment

| Workload | Namespace | GPU Shares | Percentage |
|----------|-----------|------------|------------|
| Plex | media | N/A* | Variable |
| Ollama | ai | 1 | 12.5% |
| TTS | ai | 1 | 12.5% |
| Whisper | ai | 2 | 25% |
| **Available** | - | **4** | **50%** |
| **Total** | - | **8** | **100%** |

*Plex uses webhook method without explicit resource limits

### After AI-dev Deployment

| Workload | Namespace | GPU Shares | Percentage |
|----------|-----------|------------|------------|
| Plex | media | N/A* | Variable |
| Ollama | ai | 1 | 12.5% |
| TTS | ai | 1 | 12.5% |
| Whisper | ai | 2 | 25% |
| **vLLM** | **ai-dev** | **2** | **25%** |
| **Available** | - | **2** | **25%** |
| **Total** | - | **8** | **100%** |

## GPU Memory Considerations

While time-slicing handles **compute time**, **GPU memory is NOT virtualized**:

- **Total VRAM**: 12GB (3080 Ti)
- **Expected Usage**:
  - Plex transcoding: ~1-2GB (variable)
  - Ollama (when active): ~2-4GB
  - TTS: ~0.5-1GB
  - Whisper: ~2-3GB
  - vLLM (DeepSeek 6.7B 4-bit): ~8-10GB

**Key Setting**: vLLM's `GPU_MEMORY_UTILIZATION: "0.70"` limits vLLM to 70% of total VRAM (~8.4GB), leaving headroom for other workloads.

### Memory Over-Subscription Risk

If all workloads try to use GPU simultaneously:
- Potential total: 18-24GB
- Available: 12GB
- **Risk**: CUDA OOM errors

**Mitigation Strategies**:
1. vLLM uses conservative memory limit (70%)
2. Not all workloads are active simultaneously
3. Monitor with DCGM exporter
4. Adjust vLLM memory if Plex has issues

## Monitoring GPU Usage

### Check GPU Allocation

```bash
# View node capacity
kubectl describe node homelabai | grep nvidia

# List pods using GPU
kubectl get pods -A -o json | \
  grep -l "nvidia.com/gpu" | \
  xargs kubectl describe

# Check current GPU shares used
kubectl describe node homelabai | grep -A 10 "Allocated resources"
```

### Monitor GPU Memory and Utilization

```bash
# Via DCGM exporter (if deployed)
kubectl exec -n monitoring dcgm-exporter-xxxxx -- nvidia-smi

# From any GPU pod
kubectl exec -n ai-dev -l app=vllm -- nvidia-smi

# Watch GPU usage
kubectl exec -n ai-dev -l app=vllm -- watch nvidia-smi
```

### Prometheus Metrics (if available)

```promql
# GPU memory usage
DCGM_FI_DEV_FB_USED

# GPU utilization
DCGM_FI_DEV_GPU_UTIL

# GPU temperature
DCGM_FI_DEV_GPU_TEMP
```

## Tuning GPU Allocation

### Increasing vLLM GPU Shares

If vLLM needs more GPU time and shares are available:

```bash
# Edit deployment
kubectl edit deployment -n ai-dev vllm-server

# Change:
nvidia.com/gpu.shared: "2"  # to "3" or "4"
```

### Reducing vLLM GPU Memory

If Plex transcoding fails or stutters:

```bash
# Edit ConfigMap
kubectl edit configmap -n ai-dev vllm-config

# Change:
GPU_MEMORY_UTILIZATION: "0.70"  # to "0.60" or "0.50"

# Restart vLLM
kubectl rollout restart deployment -n ai-dev vllm-server
```

### Temporarily Freeing GPU for Plex

```bash
# Scale down vLLM (emergency)
kubectl scale deployment -n ai-dev vllm-server --replicas=0

# Scale back up
kubectl scale deployment -n ai-dev vllm-server --replicas=1
```

## GPU Webhook Details

### Webhook Configuration

```bash
# View webhook config
kubectl get mutatingwebhookconfiguration gpu-directory-injector -o yaml

# Check webhook pod
kubectl get pods -n gpu-system
kubectl logs -n gpu-system -l app=gpu-webhook
```

### What the Webhook Injects

When a pod has `gpu-directories: nvidia` annotation, the webhook adds:

**Volume Mounts**:
```yaml
volumeMounts:
- name: nvidia0
  mountPath: /dev/nvidia0
- name: nvidiactl
  mountPath: /dev/nvidiactl
- name: nvidia-uvm
  mountPath: /dev/nvidia-uvm
```

**Volumes**:
```yaml
volumes:
- name: nvidia0
  hostPath:
    path: /dev/nvidia0
- name: nvidiactl
  hostPath:
    path: /dev/nvidiactl
- name: nvidia-uvm
  hostPath:
    path: /dev/nvidia-uvm
```

**Runtime Class** (if not already set):
```yaml
runtimeClassName: nvidia
```

### Webhook Failure Policy

The webhook has `failurePolicy: Fail`:
- If webhook is unavailable, pod creation fails
- Ensures pods don't start without GPU access
- Check webhook health if deployments stuck in pending

## Troubleshooting

### vLLM Pod Can't Start

**Symptom**: Pod pending or CrashLoopBackOff

**Check**:
```bash
# Check GPU shares available
kubectl describe node homelabai | grep nvidia.com/gpu.shared

# Check if webhook is running
kubectl get pods -n gpu-system

# Describe pod for events
kubectl describe pod -n ai-dev -l app=vllm
```

**Common Issues**:
1. Not enough GPU shares available (need 2 free)
2. GPU webhook not running
3. Node selector mismatch

### CUDA Out of Memory

**Symptom**: vLLM crashes with CUDA OOM error

**Solutions**:
1. Reduce `GPU_MEMORY_UTILIZATION` (0.70 → 0.60)
2. Use smaller model
3. Reduce `MAX_MODEL_LEN` (8192 → 4096)
4. Check other GPU workloads' memory usage

### Plex Transcoding Fails After vLLM Deployment

**Symptom**: Plex can't transcode or stutters

**Check**:
```bash
# Run Plex health check
bash scripts/check-plex-health.sh

# Check GPU memory usage
kubectl exec -n ai-dev -l app=vllm -- nvidia-smi
```

**Solutions**:
1. Reduce vLLM `GPU_MEMORY_UTILIZATION` to 0.60 or 0.50
2. Temporarily scale down vLLM during heavy Plex usage
3. Consider scheduling: vLLM for daytime, Plex evenings

### GPU Not Visible in Pod

**Symptom**: `nvidia-smi` fails or shows no GPU

**Check**:
```bash
# Verify annotation
kubectl get pod -n ai-dev -l app=vllm -o yaml | grep gpu-directories

# Verify runtime class
kubectl get pod -n ai-dev -l app=vllm -o yaml | grep runtimeClassName

# Check device mounts
kubectl describe pod -n ai-dev -l app=vllm | grep /dev/nvidia
```

**Solutions**:
1. Ensure `gpu-directories: nvidia` annotation is set
2. Ensure `runtimeClassName: nvidia` is set
3. Check GPU webhook is running
4. Verify node has GPU: `kubectl describe node homelabai`

## Best Practices

1. **Always use hybrid approach** for AI workloads:
   - Annotation for device mounting
   - Resource requests for fair scheduling
   - Runtime class for NVIDIA support

2. **Conservative memory limits**:
   - Set `GPU_MEMORY_UTILIZATION` to 0.70 or lower
   - Leave headroom for other workloads

3. **Monitor Plex health**:
   - Run `check-plex-health.sh` after GPU changes
   - Watch for transcoding issues
   - Adjust vLLM memory if needed

4. **Request appropriate shares**:
   - vLLM: 2 shares (25% of GPU time)
   - Increase only if available and needed

5. **Schedule heavy workloads**:
   - Train models overnight when Plex usage is low
   - Scale down vLLM during peak Plex hours if needed

## Reference

- **NVIDIA Device Plugin**: https://github.com/NVIDIA/k8s-device-plugin
- **Time-Slicing Guide**: https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/gpu-sharing.html
- **vLLM GPU Settings**: https://docs.vllm.ai/en/latest/models/performance.html
- **NVIDIA Container Runtime**: https://github.com/NVIDIA/nvidia-container-runtime

## Quick Reference

### Check GPU Status
```bash
kubectl describe node homelabai | grep -A 5 "Allocatable:"
```

### List GPU Workloads
```bash
kubectl get pods -A -o wide | grep homelabai
```

### View vLLM GPU Usage
```bash
kubectl exec -n ai-dev -l app=vllm -- nvidia-smi
```

### Adjust vLLM Memory
```bash
kubectl edit configmap -n ai-dev vllm-config
# Change GPU_MEMORY_UTILIZATION
kubectl rollout restart deployment -n ai-dev vllm-server
```

### Emergency GPU Release
```bash
kubectl scale deployment -n ai-dev vllm-server --replicas=0
```
