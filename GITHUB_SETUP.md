# GitHub Repository Setup Guide

Complete guide for setting up the AI-Dev project on GitHub with automated testing and CI/CD.

## Prerequisites

- GitHub account
- Git installed locally
- Repository created on GitHub (or ready to create)

## Initial Repository Setup

### 1. Create GitHub Repository

```bash
# Option A: Via GitHub CLI
gh repo create your-org/ai-dev-system --public --description "AI-powered coding assistant with vLLM and RAG"

# Option B: Via GitHub web interface
# 1. Go to https://github.com/new
# 2. Name: ai-dev-system
# 3. Description: AI-powered coding assistant with vLLM and RAG
# 4. Public/Private: Choose based on your needs
# 5. Do NOT initialize with README (we already have one)
# 6. Click "Create repository"
```

### 2. Initialize Local Repository

```bash
cd C:\Code\code-agent

# Initialize git if not already done
git init

# Add remote
git remote add origin https://github.com/YOUR-USERNAME/ai-dev-system.git

# Or if using SSH
git remote add origin git@github.com:YOUR-USERNAME/ai-dev-system.git
```

### 3. Create .gitignore

```bash
cat > .gitignore << 'EOF'
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
.venv
pip-log.txt
pip-delete-this-directory.txt
.pytest_cache/
.coverage
htmlcov/
*.cover
.hypothesis/

# IDEs
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db

# Kubernetes
*.kubeconfig
kubeconfig

# Secrets (IMPORTANT!)
*secret*.yaml
*-secrets.yaml
.env
*.pem
*.key
credentials.json

# Temporary files
*.tmp
*.bak
*.log

# Build artifacts
dist/
build/
*.egg-info/

# Docker
.dockerignore

# Coverage
coverage.xml
.coverage.*

# Test artifacts
.tox/
.pytest_cache/
EOF
```

### 4. Initial Commit

```bash
# Stage all files
git add .

# Commit
git commit -m "Initial commit: AI-Dev system with vLLM, Qdrant, and code indexer

Features:
- vLLM inference server with AWQ quantization
- Qdrant vector database for RAG
- Code indexer with sentence-transformers
- Kubernetes manifests for K3s deployment
- GPU time-slicing support
- Comprehensive documentation
- GitHub Actions CI/CD workflows
- Python unit tests and integration tests"

# Push to GitHub
git push -u origin main
```

## GitHub Actions Configuration

### 1. Enable GitHub Actions

1. Go to your repository on GitHub
2. Navigate to **Settings** → **Actions** → **General**
3. Under "Actions permissions", select:
   - ✅ **Allow all actions and reusable workflows**
4. Under "Workflow permissions", select:
   - ✅ **Read and write permissions**
   - ✅ **Allow GitHub Actions to create and approve pull requests**
5. Click **Save**

### 2. Configure Required Status Checks

1. Go to **Settings** → **Branches**
2. Click **Add branch protection rule**
3. Branch name pattern: `main`
4. Enable:
   - ✅ **Require status checks to pass before merging**
   - ✅ **Require branches to be up to date before merging**
5. Select these required checks:
   - `validate-manifests`
   - `lint-shell-scripts`
   - `check-security`
   - `test-python-code`
   - `build-and-test`
6. Optional but recommended:
   - ✅ **Require pull request reviews before merging** (1 approval)
   - ✅ **Require conversation resolution before merging**
   - ✅ **Require linear history**
7. Click **Create**

### 3. Configure GitHub Packages (GHCR)

For Docker image publishing:

1. Go to **Settings** → **Packages**
2. Under "Package creation", ensure packages are visible
3. After first successful build:
   - Go to **Packages** tab in your repo
   - Find `code-indexer` package
   - Click **Package settings**
   - Under "Manage access", add collaborators if needed

### 4. Configure Security Scanning

#### Enable Dependabot

1. Go to **Settings** → **Code security and analysis**
2. Enable:
   - ✅ **Dependency graph**
   - ✅ **Dependabot alerts**
   - ✅ **Dependabot security updates**
3. Create `.github/dependabot.yml`:

```yaml
version: 2
updates:
  # GitHub Actions
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"

  # Docker
  - package-ecosystem: "docker"
    directory: "/ai-dev/code-indexer"
    schedule:
      interval: "weekly"

  # Python
  - package-ecosystem: "pip"
    directory: "/ai-dev/tests"
    schedule:
      interval: "weekly"
```

#### Enable CodeQL

1. Go to **Settings** → **Code security and analysis**
2. Click **Set up** under "Code scanning"
3. Choose **CodeQL Analysis**
4. Select languages: **Python**
5. Commit the workflow file

### 5. Configure Secrets

Add secrets for sensitive data:

1. Go to **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**

**Recommended secrets**:
```
DOCKERHUB_TOKEN        # If using Docker Hub
KUBECONFIG            # For automated deployments (base64 encoded)
SLACK_WEBHOOK_URL     # For notifications
CODECOV_TOKEN         # For coverage reporting
```

**Add secret example**:
```bash
# Encode kubeconfig
cat ~/.kube/config | base64

# Add to GitHub secrets as KUBECONFIG
```

## Setting Up Badges

### 1. Add Status Badges to README

Add to the top of your main `README.md`:

```markdown
# AI-Dev System

![CI](https://github.com/YOUR-USERNAME/ai-dev-system/workflows/CI%20-%20Validate%20Manifests/badge.svg)
![Python Tests](https://github.com/YOUR-USERNAME/ai-dev-system/workflows/Python%20Tests/badge.svg)
![Docker Build](https://github.com/YOUR-USERNAME/ai-dev-system/workflows/Docker%20Build%20%26%20Test/badge.svg)
[![codecov](https://codecov.io/gh/YOUR-USERNAME/ai-dev-system/branch/main/graph/badge.svg)](https://codecov.io/gh/YOUR-USERNAME/ai-dev-system)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
```

### 2. Generate Custom Badges

Use [shields.io](https://shields.io) for custom badges:

```markdown
![Kubernetes](https://img.shields.io/badge/kubernetes-v1.28+-blue)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11-blue)
![GPU](https://img.shields.io/badge/GPU-Time--Slicing-green)
```

## Automated Deployment (Optional)

### Create Deployment Workflow

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy to K3s

on:
  push:
    branches: [ main ]
    paths:
      - 'ai-dev/**/*.yaml'
  workflow_dispatch:
    inputs:
      environment:
        description: 'Deployment environment'
        required: true
        default: 'staging'
        type: choice
        options:
          - staging
          - production

jobs:
  deploy:
    name: Deploy to K3s Cluster
    runs-on: ubuntu-latest
    environment: ${{ github.event.inputs.environment || 'staging' }}

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up kubectl
        uses: azure/setup-kubectl@v3
        with:
          version: 'v1.28.0'

      - name: Configure kubectl
        run: |
          mkdir -p ~/.kube
          echo "${{ secrets.KUBECONFIG }}" | base64 -d > ~/.kube/config

      - name: Deploy manifests
        run: |
          # Validate first
          bash ai-dev/scripts/validate-manifests.sh

          # Apply manifests
          kubectl apply -f ai-dev/namespace/
          kubectl apply -f ai-dev/storage/
          kubectl apply -f ai-dev/qdrant/
          kubectl apply -f ai-dev/vllm/

      - name: Wait for deployment
        run: |
          kubectl wait --for=condition=ready pod -l app=vllm -n ai-dev --timeout=600s

      - name: Run health checks
        run: |
          kubectl get pods -n ai-dev
          kubectl exec -n ai-dev -l app=vllm -- curl -f http://localhost:8000/health
```

## Repository Settings

### Recommended Settings

1. **General** → **Features**:
   - ✅ Issues
   - ✅ Discussions (for community)
   - ❌ Projects (unless needed)
   - ❌ Wiki (use docs/ instead)

2. **General** → **Pull Requests**:
   - ✅ Allow squash merging
   - ❌ Allow merge commits
   - ❌ Allow rebase merging
   - ✅ Automatically delete head branches

3. **Code security and analysis**:
   - ✅ Dependency graph
   - ✅ Dependabot alerts
   - ✅ Dependabot security updates
   - ✅ Secret scanning

## Testing the Setup

### 1. Trigger Workflows Manually

```bash
# Via GitHub CLI
gh workflow run "CI - Validate Manifests"
gh workflow run "Python Tests"
gh workflow run "Docker Build & Test"

# View runs
gh run list
gh run watch
```

### 2. Create Test Pull Request

```bash
# Create feature branch
git checkout -b test/ci-pipeline

# Make a small change
echo "# Test CI" >> ai-dev/README.md

# Commit and push
git add .
git commit -m "test: Verify CI pipeline"
git push -u origin test/ci-pipeline

# Create PR via CLI
gh pr create --title "Test CI Pipeline" --body "Testing GitHub Actions workflows"

# Or via web interface
```

### 3. Monitor Actions

1. Go to **Actions** tab in your repository
2. Watch the workflows execute
3. Check for:
   - ✅ All checks passing
   - ✅ Coverage reports generated
   - ✅ Security scans completed
   - ✅ Docker image built

## Troubleshooting

### Workflow Failures

**Permission denied errors**:
```yaml
# Add to workflow
permissions:
  contents: read
  packages: write
  security-events: write
```

**Docker push fails**:
```bash
# Check GHCR authentication
echo ${{ secrets.GITHUB_TOKEN }} | docker login ghcr.io -u ${{ github.actor }} --password-stdin
```

**Kubernetes dry-run fails**:
- Ensure Kind cluster is properly initialized
- Check manifest syntax locally first

### Local Testing

```bash
# Test GitHub Actions locally with act
brew install act  # or your package manager

# Run CI workflow locally
act -j validate-manifests

# Run with secrets
act -j build-and-test -s GITHUB_TOKEN=your_token
```

## Maintenance

### Regular Tasks

**Weekly**:
- Review Dependabot PRs
- Check security alerts
- Review failed workflow runs

**Monthly**:
- Update action versions
- Review coverage trends
- Audit branch protection rules

**Quarterly**:
- Review and update test coverage goals
- Audit secret usage
- Update documentation

## Resources

- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [Workflow Syntax](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions)
- [Security Hardening](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions)
- [GitHub Packages](https://docs.github.com/en/packages)

---

**Next Steps**: After setup, see [`ai-dev/tests/README.md`](ai-dev/tests/README.md) for testing documentation.
