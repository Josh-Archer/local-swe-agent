# 🚀 AI-Dev System - Ready for Safe Deployment

Your AI-Dev system is **production-ready** and configured for **safe incremental deployment** to your K3s cluster.

## ✅ What's Been Built

### Complete System (29 Files)

**Kubernetes Manifests** (15 files):
- ✅ Namespace and storage (PVCs)
- ✅ Qdrant vector database
- ✅ vLLM inference server with GPU time-slicing
- ✅ Code indexer (CronJob + manual job)
- ✅ SWE-agent deployments and jobs
- ✅ Traefik ingress with auth and rate limiting

**Scripts** (5 files):
- ✅ `deploy-safe.sh` - Interactive incremental deployment
- ✅ `deploy.sh` - Original full deployment
- ✅ `validate-manifests.sh` - Pre-deployment validation
- ✅ `test-vllm-api.py` - Comprehensive API testing
- ✅ `check-plex-health.sh` - GPU conflict detection

**Python Code** (2 files):
- ✅ `index_code.py` - 350+ line code indexer
- ✅ Test suite in test-vllm-api.py

**Documentation** (7 files, 72KB):
- ✅ `SAFE_DEPLOYMENT_GUIDE.md` - **START HERE** (27KB)
- ✅ `GPU_TIMESLICING.md` - Complete GPU configuration (11KB)
- ✅ `GPU_SETUP_SUMMARY.md` - What was changed for GPU (7KB)
- ✅ `DEPLOYMENT_QUICK_REF.md` - Quick reference card (5KB)
- ✅ `README.md` - Full system documentation (16KB)
- ✅ `QUICKSTART.md` - 30-minute guide (7KB)
- ✅ `DEPLOYMENT_CHECKLIST.md` - Step-by-step validation (9KB)
- ✅ `PROJECT_SUMMARY.md` - Architecture overview (12KB)

**Other**:
- ✅ Dockerfile for code-indexer
- ✅ ConfigMaps and Secrets templates
- ✅ Kustomization file for GitOps

## 🎯 GPU Configuration Verified

Your vLLM deployment is configured to match your existing GPU time-slicing setup:

### Hybrid GPU Access (Like Ollama)
```yaml
# Webhook triggers automatic device mounting
annotations:
  gpu-directories: nvidia

# Explicit runtime class
runtimeClassName: nvidia

# Time-sliced resource allocation (2 shares = 25% GPU time)
resources:
  limits:
    nvidia.com/gpu.shared: "2"
  requests:
    nvidia.com/gpu.shared: "2"

# NVIDIA environment variables
env:
- name: NVIDIA_VISIBLE_DEVICES
  value: "all"
- name: NVIDIA_DRIVER_CAPABILITIES
  value: "compute,utility"

# Conservative memory limit (70% = ~8.4GB of 12GB)
- name: GPU_MEMORY_UTILIZATION
  value: "0.70"
```

### GPU Share Allocation
| Workload | Before | After |
|----------|--------|-------|
| Ollama | 1 | 1 |
| TTS | 1 | 1 |
| Whisper | 2 | 2 |
| **vLLM** | **-** | **2** ← NEW |
| Available | 4 | 2 |

## 📋 Pre-Deployment Tasks (You Need to Do)

### 1. Build Code Indexer Image (Required)

```bash
cd ai-dev/code-indexer

# Build
docker build -t code-indexer:latest .

# Option A: Push to registry
docker tag code-indexer:latest your-registry.io/code-indexer:latest
docker push your-registry.io/code-indexer:latest

# Then update cronjob.yaml with your image path

# Option B: Use local (if cluster can access Docker daemon)
# No push needed, update cronjob.yaml to: code-indexer:latest
```

### 2. Configure Your Repositories (Required)

Edit `code-indexer/configmap.yaml`:
```yaml
repositories:
  - name: "grok-servaar"
    url: "https://github.com/yourusername/grok-servaar.git"
  - name: "your-app"
    url: "https://github.com/yourusername/your-app.git"
  # Add all repos you want indexed
```

### 3. Create GitHub Token (For SWE-agent, Optional)

1. Go to: https://github.com/settings/tokens/new
2. Scopes: `repo`, `workflow`
3. Generate token
4. Save temporarily: `echo "ghp_YourToken" > /tmp/github-token`

### 4. Configure API Auth (Required for ingress)

There are **no default credentials** in-repo. Deploy scripts refuse placeholders
and known insecure defaults. Create `api-auth-secret` before ingress:

```bash
# Generate htpasswd users file (bcrypt)
htpasswd -nbB admin 'your-strong-password' > /tmp/auth

# Create secret (Traefik basicAuth expects key "users")
kubectl create secret generic api-auth-secret \
  --from-file=users=/tmp/auth \
  -n ai-dev
rm -f /tmp/auth

# Template / SealedSecret example (no real credentials):
#   ai-dev/ingress/example-secret.yaml
```

### 5. Update Domain (Optional)

Edit `ingress/ingressroute.yaml`:
```yaml
match: Host(`your-domain.com`)  # Change from code-llm.archer.casa
```

## 🚀 Safe Deployment Options

### Option 1: Interactive Incremental Deployment (RECOMMENDED)

**Best for**: First deployment, production clusters, when you want control

```bash
# Run from repository root
bash ai-dev/scripts/deploy-safe.sh
```

**What it does**:
- ✅ Deploys in 6 phases with validation gates
- ✅ Pauses between phases for your approval
- ✅ Checks Plex health after GPU deployment
- ✅ Provides clear status and next steps
- ✅ Easy to abort at any time

**Time**: 20-30 minutes (including model download)

### Option 2: Automated Deployment (For Experts)

**Best for**: Second deployment, testing, when you're confident

```bash
# Non-interactive mode
INTERACTIVE=0 bash ai-dev/scripts/deploy-safe.sh
```

**Warning**: Skips validation gates! Only use if you know what you're doing.

### Option 3: Manual Phase-by-Phase

**Best for**: Maximum control, debugging issues

Follow: `DEPLOYMENT_QUICK_REF.md` or `SAFE_DEPLOYMENT_GUIDE.md`

Deploy each phase manually with validation between steps.

### Option 4: Kustomize (GitOps)

**Best for**: Integrating with existing GitOps workflow

```bash
kubectl apply -k ai-dev/
```

**Note**: No validation gates, all components deploy at once.

## 📖 Deployment Documentation

| Document | Purpose | When to Read |
|----------|---------|--------------|
| **SAFE_DEPLOYMENT_GUIDE.md** | Complete deployment strategy | **Read this first!** |
| **DEPLOYMENT_QUICK_REF.md** | Quick reference card | Print and keep handy |
| **GPU_TIMESLICING.md** | GPU configuration details | Before GPU deployment |
| **README.md** | Full system documentation | After deployment |
| **QUICKSTART.md** | Condensed guide | Alternative to safe guide |

## ⚠️ Critical Safety Rules

1. **Plex is Priority #1**
   - If Plex breaks, rollback immediately
   - Always check Plex after GPU deployment
   - Use `check-plex-health.sh` script

2. **Deploy During Low Usage**
   - Early morning (2-6 AM)
   - When Plex transcoding is minimal
   - Have someone test Plex during deployment

3. **Monitor GPU Memory**
   - First hour after deployment
   - Watch for OOM errors
   - Adjust if vLLM > 10GB VRAM

4. **Easy Rollback Available**
   - Quick: `kubectl delete deployment -n ai-dev vllm-server`
   - Full: `kubectl delete namespace ai-dev`
   - Always verify Plex recovers

5. **Validation Gates Matter**
   - Don't skip validation checks
   - If something fails, fix before continuing
   - Each phase builds on previous

## 🎯 Success Criteria

After deployment, you should have:

- ✅ All pods in `ai-dev` namespace Running
- ✅ vLLM responding to API calls
- ✅ GPU visible in vLLM pod (`nvidia-smi` works)
- ✅ **Plex pod still Running**
- ✅ **Plex transcoding still works**
- ✅ Qdrant has code embeddings
- ✅ Can query from Cline/Claude Code
- ✅ GPU shows 6/8 shares used
- ✅ GPU memory ~8-10GB (vLLM)
- ✅ No CUDA OOM errors

## 🚨 If Something Goes Wrong

### Quick Emergency Actions

**Plex Broken**:
```bash
kubectl delete deployment -n ai-dev vllm-server
sleep 30
bash ai-dev/scripts/check-plex-health.sh
```

**vLLM Won't Start**:
```bash
kubectl describe pod -n ai-dev -l app=vllm
kubectl logs -n ai-dev -l app=vllm
# Check events and logs for clues
```

**GPU OOM**:
```bash
kubectl edit configmap -n ai-dev vllm-config
# Set GPU_MEMORY_UTILIZATION: "0.60"
kubectl rollout restart deployment -n ai-dev vllm-server
```

**Full Rollback**:
```bash
kubectl delete namespace ai-dev
bash ai-dev/scripts/check-plex-health.sh
```

## 📞 Getting Help

**During Deployment**:
1. Check `SAFE_DEPLOYMENT_GUIDE.md` troubleshooting section
2. Review pod logs: `kubectl logs -n ai-dev -l app=<component>`
3. Describe resources: `kubectl describe pod -n ai-dev <pod>`
4. Check events: `kubectl get events -n ai-dev`

**After Deployment**:
1. See `README.md` for usage and configuration
2. Check `GPU_TIMESLICING.md` for GPU tuning
3. Review component logs for errors

## 🎓 Recommended Deployment Flow

**Day Before**:
1. ✅ Read `SAFE_DEPLOYMENT_GUIDE.md` completely
2. ✅ Build code-indexer Docker image
3. ✅ Configure repositories in configmap
4. ✅ Create GitHub token (if using SWE-agent)
5. ✅ Print `DEPLOYMENT_QUICK_REF.md`
6. ✅ Schedule deployment time (low Plex usage)

**Deployment Day**:
1. ✅ Open `DEPLOYMENT_QUICK_REF.md` in terminal
2. ✅ Run `bash ai-dev/scripts/deploy-safe.sh`
3. ✅ Follow prompts, validate each phase
4. ✅ **Watch Plex health after Phase 3**
5. ✅ Run final validation tests
6. ✅ Configure IDE (Cline/Claude Code)

**After Deployment**:
1. ✅ Monitor for 24 hours
2. ✅ Test Plex transcoding multiple times
3. ✅ Watch GPU memory trends
4. ✅ Test AI-dev API from IDE
5. ✅ Document any issues/adjustments

## 🔄 What Happens During Deployment

### Timeline (Estimated)

- **Phase 1**: Namespace + Storage → 1 minute
- **Phase 2**: Qdrant → 2 minutes
- **Phase 3**: vLLM (GPU) → **10-15 minutes** (model download)
- **Phase 4**: Code Indexer → 1 minute
- **Phase 5**: SWE-agent → 1 minute (optional)
- **Phase 6**: Ingress → 1 minute (optional)
- **Validation**: 5 minutes

**Total**: ~20-30 minutes

### What Takes Time

- ⏱️ Model download (5-10 minutes) - first time only
- ⏱️ PVC provisioning (1-2 minutes)
- ⏱️ Image pulls (2-5 minutes) - first time
- ⏱️ Health checks (readiness probes)

### What to Watch

- Terminal output from `deploy-safe.sh`
- Plex web UI (have it open)
- GPU usage: `watch kubectl exec -n ai-dev -l app=vllm -- nvidia-smi`
- Pod status: `watch kubectl get pods -n ai-dev`

## 📦 Next Steps After Successful Deployment

1. **Test the API**
   ```bash
   kubectl port-forward -n ai-dev svc/vllm-server 8000:8000 &
   python3 ai-dev/scripts/test-vllm-api.py
   ```

2. **Index Your Code**
   ```bash
   kubectl create job --from=cronjob/code-indexer manual-index -n ai-dev
   kubectl logs -n ai-dev -l app=code-indexer -f
   ```

3. **Configure Cline**
   - API URL: `http://localhost:8000/v1`
   - Model: `deepseek-coder-6.7b-instruct`
   - Test with simple query

4. **Monitor for 24 Hours**
   - Check Plex transcoding works
   - Watch GPU memory usage
   - Review vLLM logs for errors
   - Test AI coding assistance

5. **Optional: Fine-Tune**
   - Collect training data
   - Train LoRA adapter
   - Deploy to adapter-storage PVC

## 🎉 You're Ready!

Everything is prepared for safe deployment. The system is:

- ✅ GPU time-slicing compatible
- ✅ Plex-aware and safe
- ✅ Incrementally deployable
- ✅ Easy to rollback
- ✅ Comprehensively documented
- ✅ Production-ready

**Start Here**:
```bash
bash ai-dev/scripts/deploy-safe.sh
```

Good luck! 🚀

---

**Questions?** Check the documentation:
- **Deployment**: `SAFE_DEPLOYMENT_GUIDE.md`
- **GPU**: `GPU_TIMESLICING.md`
- **Usage**: `README.md`
- **Quick Ref**: `DEPLOYMENT_QUICK_REF.md`
