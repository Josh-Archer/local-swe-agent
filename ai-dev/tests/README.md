# AI-Dev Testing Infrastructure

Comprehensive testing suite for the AI-Dev Kubernetes deployment system.

## Quick Start

```bash
# Install test dependencies
pip install -r tests/requirements-test.txt

# Run all tests
pytest ai-dev/tests/

# Run with coverage
pytest ai-dev/tests/ --cov=ai-dev/code-indexer --cov-report=html

# Run only unit tests
pytest ai-dev/tests/ -m "not integration and not slow"

# Run specific test file
pytest ai-dev/tests/test_code_indexer.py -v
```

## Test Structure

```
ai-dev/tests/
├── README.md                 # This file
├── pytest.ini                # Pytest configuration
├── conftest.py               # Shared fixtures
├── requirements-test.txt     # Test dependencies
├── test_code_indexer.py      # Code indexer unit tests
├── test_scripts.py           # Shell script validation
└── data/                     # Test data files (if needed)
```

## Test Categories

### Unit Tests
Fast tests that mock external dependencies:
```bash
pytest -m unit
```

### Integration Tests
Tests requiring external services (Qdrant, Git):
```bash
pytest -m integration
```

### Slow Tests
Tests that take significant time (large datasets, model loading):
```bash
pytest -m slow
```

## GitHub Actions Workflows

### CI - Manifest Validation
**File**: `.github/workflows/ci.yml`

Validates:
- Kubernetes manifest syntax with `kubeval`
- Strict schema validation with `kubeconform`
- YAML linting with `yamllint`
- Shell script linting with `shellcheck`
- Security scanning with `trivy`
- Secret detection with `trufflehog`
- Dry-run deployment in Kind cluster

**Triggers**: Push to main/develop, Pull Requests

### Python Tests
**File**: `.github/workflows/python-tests.yml`

Tests:
- Python 3.10 and 3.11 compatibility
- Code formatting with `black`
- Linting with `flake8`
- Type checking with `mypy`
- Security scanning with `bandit`
- Dependency vulnerability scanning with `safety`
- Unit test execution with coverage reporting

**Triggers**: Push to code-indexer/, tests/, Pull Requests

### Docker Build & Test
**File**: `.github/workflows/docker-build.yml`

Builds and tests:
- Docker image builds with caching
- Dependency installation verification
- Security scanning with `trivy`
- Dockerfile linting with `hadolint`
- Automatic push to GHCR on main branch

**Triggers**: Push to Dockerfile, Python files, Pull Requests

## Running Tests Locally

### Prerequisites
```bash
# Install system dependencies (Ubuntu/Debian)
sudo apt-get install -y shellcheck yamllint

# Install Python test dependencies
cd ai-dev
pip install -r tests/requirements-test.txt
```

### Test Commands

#### All Tests
```bash
pytest tests/ -v
```

#### With Coverage
```bash
pytest tests/ --cov=code-indexer --cov-report=html --cov-report=term
open htmlcov/index.html  # View coverage report
```

#### Fast Tests Only
```bash
pytest tests/ -m "not slow" --maxfail=1
```

#### Specific Test Class
```bash
pytest tests/test_code_indexer.py::TestCodeChunking -v
```

#### Watch Mode (re-run on file changes)
```bash
pip install pytest-watch
ptw tests/ -- -v
```

### Manual Validation

#### Validate Kubernetes Manifests
```bash
# Using kubeval
find ai-dev -name "*.yaml" | xargs kubeval --ignore-missing-schemas

# Using kubeconform
find ai-dev -name "*.yaml" | xargs kubeconform -strict
```

#### Lint Shell Scripts
```bash
shellcheck ai-dev/scripts/*.sh
```

#### Lint YAML
```bash
yamllint ai-dev/
```

#### Security Scan
```bash
# Scan configurations
trivy config ai-dev/

# Scan Docker image
docker build -t code-indexer:test ai-dev/code-indexer/
trivy image code-indexer:test
```

## Writing Tests

### Example Unit Test
```python
def test_chunk_code(sample_python_code):
    """Test code chunking functionality."""
    chunks = chunk_code(sample_python_code, chunk_size=500)

    assert len(chunks) > 0
    assert all(len(chunk) <= 500 for chunk in chunks)
```

### Example Integration Test
```python
@pytest.mark.integration
def test_qdrant_connection(mock_qdrant_client):
    """Test connecting to Qdrant."""
    client = get_qdrant_client()
    collections = client.get_collections()

    assert collections is not None
```

### Using Fixtures
```python
def test_with_temp_repo(create_test_repo, sample_python_code):
    """Test using factory fixture."""
    repo = create_test_repo("test-repo", {
        "main.py": sample_python_code,
        "utils.py": "def helper(): pass"
    })

    assert (repo / "main.py").exists()
```

## Continuous Integration

### Status Badges

Add to your README.md:
```markdown
![CI](https://github.com/USERNAME/REPO/workflows/CI%20-%20Validate%20Manifests/badge.svg)
![Python Tests](https://github.com/USERNAME/REPO/workflows/Python%20Tests/badge.svg)
![Docker Build](https://github.com/USERNAME/REPO/workflows/Docker%20Build%20%26%20Test/badge.svg)
```

### Required Checks

Configure these as required status checks in GitHub:
- `validate-manifests`
- `lint-shell-scripts`
- `check-security`
- `test-python-code`
- `build-and-test` (Docker)

### Pre-commit Hooks

Install pre-commit hooks for local validation:
```bash
pip install pre-commit
pre-commit install
```

Create `.pre-commit-config.yaml`:
```yaml
repos:
  - repo: https://github.com/psf/black
    rev: 23.7.0
    hooks:
      - id: black
        language_version: python3.11

  - repo: https://github.com/pycqa/flake8
    rev: 6.1.0
    hooks:
      - id: flake8
        args: [--max-line-length=120]

  - repo: https://github.com/adrienverge/yamllint
    rev: v1.32.0
    hooks:
      - id: yamllint

  - repo: https://github.com/koalaman/shellcheck-precommit
    rev: v0.9.0
    hooks:
      - id: shellcheck
```

## Coverage Goals

- **Unit Tests**: > 80% coverage
- **Integration Tests**: > 60% coverage
- **Overall**: > 75% coverage

View coverage:
```bash
pytest --cov=code-indexer --cov-report=html
open htmlcov/index.html
```

## Performance Benchmarking

```bash
# Install pytest-benchmark
pip install pytest-benchmark

# Run benchmarks
pytest tests/test_code_indexer.py --benchmark-only
```

## Debugging Tests

```bash
# Run with verbose output and show print statements
pytest tests/ -v -s

# Drop into debugger on failure
pytest tests/ --pdb

# Run last failed tests only
pytest tests/ --lf

# Run specific test with extra verbosity
pytest tests/test_code_indexer.py::TestCodeChunking::test_chunk_code -vv
```

## Test Data

Place test data files in `tests/data/`:
```
tests/data/
├── sample-repos/
│   └── python-project/
│       ├── main.py
│       └── utils.py
├── configs/
│   └── test-config.yaml
└── fixtures/
    └── embeddings.json
```

## Contributing

When adding new features:
1. Write tests first (TDD)
2. Ensure all tests pass locally
3. Run linters and formatters
4. Check coverage doesn't decrease
5. Update this README if adding new test categories

## Troubleshooting

### Test Failures

**Import errors**:
```bash
# Ensure you're in the right directory
cd ai-dev
# Install dependencies
pip install -r tests/requirements-test.txt
```

**Fixture not found**:
- Check `conftest.py` for fixture definitions
- Ensure fixture scope is correct

**Slow tests**:
```bash
# Skip slow tests
pytest -m "not slow"
```

### GitHub Actions Failures

**Manifest validation fails**:
- Run `kubeval` locally on the changed files
- Check for syntax errors in YAML

**Docker build fails**:
- Test build locally: `docker build -t test ai-dev/code-indexer/`
- Check Dockerfile syntax

**Security scan fails**:
- Review Trivy output for vulnerabilities
- Update dependencies if needed

## Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [Kubernetes YAML Validation](https://www.kubeval.com/)
- [GitHub Actions](https://docs.github.com/en/actions)
- [Trivy Security Scanner](https://aquasecurity.github.io/trivy/)
