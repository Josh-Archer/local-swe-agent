# AI-Dev Deployment Quick Reference Card

**Print this and keep it handy during deployment!**

---

## 🚀 Quick Start (Automated)

```bash
# Interactive deployment with validation gates
bash ai-dev/scripts/deploy-safe.sh

# Non-interactive (YOLO mode - not recommended first time)
INTERACTIVE=0 bash ai-dev/scripts/deploy-safe.sh
```

---

## 📋 Manual Deployment Phases

### Phase 1: Namespace + Storage
```bash
kubectl apply -f ai-dev/namespace/namespace.yaml
kubectl apply -f ai-dev/storage/pvcs.yaml
kubectl get pvc -n ai-dev  # All should be "Bound"
```

### Phase 2: Qdrant
```bash
kubectl apply -f ai-dev/qdrant/qdrant-deployment.yaml
kubectl wait --for=condition=ready pod -l app=qdrant -n ai-dev --timeout=300s
kubectl exec -n ai-dev -l app=qdrant -- curl localhost:6333  # Test
```

### Phase 3: vLLM (⚠️ GPU - WATCH PLEX!)
```bash
kubectl apply -f ai-dev/vllm/vllm-configmap.yaml
kubectl apply -f ai-dev/vllm/vllm-deployment.yaml
kubectl wait --for=condition=ready pod -l app=vllm -n ai-dev --timeout=600s

# Test GPU
kubectl exec -n ai-dev -l app=vllm -- nvidia-smi

# 🚨 CHECK PLEX IMMEDIATELY!
bash ai-dev/scripts/check-plex-health.sh
```

### Phase 4: Code Indexer
```bash
kubectl apply -f ai-dev/code-indexer/configmap.yaml
kubectl apply -f ai-dev/code-indexer/cronjob.yaml

# Manual trigger (optional)
kubectl create job --from=cronjob/code-indexer manual-index -n ai-dev
```

### Phase 5: SWE-agent (Optional)
```bash
# Create secret first!
kubectl create secret generic swe-agent-secrets \
  --from-literal=github-token=ghp_YourToken -n ai-dev

kubectl apply -f ai-dev/swe-agent/configmap.yaml
kubectl apply -f ai-dev/swe-agent/deployment.yaml
```

### Phase 6: Ingress (Optional)
```bash
kubectl apply -f ai-dev/ingress/ingressroute.yaml
```

---

## 🔍 Quick Health Checks

### Check All Pods
```bash
kubectl get pods -n ai-dev
```

### Test vLLM API
```bash
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000 &
curl http://localhost:8000/health
kill %1
```

### Check GPU Usage
```bash
kubectl exec -n ai-dev -l app=vllm -- nvidia-smi
```

### Check Plex (CRITICAL!)
```bash
bash ai-dev/scripts/check-plex-health.sh
```

### View GPU Allocation
```bash
kubectl describe node homelabai | grep -A 10 "Allocated"
```

---

## 🔄 Model Hot-Swap (no full redeploy)

Model id and runtime knobs live in ConfigMap `vllm-config`. Swap without touching Qdrant/indexer/SWE-agent:

```bash
# Recommended automation (apply ConfigMap + restart vLLM + health checks)
bash ai-dev/scripts/swap-model.sh

# CLI override example
bash ai-dev/scripts/swap-model.sh \
  --model-name "TheBloke/deepseek-coder-6.7B-instruct-AWQ" \
  --model-path "/models/deepseek-coder-6.7b-instruct-awq" \
  --served-name "deepseek-coder-6.7b-instruct"

# Health only
bash ai-dev/scripts/check-model-health.sh
# or: make swap-model / make check-model-health
```

PVC `model-storage` caches each `MODEL_PATH` under `/models`; re-using a path skips download. Full notes: `ai-dev/MODEL_HOT_SWAP.md`.

---

## 🚨 Emergency Rollback

### Quick (Just vLLM)
```bash
kubectl delete deployment -n ai-dev vllm-server
bash ai-dev/scripts/check-plex-health.sh
```

### Full Rollback
```bash
kubectl delete namespace ai-dev
bash ai-dev/scripts/check-plex-health.sh
```

---

## 🔧 Common Issues & Fixes

### vLLM OOM
```bash
kubectl edit configmap -n ai-dev vllm-config
# Change: GPU_MEMORY_UTILIZATION: "0.70" → "0.60"
kubectl rollout restart deployment -n ai-dev vllm-server
```

### Plex Can't Transcode
```bash
# Reduce vLLM memory
kubectl edit configmap -n ai-dev vllm-config
# Set: GPU_MEMORY_UTILIZATION: "0.50"
kubectl rollout restart deployment -n ai-dev vllm-server
```

### Pod Stuck Pending
```bash
kubectl describe pod -n ai-dev <pod-name>
# Check: Events section for errors
# Common: Not enough GPU shares, PVC not bound
```

---

## 📊 Expected GPU Allocation

| Workload | Shares | Percentage |
|----------|--------|------------|
| Ollama | 1 | 12.5% |
| TTS | 1 | 12.5% |
| Whisper | 2 | 25% |
| **vLLM** | **2** | **25%** |
| Available | 2 | 25% |
| **Total** | **8** | **100%** |

---

## 🎯 Success Criteria

- [ ] All pods Running
- [ ] vLLM health responds: `{"status":"ok"}`
- [ ] GPU visible: `nvidia-smi` works in vLLM pod
- [ ] Plex pod Running
- [ ] Plex transcoding works
- [ ] API tests pass (4/4)
- [ ] GPU memory ~8-10GB (vLLM)

---

## 📞 Quick Commands Reference

```bash
# View logs
kubectl logs -n ai-dev -l app=vllm -f

# Restart deployment
kubectl rollout restart deployment -n ai-dev vllm-server

# Scale down (emergency)
kubectl scale deployment -n ai-dev vllm-server --replicas=0

# Scale back up
kubectl scale deployment -n ai-dev vllm-server --replicas=1

# Port forward for testing
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000

# Run API tests
python3 ai-dev/scripts/test-vllm-api.py --url http://localhost:8000

# Check events
kubectl get events -n ai-dev --sort-by='.lastTimestamp'

# Describe pod
kubectl describe pod -n ai-dev -l app=vllm
```

---

## 🎓 Deployment Tips

1. ✅ **Deploy during low Plex usage** (early morning)
2. ✅ **Have someone test Plex** during Phase 3
3. ✅ **Monitor GPU memory** for first hour
4. ✅ **Keep this card open** in terminal
5. ✅ **Don't panic** - rollback is easy!

---

## 📚 Full Documentation

- **Complete Guide**: `SAFE_DEPLOYMENT_GUIDE.md`
- **GPU Details**: `GPU_TIMESLICING.md`
- **Troubleshooting**: `README.md`
- **Architecture**: `PROJECT_SUMMARY.md`

---

**Remember**: Plex is priority #1. If Plex breaks, rollback immediately! 🚨
