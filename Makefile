.PHONY: help install test lint format validate deploy clean docker-build

# Colors for output
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[1;33m
NC := \033[0m

help: ## Show this help message
	@echo '$(BLUE)AI-Dev System - Available Commands$(NC)'
	@echo ''
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ''

# Development Setup
install: ## Install all dependencies
	@echo '$(BLUE)Installing dependencies...$(NC)'
	pip install -r ai-dev/tests/requirements-test.txt
	pip install pre-commit
	pre-commit install
	@echo '$(GREEN)Dependencies installed!$(NC)'

install-dev: install ## Install development tools
	@echo '$(BLUE)Installing development tools...$(NC)'
	pip install ipython jupyter
	@echo '$(GREEN)Development tools installed!$(NC)'

# Testing
test: ## Run all tests
	@echo '$(BLUE)Running tests...$(NC)'
	pytest ai-dev/tests/ -v

test-unit: ## Run unit tests only
	@echo '$(BLUE)Running unit tests...$(NC)'
	pytest ai-dev/tests/ -v -m "not integration and not slow"

test-integration: ## Run integration tests
	@echo '$(BLUE)Running integration tests...$(NC)'
	pytest ai-dev/tests/ -v -m integration

test-coverage: ## Run tests with coverage report
	@echo '$(BLUE)Running tests with coverage...$(NC)'
	pytest ai-dev/tests/ --cov=ai-dev/code-indexer --cov-report=html --cov-report=term
	@echo '$(GREEN)Coverage report: htmlcov/index.html$(NC)'

test-watch: ## Run tests in watch mode
	@echo '$(BLUE)Running tests in watch mode...$(NC)'
	ptw ai-dev/tests/ -- -v

# Code Quality
lint: ## Run all linters
	@echo '$(BLUE)Running linters...$(NC)'
	black --check ai-dev/code-indexer/ ai-dev/tests/
	flake8 ai-dev/code-indexer/ ai-dev/tests/ --max-line-length=120
	pylint ai-dev/code-indexer/index_code.py --disable=C,R --max-line-length=120 || true
	@echo '$(GREEN)Linting complete!$(NC)'

format: ## Format code with black
	@echo '$(BLUE)Formatting code...$(NC)'
	black ai-dev/code-indexer/ ai-dev/tests/
	@echo '$(GREEN)Code formatted!$(NC)'

lint-yaml: ## Validate YAML files
	@echo '$(BLUE)Validating YAML files...$(NC)'
	yamllint ai-dev/
	@echo '$(GREEN)YAML validation complete!$(NC)'

lint-shell: ## Validate shell scripts
	@echo '$(BLUE)Validating shell scripts...$(NC)'
	shellcheck ai-dev/scripts/*.sh
	@echo '$(GREEN)Shell script validation complete!$(NC)'

security-check: ## Run security scans
	@echo '$(BLUE)Running security checks...$(NC)'
	bandit -r ai-dev/code-indexer/ -ll
	safety check
	@echo '$(GREEN)Security checks complete!$(NC)'

# Kubernetes Validation
validate: ## Validate all Kubernetes manifests
	@echo '$(BLUE)Validating Kubernetes manifests...$(NC)'
	@find ai-dev -name "*.yaml" -type f | while read -r file; do \
		echo "Validating $$file"; \
		kubeval --ignore-missing-schemas "$$file" || exit 1; \
	done
	@echo '$(GREEN)Manifest validation complete!$(NC)'

validate-strict: ## Strict validation with kubeconform
	@echo '$(BLUE)Strict validation with kubeconform...$(NC)'
	@find ai-dev -name "*.yaml" -type f | while read -r file; do \
		echo "Validating $$file"; \
		kubeconform -strict -ignore-missing-schemas "$$file" || exit 1; \
	done
	@echo '$(GREEN)Strict validation complete!$(NC)'

# Docker
docker-build: ## Build code-indexer Docker image
	@echo '$(BLUE)Building Docker image...$(NC)'
	cd ai-dev/code-indexer && docker build -t code-indexer:latest .
	@echo '$(GREEN)Docker image built: code-indexer:latest$(NC)'

docker-test: docker-build ## Build and test Docker image
	@echo '$(BLUE)Testing Docker image...$(NC)'
	docker run --rm code-indexer:latest python3 --version
	docker run --rm code-indexer:latest python3 -c "import qdrant_client; import git; print('Dependencies OK')"
	@echo '$(GREEN)Docker image test passed!$(NC)'

docker-scan: docker-build ## Scan Docker image for vulnerabilities
	@echo '$(BLUE)Scanning Docker image...$(NC)'
	trivy image code-indexer:latest
	@echo '$(GREEN)Docker scan complete!$(NC)'

docker-push: docker-build ## Push Docker image to registry
	@echo '$(BLUE)Pushing Docker image...$(NC)'
	@read -p "Enter registry (e.g., ghcr.io/username): " registry; \
	docker tag code-indexer:latest $$registry/code-indexer:latest; \
	docker push $$registry/code-indexer:latest
	@echo '$(GREEN)Docker image pushed!$(NC)'

# Deployment
deploy-dry-run: validate ## Dry run deployment
	@echo '$(BLUE)Running deployment dry-run...$(NC)'
	kubectl apply -f ai-dev/namespace/namespace.yaml --dry-run=server
	kubectl apply -f ai-dev/storage/pvcs.yaml --dry-run=server
	kubectl apply -f ai-dev/vllm/vllm-configmap.yaml --dry-run=server
	kubectl apply -f ai-dev/vllm/vllm-deployment.yaml --dry-run=server
	@echo '$(GREEN)Dry run successful!$(NC)'

deploy: ## Deploy to Kubernetes (uses deploy-safe.sh)
	@echo '$(BLUE)Starting safe deployment...$(NC)'
	bash ai-dev/scripts/deploy-safe.sh

deploy-force: ## Deploy without validation gates
	@echo '$(YELLOW)Warning: Deploying without validation gates!$(NC)'
	INTERACTIVE=0 bash ai-dev/scripts/deploy-safe.sh

rollback: ## Rollback deployment
	@echo '$(YELLOW)Rolling back deployment...$(NC)'
	kubectl delete namespace ai-dev
	@echo '$(GREEN)Rollback complete!$(NC)'

# Monitoring
status: ## Check deployment status
	@echo '$(BLUE)Checking deployment status...$(NC)'
	kubectl get all -n ai-dev
	@echo ''
	kubectl get pvc -n ai-dev

logs-vllm: ## View vLLM logs
	kubectl logs -n ai-dev -l app=vllm -f

logs-qdrant: ## View Qdrant logs
	kubectl logs -n ai-dev -l app=qdrant -f

logs-indexer: ## View code indexer logs
	kubectl logs -n ai-dev -l app=code-indexer -f --tail=100

port-forward: ## Start port-forward to vLLM
	@echo '$(BLUE)Starting port-forward to vLLM...$(NC)'
	@echo 'Access at: http://localhost:8000'
	kubectl port-forward -n ai-dev svc/vllm-server 8000:8000

# Testing API
test-api: ## Test vLLM API
	@echo '$(BLUE)Testing vLLM API...$(NC)'
	python3 ai-dev/scripts/test-vllm-api.py --url http://localhost:8000

health-check: ## Check all component health
	@echo '$(BLUE)Running health checks...$(NC)'
	@echo 'vLLM:' && kubectl exec -n ai-dev -l app=vllm -- curl -f http://localhost:8000/health || echo 'Failed'
	@echo 'Qdrant:' && kubectl exec -n ai-dev -l app=qdrant -- curl -f http://localhost:6333 || echo 'Failed'
	@echo 'Plex:' && bash ai-dev/scripts/check-plex-health.sh || echo 'Failed'

# Cleanup
clean: ## Clean temporary files
	@echo '$(BLUE)Cleaning temporary files...$(NC)'
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '.pytest_cache' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	rm -rf htmlcov/ .coverage coverage.xml
	@echo '$(GREEN)Cleanup complete!$(NC)'

clean-docker: ## Remove Docker images and containers
	@echo '$(BLUE)Cleaning Docker artifacts...$(NC)'
	docker rmi code-indexer:latest 2>/dev/null || true
	@echo '$(GREEN)Docker cleanup complete!$(NC)'

# Git
pre-commit-run: ## Run pre-commit on all files
	@echo '$(BLUE)Running pre-commit hooks...$(NC)'
	pre-commit run --all-files
	@echo '$(GREEN)Pre-commit checks complete!$(NC)'

pre-commit-update: ## Update pre-commit hooks
	@echo '$(BLUE)Updating pre-commit hooks...$(NC)'
	pre-commit autoupdate
	@echo '$(GREEN)Pre-commit hooks updated!$(NC)'

# Documentation
docs-serve: ## Serve documentation locally
	@echo '$(BLUE)Serving documentation...$(NC)'
	@echo 'Open http://localhost:8000'
	python3 -m http.server 8000 --directory .

# CI/CD Simulation
ci-local: lint test validate ## Run all CI checks locally
	@echo '$(GREEN)All CI checks passed!$(NC)'

ci-docker: docker-build docker-test docker-scan ## Run Docker CI checks
	@echo '$(GREEN)Docker CI checks passed!$(NC)'

# Quick Start
quickstart: install validate test ## Quick start - install, validate, test
	@echo '$(GREEN)✓ Installation complete$(NC)'
	@echo '$(GREEN)✓ Manifests validated$(NC)'
	@echo '$(GREEN)✓ Tests passed$(NC)'
	@echo ''
	@echo '$(BLUE)Next steps:$(NC)'
	@echo '  1. Configure your repositories in ai-dev/code-indexer/configmap.yaml'
	@echo '  2. Build Docker image: make docker-build'
	@echo '  3. Deploy: make deploy'
