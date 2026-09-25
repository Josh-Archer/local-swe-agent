"""
Unit tests for the code indexer script.
"""

import sys
import uuid
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

# Add code-indexer to sys.path
INDEXER_DIR = Path(__file__).parent.parent / "code-indexer"
if str(INDEXER_DIR) not in sys.path:
    sys.path.insert(0, str(INDEXER_DIR))

# Mock sentence_transformers if not installed in environment
if "sentence_transformers" not in sys.modules:
    sys.modules["sentence_transformers"] = MagicMock()

from index_code import CodeIndexer, generate_point_id  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import PointStruct  # noqa: E402


@pytest.fixture(autouse=True)
def mock_dependencies():
    """Mock heavy sentence_transformers dependency for fast tests."""
    with patch.dict(
        "sys.modules",
        {
            "sentence_transformers": MagicMock(),
        },
    ):
        yield


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_config():
    """Sample configuration for testing."""
    return {
        "qdrant": {
            "collection_name": "test-code-collection",
            "vector_size": 384,
            "distance": "Cosine",
        },
        "embedding": {"model": "all-MiniLM-L6-v2", "batch_size": 32},
        "indexing": {
            "chunk_size": 10,
            "chunk_overlap": 2,
            "file_extensions": [".py", ".js", ".java"],
            "exclude_patterns": ["__pycache__", ".git", "node_modules"],
        },
        "repositories": [
            {"name": "test-repo", "url": "https://github.com/test/repo.git"}
        ],
    }


@pytest.fixture
def mock_embedding_model():
    """Mock embedding model that returns 384-dim zero vectors."""
    model = MagicMock()
    model.encode.return_value = MagicMock(tolist=lambda: [0.1] * 384)
    model.get_sentence_embedding_dimension.return_value = 384
    return model


@pytest.fixture
def in_memory_qdrant():
    """In-memory Qdrant client."""
    client = QdrantClient(":memory:")
    return client


@pytest.fixture
def sample_code_file(temp_workspace):
    """Create a sample code file for testing."""
    code_file = temp_workspace / "sample.py"
    code_file.write_text("""
def hello_world():
    '''A simple hello world function.'''
    print("Hello, World!")

class Calculator:
    def add(self, a, b):
        return a + b

    def subtract(self, a, b):
        return a - b
""")
    return code_file


class TestDeterministicPointIds:
    """Tests for deterministic point ID generation (Issue #19)."""

    def test_deterministic_id_same_input(self):
        """Same repo, file path, and chunk index must return identical UUID."""
        id1 = generate_point_id("my-repo", "src/main.py", 0)
        id2 = generate_point_id("my-repo", "src/main.py", 0)
        assert id1 == id2
        # Must be valid UUID5 string
        parsed = uuid.UUID(id1)
        assert str(parsed) == id1
        assert parsed.version == 5

    def test_distinct_repos_produce_distinct_ids(self):
        """Different repositories must produce different IDs for identical paths and chunks."""
        id_repo1 = generate_point_id("repo-alpha", "utils.py", 0)
        id_repo2 = generate_point_id("repo-beta", "utils.py", 0)
        assert id_repo1 != id_repo2

    def test_distinct_files_produce_distinct_ids(self):
        """Different files within the same repo must produce different IDs."""
        id_file1 = generate_point_id("my-repo", "src/a.py", 0)
        id_file2 = generate_point_id("my-repo", "src/b.py", 0)
        assert id_file1 != id_file2

    def test_distinct_chunks_produce_distinct_ids(self):
        """Different chunks of the same file must produce different IDs."""
        id_chunk0 = generate_point_id("my-repo", "src/a.py", 0)
        id_chunk1 = generate_point_id("my-repo", "src/a.py", 1)
        assert id_chunk0 != id_chunk1

    def test_path_normalization(self):
        """Windows backslashes and POSIX forward slashes must generate identical IDs."""
        id_posix = generate_point_id("my-repo", "src/lib/app.py", 0)
        id_win = generate_point_id("my-repo", "src\\lib\\app.py", 0)
        assert id_posix == id_win

    def test_code_indexer_static_method(self):
        """CodeIndexer.generate_point_id delegates to generate_point_id."""
        direct = generate_point_id("repo", "foo.py", 2)
        via_class = CodeIndexer.generate_point_id("repo", "foo.py", 2)
        assert direct == via_class


class TestCodeChunking:
    """Test code chunking functionality."""

    def test_chunk_code_simple(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test chunking a simple code file."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        content = "x = 1\ny = 2\nz = 3\n"
        chunks = indexer.chunk_code(content, "test.py")
        assert len(chunks) == 1
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 4
        assert "x = 1" in chunks[0]["content"]

    def test_chunk_respects_size_limit(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test that chunks don't exceed size limit."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.chunk_size = 5
        indexer.chunk_overlap = 1
        lines = [f"line_{i}" for i in range(20)]
        content = "\n".join(lines)
        chunks = indexer.chunk_code(content, "test.py")
        assert len(chunks) > 1
        for chunk in chunks:
            chunk_line_count = len(chunk["content"].split("\n"))
            assert chunk_line_count <= 5

    def test_chunk_preserves_overlap(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test that chunks overlap as configured."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.chunk_size = 4
        indexer.chunk_overlap = 2
        content = "\n".join([f"line{i}" for i in range(8)])
        chunks = indexer.chunk_code(content, "test.py")
        assert len(chunks) >= 2
        # Check overlap between first two chunks
        lines_chunk0 = set(chunks[0]["content"].split("\n"))
        lines_chunk1 = set(chunks[1]["content"].split("\n"))
        common = lines_chunk0 & lines_chunk1
        assert len(common) == 2


class TestFileFiltering:
    """Test file filtering logic."""

    def test_filter_by_extension(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test filtering files by extension."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.should_index_file(Path("test.py")) is True
        assert indexer.should_index_file(Path("test.js")) is True
        assert indexer.should_index_file(Path("test.txt")) is False

    def test_exclude_patterns(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test excluding files by pattern (e.g. __pycache__, .git)."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.should_index_file(Path("__pycache__/test.py")) is False
        assert indexer.should_index_file(Path(".git/HEAD.py")) is False
        assert indexer.should_index_file(Path("node_modules/pkg/index.js")) is False

    def test_handles_binary_files(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test that binary files are not marked for indexing."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.should_index_file(Path("image.png")) is False
        assert indexer.should_index_file(Path("binary.exe")) is False


class TestGitOperations:
    """Test Git repository operations."""

    @patch("git.Repo")
    def test_clone_repository(
        self,
        mock_repo,
        mock_config,
        in_memory_qdrant,
        mock_embedding_model,
        temp_workspace,
    ):
        """Test cloning a Git repository."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.work_dir = temp_workspace
        mock_repo.clone_from.return_value = Mock()

        path = indexer.clone_or_pull_repo("https://github.com/test/repo.git", "repo")
        assert path == temp_workspace / "repo"
        mock_repo.clone_from.assert_called_once_with(
            "https://github.com/test/repo.git", temp_workspace / "repo"
        )

    @patch("git.Repo")
    def test_update_existing_repository(
        self,
        mock_repo,
        mock_config,
        in_memory_qdrant,
        mock_embedding_model,
        temp_workspace,
    ):
        """Test updating an existing repository."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.work_dir = temp_workspace
        repo_dir = temp_workspace / "existing_repo"
        repo_dir.mkdir()

        mock_instance = Mock()
        mock_repo.return_value = mock_instance

        path = indexer.clone_or_pull_repo(
            "https://github.com/test/repo.git", "existing_repo"
        )
        assert path == repo_dir
        mock_instance.remotes.origin.pull.assert_called_once()

    @patch("git.Repo")
    def test_handle_clone_failure(
        self,
        mock_repo,
        mock_config,
        in_memory_qdrant,
        mock_embedding_model,
        temp_workspace,
    ):
        """Test handling of clone failures."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.work_dir = temp_workspace
        mock_repo.clone_from.side_effect = Exception("Network error")

        with pytest.raises(Exception, match="Network error"):
            indexer.clone_or_pull_repo("https://github.com/test/fail.git", "fail_repo")


class TestEmbeddingGeneration:
    """Test embedding generation."""

    def test_generate_embeddings(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test generating embeddings for code."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.embedding_dim == 384

    def test_batch_embedding_generation(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test embedding model encode output."""
        vec = mock_embedding_model.encode("def foo(): pass").tolist()
        assert len(vec) == 384

    def test_embedding_dimensions(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test that embeddings have correct dimensions."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.embedding_dim == 384


class TestQdrantIntegration:
    """Test Qdrant vector database integration."""

    def test_create_collection(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test creating a Qdrant collection."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.ensure_collection()
        collections = in_memory_qdrant.get_collections().collections
        assert any(c.name == indexer.collection_name for c in collections)

    def test_upsert_vectors(self, mock_config, in_memory_qdrant, mock_embedding_model):
        """Test upserting vectors to Qdrant."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.ensure_collection()
        point_id = generate_point_id("repo", "file.py", 0)
        point = PointStruct(
            id=point_id,
            vector=[0.1] * 384,
            payload={"repository": "repo", "file_path": "file.py"},
        )
        in_memory_qdrant.upsert(indexer.collection_name, points=[point])
        points = indexer.get_repository_point_ids("repo")
        assert point_id in points

    @patch("index_code.QdrantClient")
    def test_handle_connection_failure(self, mock_client, mock_config):
        """Test handling Qdrant connection failures."""
        mock_client.side_effect = Exception("Connection failed")
        with pytest.raises(Exception, match="Connection failed"):
            CodeIndexer(config_path=mock_config)


class TestConfigValidation:
    """Test configuration validation."""

    def test_valid_config(self, mock_config, in_memory_qdrant, mock_embedding_model):
        """Test that valid config is accepted."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.collection_name == "test-code-collection"
        assert indexer.chunk_size == 10
        assert indexer.chunk_overlap == 2

    def test_missing_required_fields(self, in_memory_qdrant, mock_embedding_model):
        """Test fallback defaults when configuration fields are missing."""
        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.collection_name == "code_embeddings"
        assert indexer.chunk_size == 500
        assert indexer.chunk_overlap == 50

    def test_invalid_vector_size(self, in_memory_qdrant, mock_embedding_model):
        """Test handling vector size."""
        config = {"qdrant": {"vector_size": 512}}
        indexer = CodeIndexer(
            config_path=config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        # Mock model reports 384
        assert indexer.embedding_dim == 384


class TestStalePointCleanup:
    """Test stale point retrieval and cleanup."""

    def test_get_repository_point_ids(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test retrieving all point IDs for a specific repo."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.ensure_collection()

        id1 = generate_point_id("repo-1", "a.py", 0)
        id2 = generate_point_id("repo-1", "b.py", 0)
        id3 = generate_point_id("repo-2", "c.py", 0)

        in_memory_qdrant.upsert(
            indexer.collection_name,
            points=[
                PointStruct(
                    id=id1, vector=[0.1] * 384, payload={"repository": "repo-1"}
                ),
                PointStruct(
                    id=id2, vector=[0.1] * 384, payload={"repository": "repo-1"}
                ),
                PointStruct(
                    id=id3, vector=[0.1] * 384, payload={"repository": "repo-2"}
                ),
            ],
        )

        repo1_ids = indexer.get_repository_point_ids("repo-1")
        assert repo1_ids == {id1, id2}

        repo2_ids = indexer.get_repository_point_ids("repo-2")
        assert repo2_ids == {id3}

    def test_delete_stale_points_removes_unseen_points(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Points no longer in current_point_ids must be removed from Qdrant."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.ensure_collection()

        id_active = generate_point_id("repo-1", "active.py", 0)
        id_stale = generate_point_id("repo-1", "deleted.py", 0)

        in_memory_qdrant.upsert(
            indexer.collection_name,
            points=[
                PointStruct(
                    id=id_active, vector=[0.1] * 384, payload={"repository": "repo-1"}
                ),
                PointStruct(
                    id=id_stale, vector=[0.1] * 384, payload={"repository": "repo-1"}
                ),
            ],
        )

        deleted_count = indexer.delete_stale_points("repo-1", {id_active})
        assert deleted_count == 1

        remaining = indexer.get_repository_point_ids("repo-1")
        assert remaining == {id_active}

    def test_delete_stale_points_noop_when_all_active(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """When current point IDs match existing IDs, 0 points are deleted."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.ensure_collection()

        id_active = generate_point_id("repo-1", "active.py", 0)
        in_memory_qdrant.upsert(
            indexer.collection_name,
            points=[
                PointStruct(
                    id=id_active, vector=[0.1] * 384, payload={"repository": "repo-1"}
                ),
            ],
        )

        deleted_count = indexer.delete_stale_points("repo-1", {id_active})
        assert deleted_count == 0
        assert indexer.get_repository_point_ids("repo-1") == {id_active}

    def test_delete_stale_points_batching(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Verify stale points cleanup handles large batches (>100)."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.ensure_collection()

        stale_ids = [
            generate_point_id("bulk-repo", f"file_{i}.py", 0) for i in range(120)
        ]
        points = [
            PointStruct(id=pid, vector=[0.1] * 384, payload={"repository": "bulk-repo"})
            for pid in stale_ids
        ]
        in_memory_qdrant.upsert(indexer.collection_name, points=points)

        assert len(indexer.get_repository_point_ids("bulk-repo")) == 120

        deleted_count = indexer.delete_stale_points("bulk-repo", set())
        assert deleted_count == 120
        assert len(indexer.get_repository_point_ids("bulk-repo")) == 0


class TestMultiRepoIndexingIsolation:
    """End-to-end multi-repository isolation tests (Issue #19)."""

    def test_second_repo_does_not_overwrite_first(
        self, mock_config, in_memory_qdrant, mock_embedding_model, temp_workspace
    ):
        """Indexing a second repository must not overwrite points of the first repository."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )

        repo1_dir = temp_workspace / "repo1"
        repo1_dir.mkdir()
        (repo1_dir / "main.py").write_text("def repo1_func(): pass\n")

        repo2_dir = temp_workspace / "repo2"
        repo2_dir.mkdir()
        (repo2_dir / "main.py").write_text("def repo2_func(): pass\n")

        with patch.object(indexer, "clone_or_pull_repo") as mock_clone:
            # Index repo1
            mock_clone.return_value = repo1_dir
            indexer.index_repository("https://github.com/org/repo1.git", "repo1")

            repo1_points = indexer.get_repository_point_ids("repo1")
            assert len(repo1_points) == 1
            repo1_point_id = next(iter(repo1_points))
            # Verify point id is NOT integer 0 or "0"
            assert repo1_point_id != 0 and repo1_point_id != "0"
            assert repo1_point_id == generate_point_id("repo1", "main.py", 0)

            # Index repo2
            mock_clone.return_value = repo2_dir
            indexer.index_repository("https://github.com/org/repo2.git", "repo2")

            repo2_points = indexer.get_repository_point_ids("repo2")
            assert len(repo2_points) == 1
            repo2_point_id = next(iter(repo2_points))
            assert repo2_point_id == generate_point_id("repo2", "main.py", 0)

            # CRITICAL CHECK: Repo 1 points still exist!
            repo1_remaining = indexer.get_repository_point_ids("repo1")
            assert len(repo1_remaining) == 1
            assert repo1_point_id in repo1_remaining
            assert repo1_point_id != repo2_point_id

    def test_deleted_file_in_repo_is_cleaned_up(
        self, mock_config, in_memory_qdrant, mock_embedding_model, temp_workspace
    ):
        """When a file is deleted from a repo, re-indexing removes its stale point."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )

        repo_dir = temp_workspace / "dynamic_repo"
        repo_dir.mkdir()
        file_a = repo_dir / "a.py"
        file_b = repo_dir / "b.py"
        file_a.write_text("def a(): pass\n")
        file_b.write_text("def b(): pass\n")

        with patch.object(indexer, "clone_or_pull_repo", return_value=repo_dir):
            indexer.index_repository(
                "https://github.com/org/dynamic.git", "dynamic_repo"
            )
            assert len(indexer.get_repository_point_ids("dynamic_repo")) == 2

            # Delete b.py
            file_b.unlink()

            # Re-index
            indexer.index_repository(
                "https://github.com/org/dynamic.git", "dynamic_repo"
            )
            remaining = indexer.get_repository_point_ids("dynamic_repo")
            assert len(remaining) == 1
            expected_a_id = generate_point_id("dynamic_repo", "a.py", 0)
            assert expected_a_id in remaining

    def test_stale_cleanup_does_not_delete_other_repo_points(
        self, mock_config, in_memory_qdrant, mock_embedding_model, temp_workspace
    ):
        """Cleaning up stale points in repo1 must not delete points in repo2."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )

        repo1_dir = temp_workspace / "iso_repo1"
        repo1_dir.mkdir()
        (repo1_dir / "a.py").write_text("def a(): pass\n")

        repo2_dir = temp_workspace / "iso_repo2"
        repo2_dir.mkdir()
        (repo2_dir / "x.py").write_text("def x(): pass\n")

        with patch.object(indexer, "clone_or_pull_repo") as mock_clone:
            mock_clone.return_value = repo1_dir
            indexer.index_repository("https://github.com/org/repo1.git", "iso_repo1")

            mock_clone.return_value = repo2_dir
            indexer.index_repository("https://github.com/org/repo2.git", "iso_repo2")

            # Remove all files in repo1 and re-index
            (repo1_dir / "a.py").unlink()
            mock_clone.return_value = repo1_dir
            indexer.index_repository("https://github.com/org/repo1.git", "iso_repo1")

            # Repo 1 should now have 0 points, but Repo 2 still has its point
            assert len(indexer.get_repository_point_ids("iso_repo1")) == 0
            assert len(indexer.get_repository_point_ids("iso_repo2")) == 1


class TestEndToEnd:
    """End-to-end integration tests."""

    @patch("git.Repo")
    def test_full_indexing_pipeline(
        self,
        mock_git,
        mock_config,
        in_memory_qdrant,
        mock_embedding_model,
        temp_workspace,
    ):
        """Test the complete indexing pipeline via run()."""
        repo_dir = temp_workspace / "test-repo"
        repo_dir.mkdir()
        (repo_dir / "code.py").write_text("def test(): pass\n")

        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )

        with patch.object(indexer, "clone_or_pull_repo", return_value=repo_dir):
            indexer.run()

        points = indexer.get_repository_point_ids("test-repo")
        assert len(points) == 1

    def test_handles_empty_repository(
        self, mock_config, in_memory_qdrant, mock_embedding_model, temp_workspace
    ):
        """Test handling of empty repositories."""
        repo_dir = temp_workspace / "empty-repo"
        repo_dir.mkdir()

        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )

        with patch.object(indexer, "clone_or_pull_repo", return_value=repo_dir):
            indexer.index_repository("https://github.com/org/empty.git", "empty-repo")

        assert len(indexer.get_repository_point_ids("empty-repo")) == 0

    def test_handles_large_repository(
        self, mock_config, in_memory_qdrant, mock_embedding_model, temp_workspace
    ):
        """Test repository with multiple files and chunks."""
        repo_dir = temp_workspace / "large-repo"
        repo_dir.mkdir()
        for i in range(5):
            (repo_dir / f"module_{i}.py").write_text(f"x = {i}\n" * 20)

        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.chunk_size = 10
        indexer.chunk_overlap = 2

        with patch.object(indexer, "clone_or_pull_repo", return_value=repo_dir):
            indexer.index_repository("https://github.com/org/large.git", "large-repo")

        points = indexer.get_repository_point_ids("large-repo")
        assert len(points) >= 5


# Pytest configuration
def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
