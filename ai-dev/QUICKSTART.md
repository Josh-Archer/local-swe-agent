# Quick Start Guide

Get your AI-dev system running in 30 minutes.

## Prerequisites Checklist

- [ ] K3s cluster running with GPU node
- [ ] kubectl configured and working
- [ ] Longhorn storage available
- [ ] Traefik ingress controller deployed
- [ ] Docker installed locally (for building indexer)

## Step-by-Step Installation

### Step 1: Configure Your Repositories (5 min)

Edit `code-indexer/configmap.yaml`:

```yaml
repositories:
  - name: "grok-servaar"
    url: "https://github.com/yourusername/grok-servaar.git"
  - name: "my-app"
    url: "https://github.com/yourusername/my-app.git"
```

### Step 2: Build Code Indexer Image (5 min)

```bash
cd code-indexer

# Build image
docker build -t code-indexer:latest .

# Tag for your registry (if using one)
docker tag code-indexer:latest your-registry/code-indexer:latest
docker push your-registry/code-indexer:latest

# Update cronjob.yaml with your image name
```

**Alternative**: Skip for now and deploy without code indexer initially.

### Step 3: Create Secrets (2 min)

```bash
# GitHub token for SWE-agent (get from: https://github.com/settings/tokens/new)
# Required scopes: repo, workflow
kubectl create namespace ai-dev
kubectl create secret generic swe-agent-secrets \
  --from-literal=github-token=ghp_YourGitHubTokenHere \
  -n ai-dev

# API auth (optional)
# Default: user/password - CHANGE IN PRODUCTION!
```

### Step 4: Deploy Everything (10 min)

```bash
# Make scripts executable
chmod +x scripts/*.sh

# Validate manifests
./scripts/validate-manifests.sh

# Deploy
./scripts/deploy.sh
```

Watch the deployment:
```bash
kubectl get pods -n ai-dev -w
```

### Step 5: Wait for Model Download (5-10 min)

The vLLM pod will download the base model on first start. This takes 5-10 minutes.

Check progress:
```bash
kubectl logs -n ai-dev -l app=vllm -f
```

Look for: `"Completed model download"` or `"Starting vLLM server"`

### Step 6: Test the API (2 min)

Port-forward to access locally:
```bash
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000
```

In another terminal:
```bash
python3 scripts/test-vllm-api.py --url http://localhost:8000
```

You should see:
```
✓ PASS - Health Check
✓ PASS - Models Endpoint
✓ PASS - Completion Endpoint
✓ PASS - Chat Completion Endpoint

Total: 4/4 tests passed
```

### Step 7: Index Your Code (5 min)

Trigger manual indexing:
```bash
kubectl create job --from=cronjob/code-indexer manual-index -n ai-dev

# Watch progress
kubectl logs -n ai-dev -l app=code-indexer -f
```

Wait for: `"Code indexing completed!"`

### Step 8: Verify Plex Health (1 min)

**CRITICAL - Don't skip this!**

```bash
./scripts/check-plex-health.sh
```

All checks should pass.

### Step 9: Configure Your IDE

#### For Cline (VS Code):

Install Cline extension, then configure:

```json
{
  "cline.apiProvider": "openai-compatible",
  "cline.apiUrl": "http://localhost:8000/v1",
  "cline.apiKey": "dummy-key",
  "cline.modelId": "deepseek-coder-6.7b-instruct"
}
```

Or use external URL after ingress setup:
```json
{
  "cline.apiUrl": "https://code-llm.archer.casa/v1"
}
```

#### For Claude Code (CLI):

```bash
# Configure custom model
claude config add-model \
  --name local-deepseek \
  --base-url http://localhost:8000/v1 \
  --api-key dummy-key

# Use it
claude --model local-deepseek "Write a Python function to parse YAML"
```

### Step 10: Test SWE-agent (Optional)

Find a simple GitHub issue in your repository, then:

```bash
# Edit job template
cat swe-agent/job-template.yaml | \
  sed 's|ISSUE_URL_PLACEHOLDER|https://github.com/youruser/yourrepo/issues/1|' | \
  kubectl apply -f -

# Monitor
kubectl logs -n ai-dev -f job/swe-agent-issue-0001
```

SWE-agent will attempt to solve the issue and create a PR.

## Verification Checklist

After deployment, verify:

- [ ] All pods are `Running`: `kubectl get pods -n ai-dev`
- [ ] vLLM responds to API calls: `python3 scripts/test-vllm-api.py`
- [ ] Qdrant has embeddings: `kubectl exec -n ai-dev -l app=qdrant -- curl localhost:6333/collections/code_embeddings`
- [ ] Plex is healthy: `./scripts/check-plex-health.sh`
- [ ] Ingress is configured: `kubectl get ingressroute -n ai-dev`
- [ ] Can query from IDE (Cline/Claude Code)

## Common Quick Start Issues

### vLLM Pod Stuck in Init

**Problem**: Init container downloading model is slow or failing

**Solution**:
```bash
# Check init container logs
kubectl logs -n ai-dev -l app=vllm -c model-downloader

# If network issue, manually download and copy to PVC
# Or use different mirror/CDN
```

### Out of GPU Memory

**Problem**: vLLM fails with CUDA OOM error

**Solution**:
```bash
# Reduce GPU memory utilization
kubectl edit configmap -n ai-dev vllm-config
# Change GPU_MEMORY_UTILIZATION to "0.60"

# Restart deployment
kubectl rollout restart deployment -n ai-dev vllm-server
```

### Can't Access API Externally

**Problem**: Ingress not working or DNS not resolving

**Solution**:
```bash
# Check ingress
kubectl get ingressroute -n ai-dev
kubectl describe ingressroute -n ai-dev code-llm-api

# Check Traefik
kubectl get svc -n kube-system traefik

# Test internal access first
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl http://vllm-server.ai-dev:8000/health
```

### Code Indexer Fails

**Problem**: Can't clone repositories or connect to Qdrant

**Solution**:
```bash
# Check if Qdrant is ready
kubectl get pods -n ai-dev -l app=qdrant

# Check indexer logs
kubectl logs -n ai-dev -l app=code-indexer

# For private repos, add credentials:
kubectl create secret generic git-credentials \
  --from-literal=username=youruser \
  --from-literal=password=yourtoken \
  -n ai-dev
```

## Next Steps

Once everything is working:

1. **Set up automated indexing**: The CronJob runs daily at 2 AM
2. **Configure ingress with real domain**: Update DNS and IngressRoute
3. **Enable HTTPS**: Install cert-manager and configure TLS
4. **Fine-tune a LoRA adapter**: See `TRAINING.md` (to be created)
5. **Set up monitoring**: Deploy Grafana dashboards
6. **Optimize performance**: Tune batch size, memory, etc.

## Getting Help

- Check logs: `kubectl logs -n ai-dev -l app=<component>`
- Describe resources: `kubectl describe pod -n ai-dev <pod-name>`
- Full README: See `README.md`
- GPU issues: See `grok-servaar/gpu-system/README.md`

## Minimal Working Setup

If you want to start even simpler (skip SWE-agent and code indexer):

```bash
# Deploy just vLLM + Qdrant
kubectl apply -f namespace/
kubectl apply -f storage/
kubectl apply -f qdrant/
kubectl apply -f vllm/

# Port-forward and test
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000
python3 scripts/test-vllm-api.py
```

You can add the code indexer and SWE-agent later once vLLM is working.

## Success Criteria

You'll know it's working when:

1. ✅ vLLM responds with code completions
2. ✅ Qdrant contains embeddings from your code
3. ✅ You can query from Cline/Claude Code
4. ✅ Plex is still healthy and transcoding
5. ✅ SWE-agent can resolve simple issues

**Estimated time to working system**: 30-60 minutes (depending on network speed for model download)
