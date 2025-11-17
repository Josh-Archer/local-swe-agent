# AI-Dev Deployment Checklist

Use this checklist to ensure proper deployment and configuration.

## Pre-Deployment

### Cluster Readiness
- [ ] K3s cluster is running and accessible
- [ ] GPU node (homelabai) is available and labeled
- [ ] GPU webhook is installed and functioning
- [ ] Longhorn storage is deployed and healthy
- [ ] Traefik ingress controller is running
- [ ] kubectl can access cluster: `kubectl cluster-info`

### Prerequisites Installed
- [ ] Docker (for building code-indexer image)
- [ ] Python 3.11+ (for testing scripts)
- [ ] Git
- [ ] kubectl
- [ ] (Optional) htpasswd for generating auth credentials

### Configuration Files Ready
- [ ] Repositories configured in `code-indexer/configmap.yaml`
- [ ] GitHub token generated with `repo` and `workflow` scopes
- [ ] Domain name configured (e.g., code-llm.archer.casa)
- [ ] TLS certificates available (or cert-manager configured)

## Deployment Steps

### 1. Namespace Creation
- [ ] Create namespace: `kubectl apply -f namespace/namespace.yaml`
- [ ] Verify: `kubectl get namespace ai-dev`

### 2. Storage Provisioning
- [ ] Apply PVCs: `kubectl apply -f storage/pvcs.yaml`
- [ ] Verify all PVCs bound: `kubectl get pvc -n ai-dev`
- [ ] Check Longhorn dashboard for volumes

### 3. Secrets Configuration
- [ ] Create GitHub token secret:
  ```bash
  kubectl create secret generic swe-agent-secrets \
    --from-literal=github-token=ghp_YOUR_TOKEN \
    -n ai-dev
  ```
- [ ] Verify secret: `kubectl get secret swe-agent-secrets -n ai-dev`
- [ ] (Optional) Update API auth credentials in `ingress/ingressroute.yaml`

### 4. Build and Push Code Indexer Image
- [ ] Build Docker image: `cd code-indexer && docker build -t code-indexer:latest .`
- [ ] Tag for registry: `docker tag code-indexer:latest your-registry/code-indexer:latest`
- [ ] Push to registry: `docker push your-registry/code-indexer:latest`
- [ ] Update image name in `code-indexer/cronjob.yaml`
- [ ] (Alternative) Skip if deploying without code indexer initially

### 5. Deploy Qdrant Vector Database
- [ ] Apply deployment: `kubectl apply -f qdrant/qdrant-deployment.yaml`
- [ ] Wait for ready: `kubectl wait --for=condition=ready pod -l app=qdrant -n ai-dev --timeout=300s`
- [ ] Verify: `kubectl get pods -n ai-dev -l app=qdrant`
- [ ] Test connectivity: `kubectl exec -n ai-dev -l app=qdrant -- curl localhost:6333`

### 6. Deploy vLLM Inference Server
- [ ] Apply configmap: `kubectl apply -f vllm/vllm-configmap.yaml`
- [ ] Apply deployment: `kubectl apply -f vllm/vllm-deployment.yaml`
- [ ] Monitor logs: `kubectl logs -n ai-dev -l app=vllm -f`
- [ ] Wait for model download (5-10 minutes)
- [ ] Wait for ready: `kubectl wait --for=condition=ready pod -l app=vllm -n ai-dev --timeout=600s`
- [ ] Verify GPU allocation: `kubectl describe pod -n ai-dev -l app=vllm | grep nvidia`

### 7. Deploy Code Indexer
- [ ] Apply configmap: `kubectl apply -f code-indexer/configmap.yaml`
- [ ] Apply cronjob: `kubectl apply -f code-indexer/cronjob.yaml`
- [ ] Trigger manual run: `kubectl create job --from=cronjob/code-indexer manual-index -n ai-dev`
- [ ] Monitor: `kubectl logs -n ai-dev -l app=code-indexer -f`
- [ ] Verify embeddings:
  ```bash
  kubectl exec -n ai-dev -l app=qdrant -- \
    curl localhost:6333/collections/code_embeddings
  ```

### 8. Deploy SWE-agent
- [ ] Apply configmap: `kubectl apply -f swe-agent/configmap.yaml`
- [ ] Apply deployment: `kubectl apply -f swe-agent/deployment.yaml`
- [ ] Verify: `kubectl get pods -n ai-dev -l app=swe-agent`
- [ ] (Optional) Test with issue: Use `swe-agent/job-template.yaml`

### 9. Configure Ingress
- [ ] Update domain in `ingress/ingressroute.yaml`
- [ ] Apply ingress: `kubectl apply -f ingress/ingressroute.yaml`
- [ ] Verify: `kubectl get ingressroute -n ai-dev`
- [ ] Check Traefik: `kubectl get svc -n kube-system traefik`
- [ ] Update DNS A record for domain → cluster IP
- [ ] Test external access: `curl https://code-llm.archer.casa/health`

### 10. Validation and Testing
- [ ] Run manifest validation: `bash scripts/validate-manifests.sh`
- [ ] Port-forward API: `kubectl port-forward -n ai-dev svc/vllm-server 8000:8000`
- [ ] Run API tests: `python3 scripts/test-vllm-api.py --url http://localhost:8000`
- [ ] All 4 tests should pass
- [ ] Test from external URL: `python3 scripts/test-vllm-api.py --url https://code-llm.archer.casa`

### 11. Plex Health Verification
- [ ] **CRITICAL**: Run Plex health check: `bash scripts/check-plex-health.sh`
- [ ] Verify Plex pod is Running
- [ ] Verify no errors in Plex logs
- [ ] Verify GPU devices visible in Plex pod
- [ ] Verify Plex web UI accessible
- [ ] Test transcoding a video file
- [ ] Monitor GPU usage: Watch for conflicts

### 12. Integration Testing
- [ ] Configure Cline with API endpoint
- [ ] Send test request from Cline
- [ ] Verify response quality
- [ ] Configure Claude Code (if using)
- [ ] Test code completion
- [ ] Test chat functionality
- [ ] Verify RAG context is being used

## Post-Deployment

### Monitoring Setup
- [ ] Check all pods are Running: `kubectl get pods -n ai-dev`
- [ ] Verify all PVCs are bound: `kubectl get pvc -n ai-dev`
- [ ] Monitor GPU usage:
  ```bash
  kubectl exec -n ai-dev -l app=vllm -- nvidia-smi
  ```
- [ ] Check vLLM metrics: `curl http://vllm-server.ai-dev:8000/metrics`
- [ ] (Optional) Set up Grafana dashboards

### Performance Tuning
- [ ] Monitor inference latency
- [ ] Adjust `GPU_MEMORY_UTILIZATION` if needed
- [ ] Tune `MAX_NUM_SEQS` for throughput
- [ ] Configure rate limiting appropriately
- [ ] Adjust chunk sizes for RAG

### Documentation
- [ ] Document custom configurations
- [ ] Record any issues encountered and solutions
- [ ] Update team wiki/docs with access information
- [ ] Create runbook for common operations

### Backup and Recovery
- [ ] Document backup procedure for Qdrant
- [ ] Save LoRA adapters (if fine-tuned)
- [ ] Export important configurations
- [ ] Test restore procedure

### Security Hardening
- [ ] Change default API credentials
- [ ] Review and strengthen GitHub token permissions
- [ ] Configure network policies (optional)
- [ ] Enable audit logging (optional)
- [ ] Review exposed endpoints

## Ongoing Maintenance Checklist

### Daily
- [ ] Check pod health: `kubectl get pods -n ai-dev`
- [ ] Verify Plex is healthy (automated check)
- [ ] Monitor GPU utilization

### Weekly
- [ ] Review logs for errors
- [ ] Check storage usage: `kubectl get pvc -n ai-dev`
- [ ] Verify code indexer ran successfully
- [ ] Review API usage patterns

### Monthly
- [ ] Update base models (if new versions)
- [ ] Re-train LoRA adapters with new code
- [ ] Review and update repository list
- [ ] Check for system updates
- [ ] Backup Qdrant database
- [ ] Review security settings

### As Needed
- [ ] Scale resources based on usage
- [ ] Add new repositories to index
- [ ] Fine-tune model parameters
- [ ] Update SWE-agent configurations
- [ ] Respond to Plex GPU conflicts

## Rollback Procedure

If something goes wrong:

### Quick Rollback
```bash
# Delete entire namespace
kubectl delete namespace ai-dev

# Re-deploy from last known good state
git checkout <last-good-commit>
bash scripts/deploy.sh
```

### Partial Rollback
```bash
# Rollback specific deployment
kubectl rollout undo deployment/vllm-server -n ai-dev

# Rollback to specific revision
kubectl rollout undo deployment/vllm-server --to-revision=2 -n ai-dev
```

### Emergency GPU Release
```bash
# Immediately free GPU for Plex
kubectl scale deployment vllm-server --replicas=0 -n ai-dev

# Verify Plex recovered
bash scripts/check-plex-health.sh
```

## Troubleshooting Quick Reference

| Issue | Quick Fix |
|-------|-----------|
| vLLM OOM | Reduce `GPU_MEMORY_UTILIZATION` to 0.60 |
| Plex can't transcode | Scale down vLLM: `kubectl scale deployment vllm-server --replicas=0 -n ai-dev` |
| API slow | Increase `MAX_NUM_SEQS` or check GPU usage |
| Indexer fails | Check Qdrant is ready, verify repo URLs |
| SWE-agent no PR | Verify GitHub token and permissions |
| Ingress 404 | Check IngressRoute, verify DNS |
| Pod won't start | Check PVC status, describe pod for events |

## Success Criteria

System is fully operational when:

- ✅ All pods in `ai-dev` namespace are Running
- ✅ vLLM API responds to health checks
- ✅ API tests pass (4/4)
- ✅ Qdrant contains code embeddings
- ✅ Plex health check passes
- ✅ Can query from Cline/Claude Code
- ✅ SWE-agent can resolve test issue
- ✅ External API access works (via ingress)
- ✅ GPU time-sharing works correctly
- ✅ No error logs in any component

## Support Resources

- **Logs**: `kubectl logs -n ai-dev -l app=<component>`
- **Events**: `kubectl get events -n ai-dev --sort-by='.lastTimestamp'`
- **Describe**: `kubectl describe pod -n ai-dev <pod-name>`
- **GPU Info**: `kubectl describe node homelabai`
- **Full docs**: `README.md`
- **Quick start**: `QUICKSTART.md`

## Final Verification Command

Run this to verify everything:

```bash
# Check all resources
kubectl get all,pvc,ingressroute,secrets -n ai-dev

# Run all tests
bash scripts/validate-manifests.sh && \
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000 &
sleep 5 && \
python3 scripts/test-vllm-api.py && \
bash scripts/check-plex-health.sh && \
kill %1  # Stop port-forward
```

Expected output: All checks pass, all tests pass, Plex healthy.
