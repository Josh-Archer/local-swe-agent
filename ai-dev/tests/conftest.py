"""
Shared pytest fixtures and test utilities.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock
import yaml


@pytest.fixture(scope="session")
def test_data_dir():
    """Path to test data directory."""
    return Path(__file__).parent / "data"


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_config():
    """Sample YAML configuration for testing."""
    return {
        "qdrant": {
            "url": "http://localhost:6333",
            "collection_name": "test-code",
            "vector_size": 384,
            "distance": "Cosine",
        },
        "embedding": {"model": "all-MiniLM-L6-v2", "batch_size": 32, "max_length": 512},
        "indexing": {
            "chunk_size": 500,
            "chunk_overlap": 50,
            "file_extensions": [".py", ".js", ".ts", ".java", ".go"],
            "exclude_patterns": [
                "__pycache__",
                ".git",
                "node_modules",
                "venv",
                ".pytest_cache",
            ],
        },
        "repositories": [
            {
                "name": "test-repo",
                "url": "https://github.com/test/repo.git",
                "branch": "main",
            }
        ],
    }


@pytest.fixture
def config_file(temp_dir, sample_config):
    """Create a temporary config file."""
    config_path = temp_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(sample_config, f)
    return config_path


@pytest.fixture
def sample_python_code():
    """Sample Python code for testing."""
    return '''
"""Module docstring."""

import os
import sys
from typing import List, Dict

class SampleClass:
    """A sample class for testing."""

    def __init__(self, name: str):
        self.name = name

    def greet(self) -> str:
        """Return a greeting."""
        return f"Hello, {self.name}!"

def process_data(items: List[str]) -> Dict[str, int]:
    """Process a list of items.

    Args:
        items: List of strings to process

    Returns:
        Dictionary mapping items to their lengths
    """
    result = {}
    for item in items:
        result[item] = len(item)
    return result

if __name__ == "__main__":
    obj = SampleClass("World")
    print(obj.greet())
'''


@pytest.fixture
def sample_javascript_code():
    """Sample JavaScript code for testing."""
    return """
/**
 * A sample JavaScript module
 */

class Calculator {
    constructor() {
        this.result = 0;
    }

    add(a, b) {
        this.result = a + b;
        return this.result;
    }

    multiply(a, b) {
        this.result = a * b;
        return this.result;
    }
}

function fibonacci(n) {
    if (n <= 1) return n;
    return fibonacci(n - 1) + fibonacci(n - 2);
}

export { Calculator, fibonacci };
"""


@pytest.fixture
def mock_qdrant_client():
    """Mock Qdrant client for testing."""
    client = MagicMock()
    client.get_collections.return_value = Mock(collections=[])
    client.create_collection.return_value = True
    client.upsert.return_value = Mock(status="completed")
    return client


@pytest.fixture
def mock_sentence_transformer():
    """Mock SentenceTransformer model for testing."""
    model = MagicMock()
    # Return embeddings of correct size (384 dimensions)
    model.encode.return_value = [[0.1] * 384]
    model.get_sentence_embedding_dimension.return_value = 384
    return model


@pytest.fixture
def mock_git_repo():
    """Mock Git repository for testing."""
    repo = MagicMock()
    repo.working_dir = "/tmp/test-repo"
    repo.remotes.origin.url = "https://github.com/test/repo.git"
    repo.head.commit.hexsha = "abc123"
    return repo


@pytest.fixture
def create_test_repo(temp_dir):
    """Factory fixture to create test repositories."""

    def _create_repo(name: str, files: dict):
        """
        Create a test repository with specified files.

        Args:
            name: Repository name
            files: Dict mapping file paths to content
        """
        repo_dir = temp_dir / name
        repo_dir.mkdir(parents=True, exist_ok=True)

        for file_path, content in files.items():
            full_path = repo_dir / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)

        return repo_dir

    return _create_repo


# Pytest hooks
def pytest_collection_modifyitems(config, items):
    """Modify test collection to add markers automatically."""
    for item in items:
        # Mark integration tests
        if "integration" in item.nodeid:
            item.add_marker(pytest.mark.integration)

        # Mark slow tests
        if "slow" in item.nodeid or "large" in item.nodeid:
            item.add_marker(pytest.mark.slow)
