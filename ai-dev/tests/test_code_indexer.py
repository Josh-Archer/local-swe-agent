"""
Unit tests for the code indexer script.
"""

import logging
import sys
import uuid
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml

# Add code-indexer to sys.path
INDEXER_DIR = Path(__file__).parent.parent / "code-indexer"
if str(INDEXER_DIR) not in sys.path:
    sys.path.insert(0, str(INDEXER_DIR))

# Mock sentence_transformers if not installed in environment
if "sentence_transformers" not in sys.modules:
    sys.modules["sentence_transformers"] = MagicMock()

from index_code import (  # noqa: E402
    CodeIndexer,
    CodeRetriever,
    generate_point_id,
    main,
    MAX_TOP_K,
    DEFAULT_MAX_CONTEXT_BYTES,
    is_secret_path,
    is_secret_content,
)
from inject_context import (  # noqa: E402
    inject_context_into_system_template,
    redact_secrets,
)
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
    # Leave tokenizer unset so production code does not treat a mock as one.
    model.tokenizer = None
    return model


class WordCountTokenizer:
    """One token per whitespace word. Used to make chunk tests deterministic."""

    def encode(self, text, add_special_tokens=False):
        if not text or not str(text).strip():
            return []
        return [1] * len(str(text).split())


class InflatingOffsetTokenizer:
    """Offsets point at original words; re-encoding can exceed the window.

    Words containing '_' count as two tokens, so a window of N offset spans
    re-encodes above N and must be shrunk. ``decode`` lowercases and is not a
    substring of the source; a splitter that decodes token ids fails both the
    exact-substring and the token-limit checks.
    """

    def encode(self, text, add_special_tokens=False):
        if text is None:
            return []
        raw = str(text)
        if "⟦decoded⟧" in raw:
            return [0] * (len(raw.split()) + 30)
        count = 0
        for word in raw.split():
            count += 2 if "_" in word else 1
        return [1] * count

    def decode(self, token_ids, skip_special_tokens=True):
        return "⟦decoded⟧ " + " ".join("tok" for _ in list(token_ids))

    def __call__(
        self, text, add_special_tokens=False, return_offsets_mapping=False, **kwargs
    ):
        raw = str(text)
        spans = []
        index = 0
        length = len(raw)
        while index < length:
            while index < length and raw[index].isspace():
                index += 1
            if index >= length:
                break
            end = index + 1
            while end < length and not raw[end].isspace():
                end += 1
            spans.append((index, end))
            index = end
        encoded = {"input_ids": [1] * len(spans)}
        if return_offsets_mapping:
            encoded["offset_mapping"] = spans
        return encoded


def _load_local_minilm_tokenizer():
    """Return the cached MiniLM tokenizer, or None when it is not on disk."""
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(
            "sentence-transformers/all-MiniLM-L6-v2",
            local_files_only=True,
        )
    except Exception:
        return None


def _assert_no_contained_line_ranges(chunks):
    """Fail when a chunk's line range sits entirely inside an earlier chunk.

    Several pieces of one over-long line share that line number; those are
    checked separately by requiring each piece to extend source coverage.
    """
    for index, chunk in enumerate(chunks):
        for prev in chunks[:index]:
            same_line = (
                chunk["start_line"] == chunk["end_line"]
                and prev["start_line"] == prev["end_line"]
                and chunk["start_line"] == prev["start_line"]
            )
            if same_line:
                continue
            contained = (
                chunk["start_line"] >= prev["start_line"]
                and chunk["end_line"] <= prev["end_line"]
            )
            assert not contained, (
                f"chunk lines {chunk['start_line']}-{chunk['end_line']} "
                f"are inside {prev['start_line']}-{prev['end_line']}"
            )


def _assert_exact_advancing_slices(chunks, original):
    """Each piece is an exact slice and ends further along than the previous."""
    prev_start = -1
    prev_end = 0
    for chunk in chunks:
        content = chunk["content"]
        idx = original.find(content, prev_start + 1)
        assert idx >= 0, f"piece is not an exact substring: {content[:80]!r}"
        end = idx + len(content)
        assert original[idx:end] == content
        assert end > prev_end
        prev_start = idx
        prev_end = end
    assert chunks, "expected at least one piece"
    assert prev_end >= len(original.rstrip())
    body_start = len(original) - len(original.lstrip())
    first = original.find(chunks[0]["content"])
    assert first <= body_start


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

    def test_chunk_to_embedder_window(self, in_memory_qdrant, mock_embedding_model):
        """Test that chunks fit the embedder context window (Issue #17)."""
        mock_embedding_model.max_seq_length = 256
        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.chunk_size == 256
        # Generate code with 500 lines that would exceed context window if chunked by 500 lines
        lines = [f"x_{i} = calculate_value({i}, arg=True)" for i in range(500)]
        content = "\n".join(lines)
        chunks = indexer.chunk_code(content, "test.py")

        assert len(chunks) > 1
        for chunk in chunks:
            token_count = indexer.count_tokens(chunk["content"])
            assert token_count <= indexer.chunk_size
            assert chunk["start_line"] <= chunk["end_line"]

    def test_chunk_capping_to_embedder_window(
        self, in_memory_qdrant, mock_embedding_model
    ):
        """Test that oversized chunk_size config is capped to embedder window (Issue #17)."""
        mock_embedding_model.max_seq_length = 256
        config = {
            "indexing": {
                "chunk_size": 1000,
                "chunk_overlap": 50,
            }
        }
        indexer = CodeIndexer(
            config_path=config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        # Must be capped to 256 (the embedder window)
        assert indexer.chunk_size == 256
        assert indexer.chunk_overlap == 50

    def test_chunk_with_custom_tokenizer(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test chunking with a custom tokenizer (Issue #17)."""
        mock_tokenizer = MagicMock()
        mock_tokenizer.encode.side_effect = lambda text, **kwargs: [1] * len(
            text.split()
        )

        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
            tokenizer=mock_tokenizer,
        )
        indexer.chunk_size = 10
        indexer.chunk_overlap = 2

        # 5 words per line -> each line is 5 tokens
        lines = [f"word1 word2 word3 word4 line_{i}" for i in range(10)]
        content = "\n".join(lines)
        chunks = indexer.chunk_code(content, "test.py")

        assert len(chunks) > 1
        for chunk in chunks:
            assert indexer.count_tokens(chunk["content"]) <= 10

    def test_chunk_splits_long_single_line(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test that an unusually long single line is split into sub-chunks (Issue #17)."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.chunk_size = 20
        indexer.chunk_overlap = 5
        # Single line with 50 words
        long_line = " ".join([f"var_{i}" for i in range(50)])
        chunks = indexer.chunk_code(long_line, "long.py")

        assert len(chunks) >= 3
        for chunk in chunks:
            assert chunk["start_line"] == 1
            assert chunk["end_line"] == 1
            assert indexer.count_tokens(chunk["content"]) <= 20

    def test_chunk_empty_and_whitespace(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        """Test that empty or whitespace-only code returns no chunks."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.chunk_code("", "empty.py") == []
        assert indexer.chunk_code("   \n\n\t  \n  ", "whitespace.py") == []

    def test_chunk_real_tokenizer_integration(self, in_memory_qdrant):
        """Test token-aware chunking with real transformers AutoTokenizer if available."""
        try:
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(
                "sentence-transformers/all-MiniLM-L6-v2", local_files_only=True
            )
        except Exception:
            pytest.skip("Local HuggingFace model cache not available")

        model = MagicMock()
        model.max_seq_length = 256
        model.encode.return_value = MagicMock(tolist=lambda: [0.1] * 384)
        model.get_sentence_embedding_dimension.return_value = 384

        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=model,
            tokenizer=tokenizer,
        )
        assert indexer.chunk_size == 256

        # Multi-line python function repeated to exceed 256 tokens
        sample = """def calculate_metrics(values):
    total = sum(values)
    count = len(values)
    mean = total / count if count > 0 else 0
    return {
        'total': total,
        'count': count,
        'mean': mean,
    }
""" * 10
        chunks = indexer.chunk_code(sample, "metrics.py")
        assert len(chunks) > 1
        for chunk in chunks:
            token_count = len(
                tokenizer.encode(chunk["content"], add_special_tokens=False)
            )
            assert token_count <= 254
            full_encoded = len(
                tokenizer.encode(chunk["content"], add_special_tokens=True)
            )
            assert full_encoded <= 256

    def test_long_line_pieces_stay_within_limit_and_are_exact_substrings(
        self, in_memory_qdrant, mock_embedding_model
    ):
        """Over-long lines shrink to the token limit and keep original text.

        Re-encoding an offset window of ``Value_N`` words is longer than the
        window, so the splitter must shrink. Decoding token ids would lowercase
        the text, drop ``🙂`` / ``é`` / ``字``, and blow past the limit.
        """
        tokenizer = InflatingOffsetTokenizer()
        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
            tokenizer=tokenizer,
        )
        indexer.max_seq_length = 256
        indexer.chunk_size = 8
        indexer.chunk_overlap = 2
        content_limit = min(indexer.chunk_size, indexer.max_seq_length - 2)

        line = "Value_0 🙂 café 字 " + " ".join(f"Value_{i}" for i in range(30))
        chunks = indexer.chunk_code(line, "long.py")

        assert len(chunks) > 1
        _assert_exact_advancing_slices(chunks, line)
        _assert_no_contained_line_ranges(chunks)
        for chunk in chunks:
            assert chunk["start_line"] == 1 and chunk["end_line"] == 1
            assert indexer.count_tokens(chunk["content"]) <= content_limit
            assert "⟦decoded⟧" not in chunk["content"]
        joined = "\n".join(chunk["content"] for chunk in chunks)
        assert "Value_0" in joined
        assert "🙂" in joined
        assert "é" in joined
        assert "字" in joined
        assert "value _ 0" not in joined
        for index in range(30):
            assert f"Value_{index}" in joined

    def test_single_span_over_limit_is_sliced_without_dropping_characters(
        self, in_memory_qdrant, mock_embedding_model
    ):
        """A span whose text re-encodes over the limit is cut on the original string."""

        class CharTokenizer:
            def encode(self, text, add_special_tokens=False):
                return [1] * len(str(text))

            def decode(self, token_ids, skip_special_tokens=True):
                return "decoded"

            def __call__(
                self,
                text,
                add_special_tokens=False,
                return_offsets_mapping=False,
                **kwargs,
            ):
                raw = str(text)
                encoded = {"input_ids": [1]}
                if return_offsets_mapping:
                    encoded["offset_mapping"] = [(0, len(raw))]
                return encoded

        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
            tokenizer=CharTokenizer(),
        )
        indexer.max_seq_length = 256
        indexer.chunk_size = 5
        indexer.chunk_overlap = 1
        line = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        chunks = indexer.chunk_code(line, "span.py")
        content_limit = min(indexer.chunk_size, indexer.max_seq_length - 2)
        assert len(chunks) > 1
        _assert_exact_advancing_slices(chunks, line)
        pos = 0
        for chunk in chunks:
            content = chunk["content"]
            assert indexer.count_tokens(content) <= content_limit
            assert content != "decoded"
            found = line.find(content, pos)
            assert found >= 0
            pos = found + len(content)
        assert pos == len(line)

    def test_no_duplicate_tail_chunks_before_long_line(
        self, in_memory_qdrant, mock_embedding_model
    ):
        """Drop overlap that cannot extend past the previous chunk.

        Eighty short lines fit in one chunk. The following ~240-token line
        fits alone but not with the 32-token overlap, which used to emit
        contained tails (75-80, 76-80, 77-80, ...).
        """
        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
            tokenizer=WordCountTokenizer(),
        )
        indexer.max_seq_length = 256
        indexer.chunk_size = 256
        indexer.chunk_overlap = 32

        small_lines = [f"s{i}" for i in range(80)]
        long_line = " ".join(f"w{i}" for i in range(240))
        content = "\n".join(small_lines + [long_line])
        chunks = indexer.chunk_code(content, "tails.py")

        _assert_no_contained_line_ranges(chunks)
        tails = [
            chunk
            for chunk in chunks
            if chunk["start_line"] > 1 and chunk["end_line"] <= 80
        ]
        assert tails == []
        long_chunks = [chunk for chunk in chunks if chunk["start_line"] == 81]
        assert len(long_chunks) == 1
        assert long_chunks[0]["end_line"] == 81
        assert long_chunks[0]["content"] == long_line
        assert indexer.count_tokens(long_chunks[0]["content"]) <= 254

    def test_no_duplicate_tails_when_next_line_is_split(
        self, in_memory_qdrant, mock_embedding_model
    ):
        """A following line that itself exceeds the window is not preceded by tails."""
        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
            tokenizer=WordCountTokenizer(),
        )
        indexer.max_seq_length = 256
        indexer.chunk_size = 256
        indexer.chunk_overlap = 32

        small_lines = [f"s{i}" for i in range(80)]
        long_line = " ".join(f"w{i}" for i in range(400))
        content = "\n".join(small_lines + [long_line])
        chunks = indexer.chunk_code(content, "split_tails.py")

        _assert_no_contained_line_ranges(chunks)
        tails = [
            chunk
            for chunk in chunks
            if chunk["start_line"] > 1 and chunk["end_line"] <= 80
        ]
        assert tails == []
        pieces = [chunk for chunk in chunks if chunk["start_line"] == 81]
        assert len(pieces) >= 2
        _assert_exact_advancing_slices(pieces, long_line)
        for piece in pieces:
            assert indexer.count_tokens(piece["content"]) <= 254

    def test_real_tokenizer_long_line_limit_and_original_text(
        self, in_memory_qdrant, mock_embedding_model
    ):
        """MiniLM chunks of a long line stay <= 256 tokens and keep source text."""
        tokenizer = _load_local_minilm_tokenizer()
        if tokenizer is None:
            pytest.skip("Local HuggingFace model cache not available")

        model = mock_embedding_model
        model.max_seq_length = 256
        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=model,
            tokenizer=tokenizer,
        )
        assert indexer.chunk_size == 256

        values = ",".join(f"Value_{i}" for i in range(3000))
        line = "[" + values + "] " + ("🙂" * 40) + " café " + ("字" * 20)
        chunks = indexer.chunk_code(line, "values.py")

        assert len(chunks) > 1
        _assert_exact_advancing_slices(chunks, line)
        _assert_no_contained_line_ranges(chunks)
        for chunk in chunks:
            content_tokens = len(
                tokenizer.encode(chunk["content"], add_special_tokens=False)
            )
            full_tokens = len(
                tokenizer.encode(chunk["content"], add_special_tokens=True)
            )
            assert content_tokens <= 254
            assert full_tokens <= 256
            assert chunk["content"] in line
        joined = "".join(chunk["content"] for chunk in chunks)
        assert "Value_0" in joined
        assert "🙂" in joined
        assert "é" in joined
        assert "字" in joined
        assert "value _ 0" not in joined
        assert joined.count("🙂") >= 40
        assert joined.count("字") >= 20

    def test_real_tokenizer_no_duplicate_tails(
        self, in_memory_qdrant, mock_embedding_model
    ):
        """With MiniLM, a ~240-token line after short lines does not repeat tails."""
        tokenizer = _load_local_minilm_tokenizer()
        if tokenizer is None:
            pytest.skip("Local HuggingFace model cache not available")

        mock_embedding_model.max_seq_length = 256
        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
            tokenizer=tokenizer,
        )
        words = []
        while len(tokenizer.encode(" ".join(words), add_special_tokens=False)) < 240:
            words.append(f"w{len(words)}")
        long_line = " ".join(words)
        small_lines = [f"s{i}" for i in range(80)]
        content = "\n".join(small_lines + [long_line])
        chunks = indexer.chunk_code(content, "real_tails.py")

        _assert_no_contained_line_ranges(chunks)
        covered = 0
        for chunk in chunks:
            if chunk["end_line"] <= len(small_lines):
                assert chunk["end_line"] > covered
                covered = chunk["end_line"]
        for chunk in chunks:
            full_tokens = len(
                tokenizer.encode(chunk["content"], add_special_tokens=True)
            )
            assert full_tokens <= 256

    def test_count_tokens_warns_on_word_count_fallback(
        self, in_memory_qdrant, mock_embedding_model, caplog
    ):
        """A tokenizer failure is logged; counts fall back to whitespace words."""

        class ExplodingTokenizer:
            def encode(self, text, add_special_tokens=False):
                raise RuntimeError("tokenizer exploded")

        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
            tokenizer=ExplodingTokenizer(),
        )
        with caplog.at_level(logging.WARNING, logger="index_code"):
            assert indexer.count_tokens("alpha beta gamma") == 3
            assert indexer.count_tokens("alpha beta gamma") == 3
        warnings = [
            record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        assert any(
            "falling back to whitespace word count" in message for message in warnings
        )
        assert any("tokenizer exploded" in message for message in warnings)
        assert len(warnings) == 1

    def test_init_does_not_fetch_tokenizer(
        self, in_memory_qdrant, mock_embedding_model, monkeypatch, caplog
    ):
        """Tokenizer download is not attempted while constructing an indexer."""
        import index_code as index_module

        def fail_fetch(*args, **kwargs):
            raise AssertionError(f"tokenizer fetched at startup: {args} {kwargs}")

        monkeypatch.setattr(index_module.AutoTokenizer, "from_pretrained", fail_fetch)
        CodeIndexer(
            config_path={
                "embedding": {"model": "sentence-transformers/all-MiniLM-L6-v2"}
            },
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )

        calls = {}

        def local_only_fetch(name, *args, **kwargs):
            calls["name"] = name
            calls["kwargs"] = kwargs
            raise OSError("cache miss")

        monkeypatch.setattr(
            index_module.AutoTokenizer, "from_pretrained", local_only_fetch
        )
        indexer = CodeIndexer(
            config_path={"embedding": {"model": "missing-model"}},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert calls == {}
        with caplog.at_level(logging.WARNING, logger="index_code"):
            assert indexer.count_tokens("alpha beta") == 2
        assert calls["name"] == "missing-model"
        assert calls["kwargs"].get("local_files_only") is True

    def test_indexer_source_has_no_mock_branches(self):
        """Mock-only branches stay in the tests, not in the indexer."""
        source = (INDEXER_DIR / "index_code.py").read_text(encoding="utf-8")
        assert "MagicMock" not in source


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
        assert indexer.chunk_size == 256
        assert indexer.chunk_overlap == 32

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


class TestCodeRetrieval:
    """Tests for code retrieval functionality (Issue #15)."""

    @pytest.fixture
    def indexed_qdrant(self, in_memory_qdrant, mock_config):
        """Prepare an in-memory Qdrant instance populated with sample indexed points."""
        from qdrant_client.models import VectorParams, Distance

        collection_name = mock_config["qdrant"]["collection_name"]
        in_memory_qdrant.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE),
        )

        vec1 = [0.1] * 384
        vec1[0] = 0.9

        vec2 = [0.1] * 384
        vec2[1] = 0.9

        vec3 = [0.1] * 384
        vec3[0] = 0.95

        in_memory_qdrant.upsert(
            collection_name=collection_name,
            points=[
                PointStruct(
                    id=generate_point_id("repo1", "src/auth.py", 0),
                    vector=vec1,
                    payload={
                        "repository": "repo1",
                        "file_path": "src/auth.py",
                        "content": "def authenticate(user, password):\n    return check_creds(user, password)",
                        "start_line": 1,
                        "end_line": 2,
                        "language": "py",
                        "content_hash": "hash1",
                        "indexed_at": "2026-10-17T00:00:00",
                    },
                ),
                PointStruct(
                    id=generate_point_id("repo1", "src/db.py", 0),
                    vector=vec2,
                    payload={
                        "repository": "repo1",
                        "file_path": "src/db.py",
                        "content": "def get_connection():\n    return pool.acquire()",
                        "start_line": 1,
                        "end_line": 2,
                        "language": "py",
                        "content_hash": "hash2",
                        "indexed_at": "2026-10-17T00:00:00",
                    },
                ),
                PointStruct(
                    id=generate_point_id("repo2", "auth/service.py", 0),
                    vector=vec3,
                    payload={
                        "repository": "repo2",
                        "file_path": "auth/service.py",
                        "content": "class AuthService:\n    def login(self, token): pass",
                        "start_line": 1,
                        "end_line": 2,
                        "language": "py",
                        "content_hash": "hash3",
                        "indexed_at": "2026-10-17T00:00:00",
                    },
                ),
            ],
        )
        return in_memory_qdrant

    def test_retrieve_code_basic(
        self, mock_config, indexed_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=indexed_qdrant,
            embedding_model=mock_embedding_model,
        )
        q_vec = [0.1] * 384
        q_vec[0] = 0.9
        mock_embedding_model.encode.return_value = MagicMock(tolist=lambda: q_vec)

        results = indexer.retrieve_code(
            "fix user authentication bug", repo_name="repo1", limit=5
        )
        assert len(results) >= 1
        top = results[0]
        assert top["repository"] == "repo1"
        assert top["file_path"] == "src/auth.py"
        assert "authenticate" in top["content"]
        assert top["start_line"] == 1
        assert top["end_line"] == 2
        assert top["language"] == "py"
        assert "score" in top
        assert top["score"] > 0.0

    def test_retrieve_code_filters_by_repository(
        self, mock_config, indexed_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=indexed_qdrant,
            embedding_model=mock_embedding_model,
        )
        q_vec = [0.1] * 384
        q_vec[0] = 0.9
        mock_embedding_model.encode.return_value = MagicMock(tolist=lambda: q_vec)

        results_repo1 = indexer.retrieve_code("auth", repo_name="repo1")
        assert all(r["repository"] == "repo1" for r in results_repo1)
        assert not any(r["repository"] == "repo2" for r in results_repo1)

        results_repo2 = indexer.retrieve_code("auth", repo_name="repo2")
        assert all(r["repository"] == "repo2" for r in results_repo2)
        assert not any(r["repository"] == "repo1" for r in results_repo2)

    def test_retrieve_code_all_repositories_when_repo_unspecified(
        self, mock_config, indexed_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=indexed_qdrant,
            embedding_model=mock_embedding_model,
        )
        results = indexer.retrieve_code("auth", repo_name=None, limit=10)
        repos = {r["repository"] for r in results}
        assert "repo1" in repos
        assert "repo2" in repos

    def test_retrieve_code_respects_limit(
        self, mock_config, indexed_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=indexed_qdrant,
            embedding_model=mock_embedding_model,
        )
        results = indexer.retrieve_code("auth", limit=1)
        assert len(results) == 1

    def test_retrieve_code_empty_and_whitespace_query(
        self, mock_config, indexed_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=indexed_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.retrieve_code("") == []
        assert indexer.retrieve_code("   \n\t  ") == []
        assert indexer.retrieve_code(None) == []  # type: ignore

    def test_retrieve_code_nonexistent_collection(
        self, mock_config, in_memory_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.retrieve_code("query") == []

    def test_retrieve_code_score_threshold(
        self, mock_config, indexed_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=indexed_qdrant,
            embedding_model=mock_embedding_model,
        )
        results = indexer.retrieve_code("auth", score_threshold=0.999999)
        for r in results:
            assert r["score"] >= 0.999999

    def test_retrieve_code_long_query_token_chunking(
        self, mock_config, indexed_qdrant, mock_embedding_model
    ):
        """Long query exceeding embedder window is chunked using chunk_code (#17 pair)."""
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=indexed_qdrant,
            embedding_model=mock_embedding_model,
        )
        indexer.max_seq_length = 10
        indexer.chunk_size = 10
        indexer.chunk_overlap = 2

        long_query = "word " * 30
        results = indexer.retrieve_code(long_query, limit=5)
        assert isinstance(results, list)

    def test_format_retrieved_context(self):
        chunks = [
            {
                "file_path": "ai-dev/main.py",
                "start_line": 10,
                "end_line": 20,
                "language": "py",
                "score": 0.912,
                "content": "def run():\n    pass",
            }
        ]
        context = CodeIndexer.format_retrieved_context(chunks)
        assert "## Relevant Code Context" in context
        assert "ai-dev/main.py" in context
        assert "lines 10-20" in context
        assert "def run():" in context
        assert "0.912" in context

        assert CodeIndexer.format_retrieved_context([]) == ""

    def test_search_and_query_aliases(
        self, mock_config, indexed_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=indexed_qdrant,
            embedding_model=mock_embedding_model,
        )
        with patch.object(
            indexer, "retrieve_code", return_value=[{"id": "1"}]
        ) as mock_ret:
            indexer.search("foo", repo_name="repo1", limit=3)
            mock_ret.assert_called_once_with(
                query="foo", repo_name="repo1", limit=3, score_threshold=None
            )

        with patch.object(
            indexer, "retrieve_code", return_value=[{"id": "1"}]
        ) as mock_ret:
            indexer.query("bar", repo_name="repo2", limit=4)
            mock_ret.assert_called_once_with(
                query="bar", repo_name="repo2", limit=4, score_threshold=None
            )

    def test_code_retriever_class(
        self, mock_config, indexed_qdrant, mock_embedding_model
    ):
        retriever = CodeRetriever(
            config_path=mock_config,
            qdrant_client=indexed_qdrant,
            embedding_model=mock_embedding_model,
        )
        chunks = retriever.retrieve("auth", repo_name="repo1")
        assert isinstance(chunks, list)

        formatted = retriever.retrieve_and_format("auth", repo_name="repo1")
        assert isinstance(formatted, str)

    def test_cli_main_retrieval(self, temp_workspace):
        out_file = temp_workspace / "context.md"
        with patch("index_code.CodeIndexer") as mock_cls:
            mock_inst = MagicMock()
            mock_inst.retrieve_code.return_value = [{"content": "hello"}]
            mock_inst.format_retrieved_context.return_value = "## Formatted"
            mock_cls.return_value = mock_inst

            rc = main(
                [
                    "--query",
                    "sample query",
                    "--repo",
                    "synthetic-repo",
                    "--output",
                    str(out_file),
                ]
            )
            assert rc == 0
            assert out_file.read_text(encoding="utf-8") == "## Formatted"
            mock_inst.retrieve_code.assert_called_once()

    def test_cli_skips_unscoped_retrieval(self, temp_workspace):
        out_file = temp_workspace / "context.md"
        out_file.write_text("stale context", encoding="utf-8")
        with patch("index_code.CodeIndexer") as mock_cls:
            assert main(["--query", "sample query", "--output", str(out_file)]) == 0
        mock_cls.assert_not_called()
        assert out_file.read_text(encoding="utf-8") == ""

    def test_retrieval_settings_keep_zero_values(
        self, in_memory_qdrant, mock_embedding_model
    ):
        """top_k 0 and score_threshold 0.0 must not be treated as missing."""
        indexer = CodeIndexer(
            config_path={
                "top_k": 0,
                "retrieval": {"top_k": 9, "score_threshold": 0.5},
                "score_threshold": 0.0,
            },
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.default_top_k == 0
        assert indexer.default_score_threshold == 0.0

    def test_nested_retrieval_defaults(self, in_memory_qdrant, mock_embedding_model):
        indexer = CodeIndexer(
            config_path={"retrieval": {"top_k": 3, "score_threshold": 0.0}},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.default_top_k == 3
        assert indexer.default_score_threshold == 0.0

    def test_query_points_falls_back_to_search(self, mock_config, mock_embedding_model):
        client = MagicMock()
        client.collection_exists.return_value = True
        client.query_points.side_effect = RuntimeError("query_points unavailable")
        point = MagicMock()
        point.id = "abc"
        point.score = 0.0
        point.payload = {
            "repository": "repo1",
            "file_path": "src/a.py",
            "content": "def zero():\n    return 0",
            "start_line": 1,
            "end_line": 2,
            "language": "py",
        }
        client.search.return_value = [point]
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=client,
            embedding_model=mock_embedding_model,
        )
        q_vec = [0.2] * 384
        mock_embedding_model.encode.return_value = MagicMock(tolist=lambda: q_vec)
        results = indexer.retrieve_code("zero score", limit=2)
        assert len(results) == 1
        assert results[0]["score"] == 0.0
        assert results[0]["file_path"] == "src/a.py"
        client.search.assert_called_once()


class TestInjectRetrievedContext:
    """Retrieved snippets must land inside the agent system prompt."""

    SAMPLE = """\
agent:
  system_template: |
    You are an autonomous software engineering agent.
    never force-push.
  templates:
    system_template: |
      You are an autonomous software engineering agent.
      never force-push.

# Environment configuration
environment:
  type: "docker"
"""

    def test_injects_into_every_system_template(self):
        context = (
            "## Relevant Code Context (retrieved from indexed codebase)\n\n"
            "### Snippet 1: `ai-dev/main.py` (lines 10-20)\n"
            "```py\n"
            "def run():\n"
            "    return 1\n"
            "```\n"
        )
        updated = inject_context_into_system_template(self.SAMPLE, context)
        loaded = yaml.safe_load(updated)
        for text in (
            loaded["agent"]["system_template"],
            loaded["agent"]["templates"]["system_template"],
        ):
            assert "def run():" in text
            assert "ai-dev/main.py" in text
            assert "never force-push." in text
        assert loaded["environment"]["type"] == "docker"

    def test_inject_is_idempotent(self):
        context = "def run():\n    return 1\n"
        once = inject_context_into_system_template(self.SAMPLE, context)
        twice = inject_context_into_system_template(once, context)
        assert once == twice

    def test_empty_context_is_noop(self):
        assert inject_context_into_system_template(self.SAMPLE, "  \n") == self.SAMPLE

    def test_missing_template_raises(self):
        with pytest.raises(ValueError):
            inject_context_into_system_template("agent:\n  max_steps: 1\n", "code")


class TestRepoSnippetLabelling:
    """Tests that snippets are labelled with their repository in retrieved context."""

    def test_snippets_are_labelled_with_repo(self):
        chunks = [
            {
                "repository": "alpha-repo",
                "file_path": "src/main.py",
                "start_line": 1,
                "end_line": 10,
                "language": "py",
                "score": 0.95,
                "content": "def main():\n    pass",
            },
            {
                "repository": "beta-repo",
                "file_path": "lib/utils.js",
                "start_line": 5,
                "end_line": 15,
                "language": "js",
                "score": 0.88,
                "content": "function util() {}",
            },
        ]
        context = CodeIndexer.format_retrieved_context(chunks)
        assert "[alpha-repo] `src/main.py`" in context
        assert "[beta-repo] `lib/utils.js`" in context

    def test_snippets_without_repo_omit_brackets(self):
        chunks = [
            {
                "file_path": "orphan.py",
                "start_line": 1,
                "end_line": 5,
                "language": "py",
                "content": "x = 1",
            }
        ]
        context = CodeIndexer.format_retrieved_context(chunks)
        assert "`orphan.py`" in context
        assert "[None]" not in context
        assert "[]" not in context


class TestTopKCappingAndBudgets:
    """Tests for capping top_k and enforcing token/byte budgets."""

    def test_default_max_context_bytes_constant(self):
        assert DEFAULT_MAX_CONTEXT_BYTES == 16384

    def test_top_k_is_capped_at_max(self, in_memory_qdrant, mock_embedding_model):
        indexer = CodeIndexer(
            config_path={
                "collection_name": "test_topk",
                "retrieval": {"top_k": 500},
            },
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.default_top_k == MAX_TOP_K

        # Also when limit is passed explicitly to retrieve_code
        with patch.object(
            indexer, "collection_exists", return_value=True
        ), patch.object(indexer, "_search_points", return_value=[]) as mock_search:
            indexer.retrieve_code("query", limit=100)
            mock_search.assert_called_once()
            # The second argument to _search_points is top_k, which must be <= MAX_TOP_K
            call_top_k = mock_search.call_args[0][1]
            assert call_top_k <= MAX_TOP_K

    def test_format_retrieved_context_enforces_byte_budget(self):
        # 10 chunks of 200 bytes each
        chunks = [
            {
                "repository": "repo",
                "file_path": f"file_{i}.py",
                "start_line": 1,
                "end_line": 10,
                "content": "a" * 200,
            }
            for i in range(10)
        ]
        # Low byte budget of 500 bytes
        formatted = CodeIndexer.format_retrieved_context(chunks, max_bytes=500)
        assert len(formatted.encode("utf-8")) <= 500
        # Should contain at least snippet 1, but not all 10
        assert "file_0.py" in formatted
        assert "file_9.py" not in formatted

    def test_format_retrieved_context_enforces_token_budget(self):
        chunks = [
            {
                "repository": "repo",
                "file_path": f"file_{i}.py",
                "start_line": 1,
                "end_line": 10,
                "content": f"word_{i} " * 50,
            }
            for i in range(10)
        ]
        formatted = CodeIndexer.format_retrieved_context(chunks, max_tokens=100)
        assert "file_0.py" in formatted
        assert "file_9.py" not in formatted

    def test_format_retrieved_context_caps_oversized_chunk(self):
        huge_chunk = [
            {
                "repository": "repo",
                "file_path": "huge.py",
                "start_line": 1,
                "end_line": 2000,
                "content": "word " * 1000,
            }
        ]
        formatted = CodeIndexer.format_retrieved_context(huge_chunk)
        assert "chunk truncated" in formatted

    def test_format_retrieved_context_enforces_budget_on_first_huge_snippet(self):
        # 1,000,000-char single minified line
        huge_chunk = [
            {
                "repository": "repo",
                "file_path": "minified.js",
                "start_line": 1,
                "end_line": 1,
                "content": "var x=" + ("a" * 1000000) + ";",
            }
        ]
        formatted = CodeIndexer.format_retrieved_context(huge_chunk)
        assert len(formatted.encode("utf-8")) <= DEFAULT_MAX_CONTEXT_BYTES
        assert "chunk truncated" in formatted
        assert formatted.endswith("```")

    def test_format_retrieved_context_enforces_budget_on_500_large_words(self):
        # 500 words of 2 KB each
        content = " ".join(["x" * 2000 for _ in range(500)])
        chunk = [
            {
                "repository": "repo",
                "file_path": "bigwords.txt",
                "start_line": 1,
                "end_line": 10,
                "content": content,
            }
        ]
        formatted = CodeIndexer.format_retrieved_context(chunk)
        assert len(formatted.encode("utf-8")) <= DEFAULT_MAX_CONTEXT_BYTES
        assert "chunk truncated" in formatted
        assert formatted.endswith("```")

    def test_format_retrieved_context_preserves_newlines_and_indentation(self):
        # 5,000-line snippet
        code_lines = ["def compute():\n"] + [
            f"    step_{i} = {i} * 2\n" for i in range(5000)
        ]
        content = "".join(code_lines)
        chunk = [
            {
                "repository": "repo",
                "file_path": "large_script.py",
                "start_line": 1,
                "end_line": 5001,
                "language": "python",
                "content": content,
            }
        ]
        formatted = CodeIndexer.format_retrieved_context(chunk)
        assert len(formatted.encode("utf-8")) <= DEFAULT_MAX_CONTEXT_BYTES
        assert formatted.endswith("```")
        # Ensure newlines and indentation were not flattened into space-delimited text
        lines = formatted.splitlines()
        assert len(lines) > 20
        assert any(line.startswith("    step_") for line in lines)

    def test_inject_context_enforces_byte_budget(self):
        sample = "agent:\n  system_template: |\n    Original prompt.\n"
        huge_context = "## Relevant Code\n" + ("line of retrieved code\n" * 500)
        updated = inject_context_into_system_template(
            sample, huge_context, max_bytes=300
        )
        loaded = yaml.safe_load(updated)
        template = loaded["agent"]["system_template"]
        assert "Context truncated to stay within budget" in template

    def test_enforce_context_budget_closes_open_code_fence(self):
        from inject_context import enforce_context_budget

        unclosed = (
            "## Relevant Code Context\n"
            "```python\n"
            "for item in items:\n"
            "    process(item)\n" * 100
        )
        capped = enforce_context_budget(unclosed, max_bytes=350)
        assert len(capped.encode("utf-8")) <= 350
        assert "<!-- Context truncated to stay within budget -->" in capped
        assert "```\n\n<!-- Context truncated to stay within budget -->" in capped


class TestSecretDenylistAndRedaction:
    """Tests ensuring secrets and sensitive paths never reach the prompt."""

    @pytest.mark.parametrize(
        "secret_path",
        [
            "config/secrets.yaml",
            "secrets.yaml",
            "config/credentials.json",
            "credentials.json",
            "deploy/env.sh",
            "env.sh",
            ".env",
            "deploy/.env",
            ".env.production",
            "keys",
            "keys.json",
            "api_keys.py",
            "server.key",
            "id_rsa",
            "id_rsa.pub",
            "id_ed25519",
            "terraform.tfvars",
            "prod.tfvars.json",
            "secrets/database.yaml",
            "credentials/google.json",
            ".npmrc",
            "kubeconfig",
            "KUBECONFIG",
            "k8s/kubeconfig.yaml",
            "kube/config",
            ".kube/config",
            ".netrc",
            ".pypirc",
            ".htpasswd",
            "htpasswd",
            ".docker/config.json",
            "service-account.json",
            "service-account-prod.json",
            "aws_credentials",
            "nested/KUBECONFIG",
            "nested/KUBE/config",
            "nested/.DOCKER/config.json",
            "nested/SERVICE-ACCOUNT.json",
            "nested/AWS_CREDENTIALS",
        ],
    )
    def test_is_secret_path_denylist(self, secret_path):
        assert is_secret_path(secret_path) is True

    @pytest.mark.parametrize(
        "safe_path",
        [
            "index_code.py",
            "src/main.py",
            "src/huge.py",
            "ai-dev/tests/test_code_indexer.py",
            "keyboard_handler.py",
            "monkeypatch.py",
            "src/monkey.py",
            "src/hotkeys.py",
            "monkey.py",
            "hotkeys.py",
        ],
    )
    def test_is_secret_path_safe_files(self, safe_path):
        assert is_secret_path(safe_path) is False

    def test_is_secret_content(self):
        assert is_secret_content("apiVersion: v1\nkind: Secret\n") is True
        assert is_secret_content("-----BEGIN RSA PRIVATE KEY-----\n") is True
        assert is_secret_content("-----BEGIN PRIVATE KEY-----\n") is True
        assert is_secret_content("-----END PRIVATE KEY-----\n") is True
        assert is_secret_content("-----END RSA PRIVATE KEY-----\n") is True
        assert is_secret_content("def run(): pass") is False

    def test_should_index_file_blocks_secret_paths(
        self, in_memory_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        assert indexer.should_index_file(Path("config/secrets.yaml")) is False
        assert indexer.should_index_file(Path("config/credentials.json")) is False
        assert indexer.should_index_file(Path("deploy/env.sh")) is False
        assert indexer.should_index_file(Path(".env")) is False
        assert indexer.should_index_file(Path("keys.json")) is False
        assert indexer.should_index_file(Path("main.py")) is True

    def test_retrieve_code_filters_secret_paths_and_manifests(
        self, in_memory_qdrant, mock_embedding_model
    ):
        indexer = CodeIndexer(
            config_path={},
            qdrant_client=in_memory_qdrant,
            embedding_model=mock_embedding_model,
        )
        fake_points = [
            Mock(
                id="1",
                score=0.99,
                payload={
                    "repository": "alpha",
                    "file_path": "config/secrets.yaml",
                    "content": "secret_key: supersecret",
                },
            ),
            Mock(
                id="2",
                score=0.98,
                payload={
                    "repository": "alpha",
                    "file_path": "k8s/secret.yaml",
                    "content": "apiVersion: v1\nkind: Secret\ndata:\n  token: c2VjcmV0\n",
                },
            ),
            Mock(
                id="3",
                score=0.95,
                payload={
                    "repository": "alpha",
                    "file_path": "src/app.py",
                    "content": "def app(): pass",
                },
            ),
        ]
        with patch.object(
            indexer, "collection_exists", return_value=True
        ), patch.object(indexer, "_search_points", return_value=fake_points):
            results = indexer.retrieve_code("app")
            assert len(results) == 1
            assert results[0]["file_path"] == "src/app.py"

    def test_redact_secrets_patterns(self):
        github_token = "ghp_" + ("x" * 36)
        aws_secret = "/".join(("wJalrXUtnFEMI", "K7MDENG", "bPxRfiCYEXAMPLEKEY"))
        aws_access_id = "AKIA" + "IOSFODNN7EXAMPLE"
        slack_token = "xoxb-" + "1234567890-1234567890123"
        text = (
            "token="
            + "SYNTHETIC_TOKEN_VALUE\n"
            + "GITHUB_TOKEN="
            + github_token
            + "\n"
            + "PUSHOVER_TOKEN="
            + "SYNTHETIC_PUSHOVER_VALUE\n"
            + "DB_PASSWORD="
            + "SYNTHETIC_DB_PASSWORD_VALUE\n"
            + "MY_SECRET="
            + "SYNTHETIC_SECRET_VALUE\n"
            + "APP_KEY="
            + "SYNTHETIC_APP_KEY_VALUE\n"
            + "api_token: "
            + "SYNTHETIC_API_TOKEN_VALUE\n"
            + '{"password": "'
            + "SYNTHETIC_JSON_PASSWORD_VALUE"
            + '", "api_token": "SYNTHETIC_JSON_TOKEN_VALUE"}\n'
            + "secret="
            + "SYNTHETIC_SECRET_VALUE\n"
            + "aws_secret_access_key = "
            + aws_secret
            + "\n"
            + "password="
            + "SYNTHETIC_SHORT_VALUE\n"
            + "Authorization: token "
            + "SYNTHETIC_AUTH_TOKEN_VALUE\n"
            + "Authorization: Bearer "
            + "SYNTHETIC_BEARER_VALUE\n"
            + aws_access_id
            + "\n"
            + slack_token
            + "\n"
            + "-----BEGIN PRIVATE KEY-----\nSYNTHETIC_KEY_BODY...\n-----END PRIVATE KEY-----\n"
        )
        redacted = redact_secrets(text)
        assert github_token not in redacted
        assert "SYNTHETIC_TOKEN_VALUE" not in redacted
        assert "SYNTHETIC_PUSHOVER_VALUE" not in redacted
        assert "SYNTHETIC_DB_PASSWORD_VALUE" not in redacted
        assert "SYNTHETIC_SECRET_VALUE" not in redacted
        assert "SYNTHETIC_APP_KEY_VALUE" not in redacted
        assert "SYNTHETIC_API_TOKEN_VALUE" not in redacted
        assert "SYNTHETIC_JSON_PASSWORD_VALUE" not in redacted
        assert "SYNTHETIC_JSON_TOKEN_VALUE" not in redacted
        assert aws_secret not in redacted
        assert "SYNTHETIC_SHORT_VALUE" not in redacted
        assert "SYNTHETIC_AUTH_TOKEN_VALUE" not in redacted
        assert "SYNTHETIC_BEARER_VALUE" not in redacted
        assert aws_access_id not in redacted
        assert slack_token not in redacted
        assert "SYNTHETIC_KEY_BODY" not in redacted
        assert "[REDACTED_SECRET]" in redacted
        assert "[REDACTED_AWS_KEY]" in redacted
        assert "[REDACTED_GITHUB_TOKEN]" in redacted
        assert "[REDACTED_PRIVATE_KEY]" in redacted
        assert "[REDACTED_TOKEN]" in redacted

    def test_redact_secrets_split_private_keys(self):
        # BEGIN without END in chunk (redact from BEGIN onward)
        chunk_begin = "def get_key():\n    # Key begins here\n    -----BEGIN PRIVATE KEY-----\nSYNTHETIC_KEY_BODY...\n"
        redacted_begin = redact_secrets(chunk_begin)
        assert "-----BEGIN PRIVATE KEY-----" not in redacted_begin
        assert "SYNTHETIC_KEY_BODY" not in redacted_begin
        assert "[REDACTED_PRIVATE_KEY]" in redacted_begin
        assert "def get_key():" in redacted_begin

        # END tail without BEGIN in chunk (redact from start up to END)
        chunk_end = "SYNTHETIC_KEY_BODY...\n-----END PRIVATE KEY-----\n    return key\n"
        redacted_end = redact_secrets(chunk_end)
        assert "-----END PRIVATE KEY-----" not in redacted_end
        assert "SYNTHETIC_KEY_BODY" not in redacted_end
        assert "[REDACTED_PRIVATE_KEY]" in redacted_end
        assert "return key" in redacted_end

    def test_redact_qa_key_value_forms(self):
        text = (
            "token=SYNTHETIC_TOKEN_VALUE\n"
            "GITHUB_TOKEN=SYNTHETIC_GITHUB_VALUE\n"
            "PUSHOVER_TOKEN=SYNTHETIC_PUSHOVER_VALUE\n"
            "DB_PASSWORD=SYNTHETIC_PASSWORD_VALUE\n"
            "api_token: SYNTHETIC_API_TOKEN_VALUE\n"
            '"password": "SYNTHETIC_JSON_PASSWORD_VALUE"\n'
            "secret=SYNTHETIC_SECRET_VALUE\n"
            "aws_secret_access_key = SYNTHETIC_AWS_SECRET_VALUE\n"
            "Authorization: token SYNTHETIC_AUTH_TOKEN_VALUE\n"
        )
        redacted = redact_secrets(text)
        for value in (
            "SYNTHETIC_TOKEN_VALUE",
            "SYNTHETIC_GITHUB_VALUE",
            "SYNTHETIC_PUSHOVER_VALUE",
            "SYNTHETIC_PASSWORD_VALUE",
            "SYNTHETIC_API_TOKEN_VALUE",
            "SYNTHETIC_JSON_PASSWORD_VALUE",
            "SYNTHETIC_SECRET_VALUE",
            "SYNTHETIC_AWS_SECRET_VALUE",
            "SYNTHETIC_AUTH_TOKEN_VALUE",
        ):
            assert value not in redacted

    def test_enforce_context_budget_truncates_large_single_line(self):
        from inject_context import enforce_context_budget

        huge_line = "````python\n" + ("x" * 100_000) + "\n````"
        capped = enforce_context_budget(huge_line, max_bytes=300, max_tokens=80)
        assert len(capped.encode("utf-8")) <= 300
        assert "\n" in capped
        assert "````\n\n<!-- Context truncated to stay within budget -->" in capped

    def test_format_retrieved_context_redacts_secrets(self):
        chunks = [
            {
                "repository": "alpha",
                "file_path": "src/auth.py",
                "start_line": 1,
                "end_line": 5,
                "content": "DB_PASSWORD = 'SYNTHETIC_DB_PASSWORD'\napi_key: 'SYNTHETIC_API_KEY'\n",
            }
        ]
        formatted = CodeIndexer.format_retrieved_context(chunks)
        assert "SYNTHETIC_DB_PASSWORD" not in formatted
        assert "SYNTHETIC_API_KEY" not in formatted
        assert "[REDACTED_SECRET]" in formatted


class TestNitsEmbedderAndFences:
    """Tests for lazy embedder load and triple-backtick fence handling."""

    def test_embedder_lazy_loaded_after_qdrant_check(self):
        # Point to an unreachable Qdrant
        indexer = CodeIndexer(
            config_path={"qdrant_url": "http://127.0.0.1:1"},
        )
        # SentenceTransformer must not have been loaded yet
        assert indexer._embedding_model is None
        # In query mode when Qdrant is down, retrieve_code must return [] without loading embedder
        results = indexer.retrieve_code("query")
        assert results == []
        assert indexer._embedding_model is None

    def test_triple_backtick_escaping_in_snippets(self):
        chunks = [
            {
                "repository": "repo",
                "file_path": "docs.py",
                "start_line": 1,
                "end_line": 5,
                "language": "py",
                "content": 'code = """\n```python\nprint("hello")\n```\n"""',
            }
        ]
        formatted = CodeIndexer.format_retrieved_context(chunks)
        # Must use fence of 4 or more backticks so inner triple backticks don't break the code block
        assert "````py\n" in formatted
        assert "\n````" in formatted
        assert 'print("hello")' in formatted


# Pytest configuration
def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "integration: marks tests as integration tests")


class RecordingTokenizer:
    """Fake tokenizer that records each encode call and its keyword arguments.

    With special tokens it adds [CLS]/[SEP] ids, like a real BERT tokenizer.
    """

    def __init__(self):
        self.calls = []

    def encode(self, text, add_special_tokens=True):
        self.calls.append({"text": text, "add_special_tokens": add_special_tokens})
        ids = [7] * len(str(text).split())
        if add_special_tokens:
            ids = [101] + ids + [102]
        return ids


class RecordingEncoder:
    """Fake embedding model that records the exact text passed to ``encode``."""

    max_seq_length = 256
    tokenizer = None

    def __init__(self, dim=384):
        self.dim = dim
        self.encoded = []

    def get_embedding_dimension(self):
        return self.dim

    def encode(self, text, *args, **kwargs):
        self.encoded.append(text)
        return MagicMock(tolist=lambda: [0.1] * self.dim)


class TestFakeTokenizerAndEncoder:
    """Pin tokenizer flags and encoded chunk text without a real model (Issue #32)."""

    def test_token_ids_do_not_request_special_tokens(
        self, mock_config, in_memory_qdrant
    ):
        tokenizer = RecordingTokenizer()
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=RecordingEncoder(),
            tokenizer=tokenizer,
        )

        ids = indexer._token_ids("alpha beta gamma")

        assert ids == [7, 7, 7]
        assert indexer.count_tokens("alpha beta gamma") == 3
        assert tokenizer.calls, "tokenizer.encode was not called"
        for call in tokenizer.calls:
            assert call["add_special_tokens"] is False

    def test_index_repository_encodes_exact_chunk_text(
        self, mock_config, in_memory_qdrant, temp_workspace
    ):
        encoder = RecordingEncoder()
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=encoder,
            tokenizer=WordCountTokenizer(),
        )
        repo_dir = temp_workspace / "exact-repo"
        repo_dir.mkdir()
        source = "def exact_text():\n    return 'abc123'\n"
        (repo_dir / "main.py").write_text(source)

        with patch.object(indexer, "clone_or_pull_repo", return_value=repo_dir):
            indexer.index_repository("https://github.com/org/exact.git", "exact")

        expected = [c["content"] for c in indexer.chunk_code(source, "main.py")]
        assert expected, "expected at least one chunk"
        assert encoder.encoded == expected
        assert all(isinstance(text, str) for text in encoder.encoded)
        assert "def exact_text():" in encoder.encoded[0]

        points, _ = in_memory_qdrant.scroll(
            collection_name=indexer.collection_name, with_payload=True
        )
        assert [p.payload["content"] for p in points] == expected


class TestEmbeddingDimensionApi:
    """Prefer get_embedding_dimension; fall back to the deprecated name."""

    def test_prefers_new_api(self, mock_config, in_memory_qdrant):
        model = RecordingEncoder(dim=512)
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=model,
        )
        assert indexer.embedding_dim == 512

    def test_falls_back_to_legacy_api(self, mock_config, in_memory_qdrant):
        class Legacy:
            tokenizer = None

            def get_sentence_embedding_dimension(self):
                return 768

        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=Legacy(),
        )
        assert indexer.embedding_dim == 768

    def test_new_api_does_not_call_deprecated_method(
        self, mock_config, in_memory_qdrant
    ):
        model = Mock(
            spec=["get_embedding_dimension", "get_sentence_embedding_dimension"]
        )
        model.get_embedding_dimension.return_value = 256
        model.get_sentence_embedding_dimension.side_effect = AssertionError(
            "deprecated API called"
        )
        indexer = CodeIndexer(
            config_path=mock_config,
            qdrant_client=in_memory_qdrant,
            embedding_model=model,
        )
        assert indexer.embedding_dim == 256
        model.get_sentence_embedding_dimension.assert_not_called()
