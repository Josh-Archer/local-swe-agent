# AI-Dev: Fine-Tuned Coding LLM System

A complete coding assistant system running on Kubernetes with:
- Fine-tuned open-source LLM for code generation
- RAG (Retrieval-Augmented Generation) with vector database
- OpenAI-compatible API for integration with Cline and Claude Code
- SWE-agent for autonomous GitHub issue resolution
- Optimized for NVIDIA 3080 Ti GPU

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  Kubernetes Cluster (ai-dev namespace)                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────────┐      ┌──────────────────┐            │
│  │  Code Indexer    │──────▶│  Qdrant Vector  │            │
│  │  (CronJob)       │      │  Database        │            │
│  │  - Git clone     │      │  (Deployment)    │            │
│  │  - Embed code    │      │  + PVC (Longhorn)│            │
│  └──────────────────┘      └──────────────────┘            │
│                                      │                       │
│                                      │ RAG Retrieval        │
│  ┌──────────────────┐               ▼                       │
│  │  SWE-agent       │      ┌──────────────────┐            │
│  │  - GitHub issues │◀─────│  vLLM Inference  │            │
│  │  - Auto PRs      │      │  - Base model    │◀──NVIDIA   │
│  └──────────────────┘      │  - LoRA adapters │   3080 Ti  │
│                            │  - OpenAI API    │            │
│                            └──────────────────┘            │
│                                     │                       │
│                                     │                       │
│                            ┌──────────────────┐            │
│                            │  Traefik Ingress │            │
│                            │  + HTTPS         │            │
│                            └──────────────────┘            │
│                                     │                       │
└─────────────────────────────────────┼───────────────────────┘
                                      │
                                      ▼
                        https://code-llm.archer.casa
                                      │
                        ┌─────────────┴─────────────┐
                        │                           │
                   ┌────▼────┐              ┌──────▼──────┐
                   │  Cline  │              │ Claude Code │
                   │  (IDE)  │              │    (CLI)    │
                   └─────────┘              └─────────────┘
```

## Components

### 1. vLLM Inference Server
- **Base Model**: DeepSeek Coder 6.7B (or Qwen2.5-Coder 7B)
- **Quantization**: 4-bit for 3080 Ti (12GB VRAM)
- **API**: OpenAI-compatible endpoints
- **Features**: LoRA adapter support, prefix caching, batching
- **GPU**: Time-sliced (2/8 shares = 25% GPU time) with 70% memory limit
- **GPU Sharing**: Shares GPU with Plex, Ollama (1), TTS (1), Whisper (2)
- **See**: [GPU_TIMESLICING.md](GPU_TIMESLICING.md) for detailed GPU configuration

### 2. Qdrant Vector Database
- **Purpose**: Store code embeddings for RAG
- **Embeddings**: sentence-transformers/all-MiniLM-L6-v2
- **Storage**: Longhorn PVC (20GB)
- **Collections**: code_embeddings

### 3. Code Indexer
- **Type**: Kubernetes CronJob (daily at 2 AM)
- **Function**: Clone repos → Chunk code → Generate embeddings → Upload to Qdrant
- **Storage**: 10GB cache for Git clones
- **Configuration**: ConfigMap with repository list

### 4. SWE-agent
- **Purpose**: Autonomous GitHub issue resolution
- **LLM**: Uses vLLM API (no external calls)
- **Features**: Multi-step reasoning, tool use, PR creation
- **Deployment**: On-demand Jobs + optional server

### 5. Traefik Ingress
- **Domain**: code-llm.archer.casa
- **TLS**: cert-manager with Let's Encrypt
- **Auth**: Basic authentication (configurable)
- **Rate Limiting**: 100 req/s average

## Directory Structure

```
ai-dev/
├── namespace/
│   └── namespace.yaml              # ai-dev namespace
├── storage/
│   └── pvcs.yaml                   # PVCs for models, adapters, Qdrant, cache
├── qdrant/
│   └── qdrant-deployment.yaml      # Vector database
├── vllm/
│   ├── vllm-configmap.yaml         # Model and performance config
│   └── vllm-deployment.yaml        # Inference server with GPU
├── code-indexer/
│   ├── Dockerfile                  # Indexer container image
│   ├── index_code.py               # Python indexing script
│   ├── config.yaml                 # Repository configuration
│   ├── configmap.yaml              # Kubernetes ConfigMap
│   └── cronjob.yaml                # Scheduled indexing job
├── swe-agent/
│   ├── configmap.yaml              # SWE-agent configuration
│   ├── deployment.yaml             # SWE-agent server
│   ├── job-template.yaml           # Template for issue resolution
│   └── secret-template.yaml        # GitHub token
├── ingress/
│   └── ingressroute.yaml           # Traefik + middleware + auth
├── scripts/
│   ├── validate-manifests.sh       # Pre-deployment validation
│   ├── deploy.sh                   # Full deployment script
│   ├── test-vllm-api.py            # API testing
│   └── check-plex-health.sh        # GPU conflict detection
└── README.md                        # This file
```

## Prerequisites

### Cluster Requirements
- K3s cluster with 3+ nodes
- NVIDIA GPU node (homelabai) with:
  - NVIDIA 3080 Ti (12GB VRAM)
  - GPU webhook for automatic device mounting
  - Time-sliced GPU support
- Longhorn storage provisioner
- Traefik ingress controller
- cert-manager (optional, for HTTPS)

### Local Requirements
- kubectl configured for cluster access
- Docker (for building code-indexer image)
- Python 3.11+ (for testing scripts)
- Git

## Quick Start

### 1. Configure Repositories

Edit `code-indexer/configmap.yaml` and add your repositories:

```yaml
repositories:
  - name: "grok-servaar"
    url: "https://github.com/yourusername/grok-servaar.git"
  - name: "my-project"
    url: "https://github.com/yourusername/my-project.git"
```

### 2. Build Code Indexer Image

```bash
cd code-indexer
docker build -t your-registry/code-indexer:latest .
docker push your-registry/code-indexer:latest
```

Update `code-indexer/cronjob.yaml` with your image name.

### 3. Configure Secrets

**GitHub Token** (for SWE-agent):
```bash
kubectl create secret generic swe-agent-secrets \
  --from-literal=github-token=ghp_YourTokenHere \
  -n ai-dev
```

**API Authentication** (optional):
```bash
# Generate password hash
htpasswd -nb admin yourpassword | base64

# Update ingress/ingressroute.yaml with the hash
```

### 4. Validate Manifests

```bash
bash scripts/validate-manifests.sh
```

### 5. Deploy

```bash
bash scripts/deploy.sh
```

### 6. Monitor Deployment

```bash
# Watch pods
kubectl get pods -n ai-dev -w

# Check vLLM logs
kubectl logs -n ai-dev -l app=vllm -f

# Check Qdrant
kubectl logs -n ai-dev -l app=qdrant
```

### 7. Test API

Port-forward and test locally:
```bash
kubectl port-forward -n ai-dev svc/vllm-server 8000:8000

python3 scripts/test-vllm-api.py --url http://localhost:8000
```

### 8. Index Code

Trigger manual indexing:
```bash
kubectl create job --from=cronjob/code-indexer manual-index-$(date +%s) -n ai-dev

# Watch progress
kubectl logs -n ai-dev -l app=code-indexer -f
```

### 9. Verify Plex Health

**CRITICAL**: After GPU workload deployment:
```bash
bash scripts/check-plex-health.sh
```

## Usage

### Accessing the API

**Internal (cluster)**:
```bash
http://vllm-server.ai-dev.svc.cluster.local:8000
```

**External (after ingress setup)**:
```bash
https://code-llm.archer.casa
```

### Integrate with Cline

In VS Code settings:
```json
{
  "cline.apiProvider": "openai-compatible",
  "cline.apiUrl": "https://code-llm.archer.casa/v1",
  "cline.apiKey": "dummy-key",
  "cline.modelId": "deepseek-coder-6.7b-instruct"
}
```

### Integrate with Claude Code

Configure custom model in `~/.claude-code/config.json`:
```json
{
  "customModels": [
    {
      "name": "local-deepseek",
      "baseURL": "https://code-llm.archer.casa/v1",
      "apiKey": "dummy-key"
    }
  ]
}
```

### Using SWE-agent

**Resolve a GitHub issue**:
```bash
# Edit job-template.yaml with issue URL
cat swe-agent/job-template.yaml | \
  sed 's|ISSUE_URL_PLACEHOLDER|https://github.com/owner/repo/issues/123|' | \
  kubectl apply -f -

# Monitor
kubectl logs -n ai-dev -f job/swe-agent-issue-123
```

**Automatic nightly issue resolution**:
Configure a CronJob similar to code-indexer that queries GitHub for issues labeled "ai-fix".

## Configuration

### vLLM Performance Tuning

Edit `vllm/vllm-configmap.yaml`:

```yaml
GPU_MEMORY_UTILIZATION: "0.70"  # Lower if Plex has issues
MAX_MODEL_LEN: "8192"            # Increase for longer context
MAX_NUM_SEQS: "256"              # Batch size
```

### RAG Tuning

Edit `code-indexer/config.yaml`:

```yaml
chunk_size: 500       # Lines per chunk
chunk_overlap: 50     # Overlap between chunks
embedding_model: "sentence-transformers/all-MiniLM-L6-v2"
```

### SWE-agent Tuning

Edit `swe-agent/configmap.yaml`:

```yaml
agent:
  max_steps: 50       # Max reasoning steps
  timeout: 600        # Task timeout (seconds)
model:
  temperature: 0.2    # Lower = more deterministic
```

## Fine-Tuning (Optional)

To fine-tune LoRA adapters:

1. Prepare training data from your code
2. Use Axolotl or Unsloth in a Kubernetes Job
3. Save adapters to `adapter-storage` PVC
4. Enable in vLLM deployment:

```yaml
args:
  - --enable-lora
  - --lora-modules=custom=/adapters/custom-lora
```

See separate `TRAINING.md` for detailed guide.

## Monitoring

### Check System Status

```bash
# All resources
kubectl get all -n ai-dev

# GPU usage
kubectl exec -n ai-dev -l app=vllm -- nvidia-smi

# Qdrant stats
kubectl exec -n ai-dev -l app=qdrant -- curl localhost:6333/collections/code_embeddings
```

### Metrics (if Prometheus deployed)

```bash
# Query vLLM metrics
curl http://vllm-server.ai-dev:8000/metrics
```

### Logs

```bash
# vLLM
kubectl logs -n ai-dev -l app=vllm --tail=100

# Qdrant
kubectl logs -n ai-dev -l app=qdrant --tail=100

# Code indexer
kubectl logs -n ai-dev -l app=code-indexer --tail=100

# SWE-agent
kubectl logs -n ai-dev -l app=swe-agent --tail=100
```

## Troubleshooting

### vLLM Pod Not Starting

**Symptoms**: Pod stuck in `ContainerCreating` or `CrashLoopBackOff`

**Check**:
```bash
kubectl describe pod -n ai-dev -l app=vllm
kubectl logs -n ai-dev -l app=vllm
```

**Common issues**:
- GPU not mounted: Check `gpu-directories: nvidia` annotation
- Out of memory: Reduce `GPU_MEMORY_UTILIZATION`
- Model download failed: Check init container logs
- PVC not bound: Check `kubectl get pvc -n ai-dev`

### Plex Transcoding Issues

**Symptoms**: Plex can't transcode after vLLM deployment

**Check**:
```bash
bash scripts/check-plex-health.sh
```

**Fix**:
- Lower vLLM GPU memory: Edit configmap, set to `0.60`
- Check time-slicing: `kubectl describe node homelabai | grep gpu`
- Schedule training jobs overnight when Plex usage is low

### Code Indexer Fails

**Symptoms**: Indexer job fails or no embeddings in Qdrant

**Check**:
```bash
kubectl logs -n ai-dev job/code-indexer-manual
```

**Common issues**:
- Git clone failed: Check repository URL and credentials
- Qdrant unreachable: `kubectl get svc -n ai-dev qdrant`
- Out of memory: Increase resources in cronjob.yaml
- Wrong file extensions: Check `code_extensions` in config

### SWE-agent Can't Create PRs

**Symptoms**: SWE-agent runs but doesn't create PR

**Check**:
```bash
kubectl logs -n ai-dev job/swe-agent-issue-XXX
```

**Common issues**:
- GitHub token missing: `kubectl get secret swe-agent-secrets -n ai-dev`
- Token permissions: Need `repo` and `workflow` scopes
- vLLM not responding: Check vLLM health
- Docker socket not accessible: Check volume mount

### API Returns Errors

**Symptoms**: 401, 403, or 500 errors from API

**Check**:
```bash
python3 scripts/test-vllm-api.py --url http://localhost:8000
```

**Common issues**:
- Auth required: Use credentials from `api-auth-secret`
- Rate limited: Check middleware configuration
- Model not loaded: Check vLLM logs
- Out of VRAM: Reduce concurrent requests or model size

## Maintenance

### Update Base Model

```bash
# Download new model to PVC
kubectl exec -n ai-dev -l app=vllm -- /scripts/download-model.sh

# Or update deployment with new model
kubectl edit configmap -n ai-dev vllm-config
kubectl rollout restart deployment -n ai-dev vllm-server
```

### Re-index Repositories

```bash
# Trigger manual indexing
kubectl create job --from=cronjob/code-indexer reindex-$(date +%s) -n ai-dev
```

### Backup Vector Database

```bash
# Create Qdrant snapshot
kubectl exec -n ai-dev -l app=qdrant -- \
  curl -X POST localhost:6333/collections/code_embeddings/snapshots

# Copy snapshot out
kubectl cp ai-dev/<qdrant-pod>:/qdrant/storage/snapshots ./backup/
```

### Scale Down (Save GPU)

```bash
# Temporarily stop vLLM
kubectl scale deployment vllm-server --replicas=0 -n ai-dev

# Resume
kubectl scale deployment vllm-server --replicas=1 -n ai-dev
```

## Resource Usage

**Current Allocation**:
- vLLM: 16-24GB RAM, 4-8 CPU, 8-10GB VRAM
- Qdrant: 2-4GB RAM, 1-2 CPU
- Code Indexer: 2-4GB RAM, 1-2 CPU (during runs)
- SWE-agent: 4-8GB RAM, 2-4 CPU (per job)

**Storage**:
- Models: 50GB (Longhorn)
- Adapters: 10GB (Longhorn)
- Qdrant: 20GB (Longhorn)
- Indexer cache: 10GB (Longhorn)

**Total**: ~90GB storage, 24-40GB RAM, 7-16 CPU cores

## Security Considerations

- **API Authentication**: Enable basic auth in production
- **GitHub Tokens**: Use fine-grained tokens with minimal scopes
- **Network Policies**: Restrict ingress to vLLM (optional)
- **Secrets**: Never commit secrets to Git
- **Rate Limiting**: Enabled by default (100 req/s)

## Performance Benchmarks

Expected performance (DeepSeek Coder 6.7B, 4-bit):

- **Cold start**: 2-5 minutes (model loading)
- **Inference latency**: 1-3s for chat (50-100 tokens)
- **Throughput**: 20-50 requests/minute
- **Context window**: 8192 tokens
- **RAG retrieval**: <500ms for top-5 chunks

## Future Enhancements

- [ ] Add Grafana dashboards for monitoring
- [ ] Implement fine-tuning pipeline (Axolotl Job)
- [ ] Multi-model support (switch between models)
- [ ] Hybrid search (keyword + vector)
- [ ] Function calling support
- [ ] Streaming responses
- [ ] Model quantization optimization (AWQ/GPTQ)
- [ ] Auto-scaling based on load
- [ ] Multi-GPU support (if adding more GPUs)

## References

- vLLM Documentation: https://docs.vllm.ai
- SWE-agent: https://github.com/SWE-agent/SWE-agent
- Qdrant: https://qdrant.tech/documentation
- DeepSeek Coder: https://huggingface.co/deepseek-ai
- Sentence Transformers: https://www.sbert.net

## Support

For issues related to:
- **Cluster/GitOps**: See `MASTER_README.md`, `TESTING.md`
- **GPU System**: See `grok-servaar/gpu-system/README.md`
- **This System**: Open an issue or check logs

## License

MIT (or match your organization's license)
