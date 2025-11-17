# Safe Deployment Guide for AI-Dev System

This guide provides a **production-safe deployment strategy** for your K3s cluster with incremental rollout, validation gates, and rollback procedures.

## 🎯 Deployment Philosophy

1. **Plex is Sacred** - Never compromise Plex functionality
2. **Incremental Rollout** - Deploy components one at a time
3. **Validate Each Step** - Check before proceeding
4. **Easy Rollback** - Quick recovery if issues arise
5. **Monitor Continuously** - Watch GPU, memory, Plex health

## 📋 Pre-Deployment Checklist

### Prerequisites

- [ ] You are in a Git repository (`grok-servaar`)
- [ ] You have cluster admin access
- [ ] kubectl is configured and working
- [ ] You understand the GPU time-slicing setup
- [ ] You've read `GPU_TIMESLICING.md`
- [ ] Plex is currently healthy and transcoding works
- [ ] You have a maintenance window (low Plex usage time)

### Setup Preparation

```bash
# 1. Verify cluster access
kubectl cluster-info
kubectl get nodes

# 2. Check current GPU usage
kubectl describe node homelabai | grep -A 10 "Allocatable:"

# Expected: nvidia.com/gpu.shared: 8
# Current usage: Ollama=1, TTS=1, Whisper=2 (4 used, 4 available)

# 3. Verify Plex is healthy NOW (baseline)
kubectl get pods -n media -l app=plex
kubectl logs -n media -l app=plex --tail=20

# 4. Test Plex transcoding (play a video that requires transcoding)
# Note the current performance

# 5. Check GPU memory usage (baseline)
kubectl exec -n ai ollama-xxxxx -- nvidia-smi
# Note VRAM usage
```

### Build Prerequisites

```bash
# 1. Build code-indexer Docker image
cd ai-dev/code-indexer
docker build -t code-indexer:latest .

# 2. Tag for your registry (if using one)
# Option A: Use local registry
# docker tag code-indexer:latest localhost:5000/code-indexer:latest
# docker push localhost:5000/code-indexer:latest

# Option B: Use external registry
# docker tag code-indexer:latest your-registry.io/code-indexer:latest
# docker push your-registry.io/code-indexer:latest

# 3. Update cronjob.yaml with your image path
# Edit: code-indexer/cronjob.yaml
# Change: image: code-indexer:latest
# To: image: your-registry.io/code-indexer:latest

cd ../..
```

### Configure Secrets

```bash
# 1. GitHub token for SWE-agent
# Generate at: https://github.com/settings/tokens/new
# Required scopes: repo, workflow

# Store in file (temporarily)
echo "ghp_YourActualTokenHere" > /tmp/github-token

# 2. Configure your repositories
# Edit: ai-dev/code-indexer/configmap.yaml
# Add your actual Git repositories

# 3. (Optional) Change API authentication
# Edit: ai-dev/ingress/ingressroute.yaml
# Generate password: htpasswd -nb admin yourpassword | base64
```

## 🚀 Deployment Strategy: Incremental Rollout

We'll deploy in **5 phases** with validation gates between each phase.

### Phase 1: Namespace + Storage (No GPU Impact)

**What**: Create namespace and PVCs
**Risk**: None (no pods running)
**Rollback**: Easy (`kubectl delete namespace ai-dev`)

```bash
# Deploy
kubectl apply -f ai-dev/namespace/namespace.yaml
kubectl apply -f ai-dev/storage/pvcs.yaml

# Validate
kubectl get namespace ai-dev
kubectl get pvc -n ai-dev

# Expected: All PVCs in "Bound" status
# If not bound, check Longhorn dashboard
```

**Validation Gate** ✅:
- [ ] Namespace exists
- [ ] All 4 PVCs are Bound
- [ ] Longhorn shows healthy volumes

**Proceed?** Yes → Phase 2 | No → Debug PVC issues

---

### Phase 2: Qdrant Vector Database (No GPU Impact)

**What**: Deploy Qdrant for RAG
**Risk**: Low (no GPU, just CPU/memory)
**Rollback**: `kubectl delete -f ai-dev/qdrant/`

```bash
# Deploy
kubectl apply -f ai-dev/qdrant/qdrant-deployment.yaml

# Watch deployment
kubectl get pods -n ai-dev -w
# Wait for "Running" status (Ctrl+C to exit watch)

# Validate
kubectl get pods -n ai-dev -l app=qdrant
kubectl logs -n ai-dev -l app=qdrant --tail=50

# Test Qdrant API
kubectl exec -n ai-dev -l app=qdrant -- curl -s localhost:6333

# Expected: {"title":"qdrant - vector search engine","version":"..."}
```

**Validation Gate** ✅:
- [ ] Qdrant pod is Running
- [ ] No error logs
- [ ] Qdrant API responds
- [ ] PVC mounted correctly

**Proceed?** Yes → Phase 3 | No → Debug Qdrant issues

---

### Phase 3: vLLM Inference Server (⚠️ GPU WORKLOAD)

**What**: Deploy vLLM with GPU time-slicing
**Risk**: MEDIUM - Could impact Plex if misconfigured
**Rollback**: `kubectl delete -f ai-dev/vllm/`

#### 3.1 Pre-GPU Deployment Checks

```bash
# 1. Verify Plex is healthy
bash ai-dev/scripts/check-plex-health.sh

# 2. Check GPU shares available
kubectl describe node homelabai | grep nvidia.com/gpu.shared
# Expected: 4 available (need 2 for vLLM)

# 3. Note current GPU memory usage
kubectl exec -n ai ollama-xxxxx -- nvidia-smi
# Note VRAM usage (should be ~2-4GB)
```

#### 3.2 Deploy vLLM

```bash
# Deploy ConfigMap first
kubectl apply -f ai-dev/vllm/vllm-configmap.yaml

# Deploy vLLM server
kubectl apply -f ai-dev/vllm/vllm-deployment.yaml

# Watch deployment (this will take 5-10 minutes for model download)
kubectl get pods -n ai-dev -w

# Monitor logs in another terminal
kubectl logs -n ai-dev -l app=vllm -f
```

#### 3.3 What to Watch For

**Init Container (model-downloader)**:
```bash
# Check init container progress
kubectl logs -n ai-dev -l app=vllm -c model-downloader -f

# Expected: "Downloading model..."
# Then: "Download complete!"
# Time: 5-10 minutes depending on network
```

**Main Container (vllm)**:
```bash
# Check main container startup
kubectl logs -n ai-dev -l app=vllm -f

# Expected sequence:
# 1. "Loading model..."
# 2. "Initializing CUDA..."
# 3. "Model loaded successfully"
# 4. "Starting server on 0.0.0.0:8000"
# 5. "Application startup complete"
```

#### 3.4 Immediate Post-Deployment Checks

```bash
# 1. Check pod status
kubectl get pods -n ai-dev -l app=vllm

# Expected: Running (not CrashLoopBackOff)

# 2. Check GPU allocation
kubectl describe pod -n ai-dev -l app=vllm | grep nvidia.com/gpu.shared
# Expected: Limits: nvidia.com/gpu.shared: 2

# 3. Verify GPU devices mounted
kubectl exec -n ai-dev -l app=vllm -- ls -l /dev/nvidia*
# Expected: /dev/nvidia0, /dev/nvidiactl, /dev/nvidia-uvm

# 4. Check nvidia-smi works
kubectl exec -n ai-dev -l app=vllm -- nvidia-smi
# Expected: Shows GPU info, memory usage

# 5. Check GPU memory usage
kubectl exec -n ai-dev -l app=vllm -- nvidia-smi --query-gpu=memory.used --format=csv
# Expected: ~8-10GB (70% of 12GB)

# 6. Test vLLM health endpoint
kubectl exec -n ai-dev -l app=vllm -- curl -s http://localhost:8000/health
# Expected: {"status":"ok"} or similar
```

#### 3.5 🚨 CRITICAL: Plex Health Check

```bash
# Run comprehensive Plex check
bash ai-dev/scripts/check-plex-health.sh

# Manual checks if script fails:

# 1. Plex pod status
kubectl get pods -n media -l app=plex
# Expected: Running

# 2. Plex logs for GPU errors
kubectl logs -n media -l app=plex --tail=50 | grep -i "gpu\|error\|cuda"
# Expected: No new errors

# 3. Plex web UI accessible
curl -I https://plex.archer.casa
# Expected: HTTP 200 or 301/302

# 4. Check GPU still visible to Plex
kubectl exec -n media plex-0 -- ls -l /dev/nvidia*
# Expected: Devices present

# 5. TEST TRANSCODING - CRITICAL!
# Play a video that requires transcoding
# Watch for stuttering or errors
```

**Validation Gate** ✅:
- [ ] vLLM pod is Running
- [ ] GPU devices visible in vLLM pod
- [ ] nvidia-smi works in vLLM pod
- [ ] vLLM health endpoint responds
- [ ] GPU memory ~8-10GB (within expected range)
- [ ] **Plex pod still Running**
- [ ] **No new errors in Plex logs**
- [ ] **Plex web UI accessible**
- [ ] **Plex transcoding still works**

**If ANY Plex check fails** ❌:

```bash
# EMERGENCY ROLLBACK
kubectl delete deployment -n ai-dev vllm-server

# Wait 30 seconds
sleep 30

# Re-check Plex
bash ai-dev/scripts/check-plex-health.sh

# If Plex recovers: Investigate vLLM config (reduce GPU_MEMORY_UTILIZATION)
# If Plex still broken: Deeper issue, check GPU webhook, NVIDIA runtime
```

**Proceed?** Yes → Phase 4 | No → Fix Plex issues before continuing

---

### Phase 4: Code Indexer (No GPU Impact)

**What**: Deploy code indexer CronJob
**Risk**: Low (CPU/memory only, runs periodically)
**Rollback**: `kubectl delete -f ai-dev/code-indexer/`

```bash
# Deploy ConfigMap and CronJob
kubectl apply -f ai-dev/code-indexer/configmap.yaml
kubectl apply -f ai-dev/code-indexer/cronjob.yaml

# CronJob won't run until scheduled (2 AM daily)
# Trigger manual run for testing
kubectl create job --from=cronjob/code-indexer manual-index-test -n ai-dev

# Watch job
kubectl get jobs -n ai-dev -w

# Monitor logs
kubectl logs -n ai-dev -l app=code-indexer -f

# Expected:
# 1. "Initializing Code Indexer..."
# 2. "Connected to Qdrant..."
# 3. "Loading embedding model..."
# 4. "Cloning <repo>..."
# 5. "Found X code files to index"
# 6. "Uploading batch of embeddings..."
# 7. "Completed indexing <repo>: X chunks indexed"
# 8. "Code indexing completed!"
```

**Validation Gate** ✅:
- [ ] CronJob created
- [ ] Manual job completed successfully
- [ ] Embeddings in Qdrant
- [ ] No errors in logs

**Verify embeddings**:
```bash
kubectl exec -n ai-dev -l app=qdrant -- \
  curl -s localhost:6333/collections/code_embeddings | jq

# Expected: "points_count" > 0
```

**Proceed?** Yes → Phase 5 | No → Debug indexer issues

---

### Phase 5: SWE-agent (Optional, Low Priority)

**What**: Deploy SWE-agent for autonomous issue resolution
**Risk**: Low (only runs on-demand, no GPU unless triggered)
**Rollback**: `kubectl delete -f ai-dev/swe-agent/`

```bash
# 1. Create GitHub secret
kubectl create secret generic swe-agent-secrets \
  --from-file=github-token=/tmp/github-token \
  -n ai-dev

# Clean up temporary file
rm /tmp/github-token

# 2. Deploy SWE-agent
kubectl apply -f ai-dev/swe-agent/configmap.yaml
kubectl apply -f ai-dev/swe-agent/deployment.yaml

# 3. Validate
kubectl get pods -n ai-dev -l app=swe-agent
kubectl logs -n ai-dev -l app=swe-agent

# Note: SWE-agent server may not be used immediately
# Jobs are created on-demand to resolve issues
```

**Validation Gate** ✅:
- [ ] Secret created
- [ ] SWE-agent pod running (if using server deployment)
- [ ] No errors in logs

**Proceed?** Yes → Phase 6 (Ingress)

---

### Phase 6: Ingress (External Access)

**What**: Expose vLLM API via Traefik
**Risk**: Low (just networking)
**Rollback**: `kubectl delete -f ai-dev/ingress/`

```bash
# 1. Update domain in IngressRoute if needed
# Edit: ai-dev/ingress/ingressroute.yaml
# Change: code-llm.archer.casa to your domain

# 2. Deploy ingress
kubectl apply -f ai-dev/ingress/ingressroute.yaml

# 3. Validate
kubectl get ingressroute -n ai-dev
kubectl describe ingressroute -n ai-dev code-llm-api

# 4. Check Traefik recognized it
kubectl logs -n kube-system -l app.kubernetes.io/name=traefik --tail=20

# 5. Test internal access first
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl http://vllm-server.ai-dev:8000/health

# Expected: {"status":"ok"}

# 6. Update DNS (if using custom domain)
# Add A record: code-llm.archer.casa → <cluster-ip>

# 7. Test external access (after DNS propagates)
curl https://code-llm.archer.casa/health
```

**Validation Gate** ✅:
- [ ] IngressRoute created
- [ ] Internal access works
- [ ] External access works (if DNS configured)
- [ ] HTTPS works (if cert-manager configured)

---

## ✅ Final System Validation

After all phases are deployed:

```bash
# 1. Check all pods
kubectl get pods -n ai-dev

# Expected:
# - qdrant-xxx: Running
# - vllm-server-xxx: Running
# - swe-agent-xxx: Running (if deployed)
# - code-indexer jobs: Completed

# 2. Run comprehensive API tests
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000 &
sleep 5
python3 ai-dev/scripts/test-vllm-api.py --url http://localhost:8000

# Expected: All 4 tests pass
# - Health Check: PASS
# - Models Endpoint: PASS
# - Completion Endpoint: PASS
# - Chat Completion Endpoint: PASS

kill %1  # Stop port-forward

# 3. Test RAG context retrieval
kubectl exec -n ai-dev -l app=qdrant -- \
  curl -s localhost:6333/collections/code_embeddings

# Expected: points_count > 0

# 4. FINAL Plex health check
bash ai-dev/scripts/check-plex-health.sh

# All checks must pass!

# 5. Check GPU allocation
kubectl describe node homelabai | grep -A 10 "Allocated resources"

# Expected: nvidia.com/gpu.shared: 6/8 used
# (Ollama=1, TTS=1, Whisper=2, vLLM=2)

# 6. Monitor GPU usage for 10 minutes
watch -n 10 'kubectl exec -n ai-dev -l app=vllm -- nvidia-smi'

# Watch for:
# - VRAM usage stable (~8-10GB)
# - No OOM errors
# - GPU utilization varies (0-100% normal with time-slicing)
```

## 🔧 Post-Deployment Configuration

### 1. Configure Cline (VS Code)

```json
{
  "cline.apiProvider": "openai-compatible",
  "cline.apiUrl": "https://code-llm.archer.casa/v1",
  "cline.apiKey": "dummy-key",
  "cline.modelId": "deepseek-coder-6.7b-instruct"
}
```

Or use local port-forward:
```bash
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000

# Then in Cline:
# apiUrl: "http://localhost:8000/v1"
```

### 2. Configure Claude Code

```bash
# If using external URL
export OPENAI_API_BASE=https://code-llm.archer.casa/v1
export OPENAI_API_KEY=dummy-key

# If using port-forward
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000
export OPENAI_API_BASE=http://localhost:8000/v1
```

### 3. Test from IDE

Send a test request:
- "Write a Python function to calculate Fibonacci numbers"
- Check if RAG context is included (references to your code)

## 📊 Monitoring Setup

### Daily Checks

```bash
# 1. All pods healthy
kubectl get pods -n ai-dev

# 2. vLLM responding
kubectl exec -n ai-dev -l app=vllm -- curl -s http://localhost:8000/health

# 3. Plex healthy
bash ai-dev/scripts/check-plex-health.sh

# 4. GPU memory usage
kubectl exec -n ai-dev -l app=vllm -- \
  nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

### Alerting (Manual for Now)

Watch for:
- vLLM pod restarts: `kubectl get pods -n ai-dev -l app=vllm`
- High error rate in logs: `kubectl logs -n ai-dev -l app=vllm --tail=100 | grep ERROR`
- Plex transcoding failures: Check Plex dashboard
- GPU OOM: `kubectl logs -n ai-dev -l app=vllm | grep "out of memory"`

## 🚨 Rollback Procedures

### Quick Rollback (Emergency)

```bash
# Just remove vLLM (frees GPU immediately)
kubectl delete deployment -n ai-dev vllm-server

# Check Plex recovered
bash ai-dev/scripts/check-plex-health.sh
```

### Full Rollback

```bash
# Delete entire namespace
kubectl delete namespace ai-dev

# Verify Plex recovered
bash ai-dev/scripts/check-plex-health.sh

# If Plex still broken, deeper issue with GPU setup
# Check: GPU webhook, NVIDIA device plugin, runtime
```

### Partial Rollback

```bash
# Keep Qdrant and indexer, remove vLLM
kubectl delete deployment -n ai-dev vllm-server
kubectl delete configmap -n ai-dev vllm-config

# Or remove just ingress
kubectl delete ingressroute -n ai-dev code-llm-api
```

## 🔍 Troubleshooting Common Issues

### vLLM Pod Stuck in Pending

**Symptoms**: Pod doesn't start, status "Pending"

**Check**:
```bash
kubectl describe pod -n ai-dev -l app=vllm
```

**Common Causes**:
1. Not enough GPU shares available
   - Fix: Check `kubectl describe node homelabai`
   - Need 2 free shares
2. PVC not bound
   - Fix: Check Longhorn dashboard
3. GPU webhook not running
   - Fix: Check `kubectl get pods -n gpu-system`

### vLLM Crashes with CUDA OOM

**Symptoms**: Pod CrashLoopBackOff, logs show "CUDA out of memory"

**Fix**:
```bash
# Reduce memory utilization
kubectl edit configmap -n ai-dev vllm-config
# Change GPU_MEMORY_UTILIZATION: "0.70" to "0.60"

# Restart deployment
kubectl rollout restart deployment -n ai-dev vllm-server
```

### Plex Transcoding Fails After vLLM Deploy

**Symptoms**: Plex can't transcode or stutters

**Fix**:
```bash
# Option 1: Reduce vLLM memory
kubectl edit configmap -n ai-dev vllm-config
# Set GPU_MEMORY_UTILIZATION: "0.50"
kubectl rollout restart deployment -n ai-dev vllm-server

# Option 2: Reduce vLLM GPU shares
kubectl edit deployment -n ai-dev vllm-server
# Change nvidia.com/gpu.shared: 2 to 1
kubectl rollout restart deployment -n ai-dev vllm-server

# Option 3: Temporary - scale down vLLM during Plex usage
kubectl scale deployment -n ai-dev vllm-server --replicas=0
# Scale back up later
kubectl scale deployment -n ai-dev vllm-server --replicas=1
```

### Model Download Fails

**Symptoms**: Init container fails or times out

**Check**:
```bash
kubectl logs -n ai-dev -l app=vllm -c model-downloader
```

**Fix**:
```bash
# Increase init container timeout (if needed)
# Or manually download model to PVC

# Option: Pre-download model
kubectl run -it --rm model-dl --image=python:3.11-slim \
  --overrides='{"spec":{"containers":[{"name":"model-dl","image":"python:3.11-slim","command":["sleep","3600"],"volumeMounts":[{"name":"models","mountPath":"/models"}]}],"volumes":[{"name":"models","persistentVolumeClaim":{"claimName":"model-storage"}}]}}' \
  -n ai-dev

# Inside pod:
pip install huggingface_hub
python3 -c "from huggingface_hub import snapshot_download; snapshot_download('deepseek-ai/deepseek-coder-6.7b-instruct', local_dir='/models/deepseek-coder-6.7b-instruct')"
exit

# Then restart vLLM deployment
```

### Code Indexer Fails

**Symptoms**: Indexer job fails or no embeddings

**Check**:
```bash
kubectl logs -n ai-dev -l app=code-indexer
```

**Common Issues**:
1. Can't reach Qdrant
   - Fix: Check `kubectl get svc -n ai-dev qdrant`
2. Can't clone repo
   - Fix: Check repository URLs in ConfigMap
   - For private repos: Add credentials
3. Out of memory
   - Fix: Increase resources in cronjob.yaml

## 📅 Maintenance Schedule

### Daily
- ✅ Check all pods: `kubectl get pods -n ai-dev`
- ✅ Verify Plex health: `bash ai-dev/scripts/check-plex-health.sh`

### Weekly
- Check GPU memory trends
- Review vLLM logs for errors
- Verify code indexer ran successfully

### Monthly
- Update base model (if new version)
- Review GPU allocation (adjust if needed)
- Backup Qdrant database
- Update documentation with learnings

## ✅ Success Criteria

System is fully operational when:

1. ✅ All pods in `ai-dev` namespace are Running
2. ✅ vLLM API responds to health checks
3. ✅ All 4 API tests pass
4. ✅ Qdrant contains code embeddings (points_count > 0)
5. ✅ **Plex health check passes**
6. ✅ **Plex transcoding works**
7. ✅ Can query from Cline/Claude Code
8. ✅ GPU shows 6/8 shares used
9. ✅ GPU memory stable (~8-10GB for vLLM)
10. ✅ No CUDA OOM errors
11. ✅ External API access works (if ingress configured)

## 🎓 Safe Deployment Philosophy Summary

1. **Start Small** - Namespace → Storage → Qdrant
2. **Test Before GPU** - Verify non-GPU components first
3. **GPU with Caution** - vLLM deployment with immediate Plex checks
4. **Validate Each Phase** - Don't proceed if validation fails
5. **Quick Rollback** - Always have escape hatch ready
6. **Monitor Continuously** - Watch GPU, memory, Plex
7. **Document Issues** - Record problems and solutions

## 📞 Getting Help

If you encounter issues during deployment:

1. Check this guide's troubleshooting section
2. Review component-specific docs (README.md, GPU_TIMESLICING.md)
3. Check logs: `kubectl logs -n ai-dev -l app=<component>`
4. Describe resources: `kubectl describe pod -n ai-dev <pod-name>`
5. Check events: `kubectl get events -n ai-dev --sort-by='.lastTimestamp'`

## 🚀 Ready to Deploy?

Follow the phases in order:
1. Complete pre-deployment checklist
2. Phase 1: Namespace + Storage
3. Phase 2: Qdrant
4. Phase 3: vLLM (⚠️ CRITICAL - Watch Plex!)
5. Phase 4: Code Indexer
6. Phase 5: SWE-agent (optional)
7. Phase 6: Ingress
8. Final validation
9. Configure IDE integration
10. Celebrate! 🎉

**Remember**: You can always rollback. Plex is priority #1. Take your time on Phase 3.

Good luck! 🚀
