#!/usr/bin/env python3
"""
Code Indexer for RAG System
Clones repositories, chunks code files, generates embeddings, and uploads to Qdrant.
"""

import os
import sys
import logging
import yaml
from pathlib import Path
from typing import List, Dict, Any, Set, Union, Optional, cast, Tuple
from datetime import datetime
import hashlib
import uuid

import git
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    PointIdsList,
)
from sentence_transformers import SentenceTransformer

try:
    from transformers import AutoTokenizer, logging as hf_logging

    hf_logging.set_verbosity_error()
except ImportError:
    AutoTokenizer = None  # type: ignore
    hf_logging = None  # type: ignore

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _usable_tokenizer(tokenizer: Any) -> bool:
    """Return True when `tokenizer` exposes an encode callable."""
    if tokenizer is None:
        return False
    encode = getattr(tokenizer, "encode", None)
    return callable(encode)


def _coerce_id_list(encoded: Any) -> Optional[List[Any]]:
    """Return token ids when `encoded` is a sequence of ids, else None."""
    if isinstance(encoded, tuple):
        encoded = list(encoded)
    if not isinstance(encoded, list):
        return None
    if encoded and isinstance(encoded[0], (list, tuple)):
        encoded = list(encoded[0])
    if not encoded:
        return []
    try:
        int(encoded[0])
    except (TypeError, ValueError):
        return None
    return encoded


def _coerce_offset_spans(offsets: Any) -> Optional[List[Tuple[int, int]]]:
    """Return character spans when `offsets` is a list of (start, end) pairs."""
    if not isinstance(offsets, (list, tuple)):
        return None
    spans: List[Tuple[int, int]] = []
    for pair in offsets:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        try:
            start = int(pair[0])
            end = int(pair[1])
        except (TypeError, ValueError):
            return None
        if isinstance(pair[0], bool) or isinstance(pair[1], bool):
            return None
        if start < 0 or end < 0:
            return None
        spans.append((start, end))
    return spans


def _whitespace_spans(text: str) -> List[Tuple[int, int]]:
    """Spans of non-whitespace runs, measured on the original string."""
    spans: List[Tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        end = index + 1
        while end < length and not text[end].isspace():
            end += 1
        spans.append((index, end))
        index = end
    return spans


def generate_point_id(repo_name: str, file_path: str, chunk_index: int = 0) -> str:
    """Generate a deterministic point ID (UUID5) for a chunk within a repository file."""
    normalized_path = file_path.replace("\\", "/")
    key = f"{repo_name}:{normalized_path}:{chunk_index}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


class CodeIndexer:
    """Indexes code repositories into Qdrant vector database."""

    def __init__(
        self,
        config_path: Union[str, Path, Dict[str, Any]] = "/app/config.yaml",
        qdrant_client: Optional[Any] = None,
        embedding_model: Optional[Any] = None,
        tokenizer: Optional[Any] = None,
    ):
        """Initialize the code indexer."""
        logger.info("Initializing Code Indexer...")

        # Load configuration
        if isinstance(config_path, dict):
            self.config: Dict[str, Any] = config_path
        else:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                self.config = loaded if isinstance(loaded, dict) else {}

        # Collection name
        qdrant_cfg = self.config.get("qdrant")
        qdrant_dict: Dict[str, Any] = qdrant_cfg if isinstance(qdrant_cfg, dict) else {}
        self.collection_name: str = (
            self.config.get("collection_name")
            or qdrant_dict.get("collection_name")
            or "code_embeddings"
        )

        # Initialize Qdrant client
        if qdrant_client is not None:
            self.qdrant = qdrant_client
        else:
            qdrant_url = os.getenv(
                "QDRANT_URL",
                self.config.get("qdrant_url")
                or qdrant_dict.get("url")
                or "http://qdrant:6333",
            )
            if qdrant_url == ":memory:":
                self.qdrant = QdrantClient(":memory:")
            else:
                self.qdrant = QdrantClient(url=qdrant_url)
            logger.info(f"Connected to Qdrant at {qdrant_url}")

        # Initialize embedding model
        embedding_cfg = self.config.get("embedding")
        embedding_dict: Dict[str, Any] = (
            embedding_cfg if isinstance(embedding_cfg, dict) else {}
        )
        model_name: str = (
            self.config.get("embedding_model")
            or embedding_dict.get("model")
            or "sentence-transformers/all-MiniLM-L6-v2"
        )
        self._embedding_model_name = model_name
        if embedding_model is not None:
            self.embedding_model = embedding_model
        else:
            logger.info(f"Loading embedding model: {model_name}")
            self.embedding_model = SentenceTransformer(model_name)

        if hasattr(self.embedding_model, "get_sentence_embedding_dimension"):
            self.embedding_dim: int = (
                self.embedding_model.get_sentence_embedding_dimension()
            )
        else:
            self.embedding_dim = int(qdrant_dict.get("vector_size", 384))

        # Context window and tokenizer (Issue #17)
        max_seq_len = getattr(self.embedding_model, "max_seq_length", None)
        if isinstance(max_seq_len, int) and max_seq_len > 0:
            self.max_seq_length: int = max_seq_len
        else:
            self.max_seq_length = 256

        # Prefer an explicit tokenizer or the one bundled with the embedding
        # model. Do not download a tokenizer here: a missing tokenizer is
        # resolved lazily from the local cache on first use.
        self._tokenizer_resolved = False
        self._tokenizer_failure: Optional[str] = None
        self._token_fallback_warned = False
        self._offset_fallback_warned = False
        self.tokenizer = None
        if tokenizer is not None:
            self.tokenizer = tokenizer
            self._tokenizer_resolved = True
        else:
            model_tokenizer = getattr(self.embedding_model, "tokenizer", None)
            if _usable_tokenizer(model_tokenizer):
                self.tokenizer = model_tokenizer
                self._tokenizer_resolved = True

        # Chunking parameters (tokens)
        indexing_cfg = self.config.get("indexing")
        indexing_dict: Dict[str, Any] = (
            indexing_cfg if isinstance(indexing_cfg, dict) else {}
        )
        chunk_size_val = (
            self.config.get("chunk_size")
            or indexing_dict.get("chunk_size")
            or self.max_seq_length
        )
        configured_chunk_size = int(chunk_size_val)
        if configured_chunk_size > self.max_seq_length:
            logger.info(
                f"Configured chunk_size ({configured_chunk_size}) exceeds embedding model context window "
                f"({self.max_seq_length}); capping to {self.max_seq_length}"
            )
            self.chunk_size: int = self.max_seq_length
        else:
            self.chunk_size = configured_chunk_size

        chunk_overlap_val = (
            self.config.get("chunk_overlap") or indexing_dict.get("chunk_overlap") or 32
        )
        self.chunk_overlap: int = min(
            int(chunk_overlap_val), max(0, self.chunk_size - 1)
        )

        # Code file extensions to index
        code_exts = self.config.get("code_extensions") or indexing_dict.get(
            "file_extensions"
        )
        if code_exts:
            self.code_extensions: Set[str] = set(code_exts)
        else:
            self.code_extensions = {
                ".py",
                ".js",
                ".ts",
                ".tsx",
                ".jsx",
                ".java",
                ".go",
                ".rs",
                ".cpp",
                ".c",
                ".h",
                ".hpp",
                ".cs",
                ".rb",
                ".php",
                ".swift",
                ".kt",
                ".scala",
                ".sh",
                ".bash",
                ".yaml",
                ".yml",
                ".json",
                ".md",
                ".sql",
                ".html",
                ".css",
                ".vue",
                ".dockerfile",
            }

        # Work directory
        # nosec B108
        self.work_dir = Path(self.config.get("work_dir", "/tmp/indexer"))  # nosec
        self.work_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def generate_point_id(repo_name: str, file_path: str, chunk_index: int = 0) -> str:
        """Generate a deterministic point ID for a chunk within a repository file."""
        return generate_point_id(repo_name, file_path, chunk_index)

    def ensure_collection(self):
        """Create Qdrant collection if it doesn't exist."""
        collections = self.qdrant.get_collections().collections
        collection_names = [c.name for c in collections]

        if self.collection_name not in collection_names:
            logger.info(f"Creating collection: {self.collection_name}")
            self.qdrant.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=self.embedding_dim, distance=Distance.COSINE
                ),
            )
        else:
            logger.info(f"Collection {self.collection_name} already exists")

    def get_repository_point_ids(self, repo_name: str) -> Set[str]:
        """Retrieve all existing point IDs for a specific repository."""
        scroll_filter = Filter(
            must=[
                FieldCondition(
                    key="repository",
                    match=MatchValue(value=repo_name),
                )
            ]
        )
        existing_ids: Set[str] = set()
        offset = None

        try:
            while True:
                scroll_result = self.qdrant.scroll(
                    collection_name=self.collection_name,
                    scroll_filter=scroll_filter,
                    limit=100,
                    offset=offset,
                    with_payload=False,
                    with_vectors=False,
                )
                if not scroll_result:
                    break
                records, next_page = scroll_result
                if records:
                    for record in records:
                        rec_id = (
                            record.id if hasattr(record, "id") else record.get("id")
                        )
                        if rec_id is not None:
                            existing_ids.add(rec_id)

                if next_page is None or not records:
                    break
                offset = next_page
        except Exception as e:
            logger.warning(f"Failed to scroll points for repository {repo_name}: {e}")

        return existing_ids

    def delete_stale_points(self, repo_name: str, current_point_ids: Set[str]) -> int:
        """Delete points for a repository that are no longer present."""
        existing_ids = self.get_repository_point_ids(repo_name)
        stale_ids = existing_ids - current_point_ids

        if not stale_ids:
            logger.info(f"No stale points found for repository {repo_name}")
            return 0

        logger.info(f"Found {len(stale_ids)} stale points to delete for {repo_name}")
        stale_id_list = list(stale_ids)
        batch_size = 100

        for i in range(0, len(stale_id_list), batch_size):
            batch = stale_id_list[i : i + batch_size]
            points_selector = PointIdsList(
                points=cast(List[Union[int, str, uuid.UUID]], batch)
            )
            try:
                self.qdrant.delete(
                    collection_name=self.collection_name,
                    points_selector=points_selector,
                )
            except Exception as e:
                logger.warning(
                    f"Failed to delete batch of stale points for {repo_name}: {e}"
                )

        logger.info(
            f"Completed stale point cleanup for {repo_name}: deleted {len(stale_ids)} points"
        )
        return len(stale_ids)

    def clone_or_pull_repo(self, repo_url: str, repo_name: str) -> Path:
        """Clone repository or pull if it already exists."""
        repo_path = self.work_dir / repo_name

        try:
            if repo_path.exists():
                logger.info(f"Pulling updates for {repo_name}...")
                repo = git.Repo(repo_path)
                origin = repo.remotes.origin
                origin.pull()
            else:
                logger.info(f"Cloning {repo_name} from {repo_url}...")
                git.Repo.clone_from(repo_url, repo_path)

            return repo_path
        except Exception as e:
            logger.error(f"Failed to clone/pull {repo_name}: {e}")
            raise

    def _warn_token_fallback(self, reason: str) -> None:
        """Log once when token counts fall back to a whitespace word count."""
        if self._token_fallback_warned:
            return
        self._token_fallback_warned = True
        logger.warning(
            "Tokenizer unavailable (%s); falling back to whitespace word count. "
            "Word counts can be much lower than the embedding model's token counts "
            "and may let chunks exceed the context window.",
            reason,
        )

    def _get_tokenizer(self) -> Any:
        """Return the tokenizer, loading it from the local cache on first use."""
        if self._tokenizer_resolved:
            return self.tokenizer
        self._tokenizer_resolved = True
        if AutoTokenizer is None:
            self._tokenizer_failure = "transformers is not installed"
            self.tokenizer = None
            return None
        try:
            # local_files_only: never reach the network from indexing.
            self.tokenizer = AutoTokenizer.from_pretrained(
                self._embedding_model_name,
                local_files_only=True,
            )
            logger.info(
                "Loaded tokenizer for %s from the local cache",
                self._embedding_model_name,
            )
        except Exception as exc:
            self.tokenizer = None
            self._tokenizer_failure = (
                f"could not load a cached tokenizer for {self._embedding_model_name}: "
                f"{exc}"
            )
        return self.tokenizer

    def _token_ids(self, text: str) -> Optional[List[Any]]:
        """Encode `text` to content token ids, or None when that is not possible."""
        tokenizer = self._get_tokenizer()
        if tokenizer is None:
            self._warn_token_fallback(
                self._tokenizer_failure or "no tokenizer is configured"
            )
            return None
        try:
            encoded: Any = None
            if hasattr(tokenizer, "encode") and callable(tokenizer.encode):
                try:
                    encoded = tokenizer.encode(text, add_special_tokens=False)
                except TypeError:
                    encoded = tokenizer.encode(text)
            elif callable(tokenizer):
                called = tokenizer(text, add_special_tokens=False)
                if isinstance(called, dict) or hasattr(called, "get"):
                    encoded = called.get("input_ids")
            ids = _coerce_id_list(encoded)
        except Exception as exc:
            self._warn_token_fallback(f"tokenizer error: {exc}")
            return None
        if ids is None:
            self._warn_token_fallback("tokenizer returned no token ids")
            return None
        return ids

    def count_tokens(self, text: str) -> int:
        """Count the number of content tokens in the given text for the embedder."""
        if not text:
            return 0
        ids = self._token_ids(text)
        if ids is not None:
            return len(ids)
        return max(1, len(text.split()))

    def _offset_spans(self, text: str) -> Optional[List[Tuple[int, int]]]:
        """Character spans of tokens in `text`, or None when offsets are unavailable.

        Spans index the original string. Callers slice with them instead of
        decoding token ids, which would lowercase, strip accents, and drop
        characters the vocabulary maps to unknown.
        """
        tokenizer = self._get_tokenizer()
        if tokenizer is None or not callable(tokenizer):
            return None
        try:
            encoded = tokenizer(
                text,
                add_special_tokens=False,
                return_offsets_mapping=True,
            )
        except Exception as exc:
            if not self._offset_fallback_warned:
                self._offset_fallback_warned = True
                logger.warning(
                    "Tokenizer offset mapping unavailable (%s); "
                    "splitting long lines on original whitespace spans",
                    exc,
                )
            return None
        offsets = None
        if isinstance(encoded, dict) or hasattr(encoded, "get"):
            try:
                offsets = encoded.get("offset_mapping")
            except Exception:
                offsets = None
        spans = _coerce_offset_spans(offsets)
        if not spans:
            if not self._offset_fallback_warned:
                self._offset_fallback_warned = True
                logger.warning(
                    "Tokenizer did not return offset mappings; "
                    "splitting long lines on original whitespace spans"
                )
            return None
        return spans

    def _fit_prefix(self, text: str, max_tokens: int) -> str:
        """Longest prefix of `text` whose content token count is <= max_tokens."""
        if not text:
            return ""
        if max_tokens < 1:
            max_tokens = 1
        if self.count_tokens(text) <= max_tokens:
            return text
        best = ""
        low = 1
        high = len(text)
        while low <= high:
            mid = (low + high) // 2
            candidate = text[:mid]
            if candidate.strip() and self.count_tokens(candidate) <= max_tokens:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        if best:
            return best
        return text[:1]

    def _pieces_from_spans(
        self,
        text: str,
        spans: List[Tuple[int, int]],
        max_tokens: int,
        overlap: int,
    ) -> List[str]:
        """Slice `text` into exact substrings that each re-encode within max_tokens.

        A window that cannot extend past the previous piece drops its overlap
        instead of emitting a suffix that is already fully covered.
        """
        if max_tokens < 1:
            max_tokens = 1
        overlap = min(max(0, overlap), max_tokens - 1)
        spans = list(spans)
        pieces: List[str] = []
        total = len(spans)
        cursor = 0
        previous_end = 0
        steps = 0
        # Each step consumes at least one character or one span.
        step_limit = len(text) + total + 5
        length = len(text)

        while cursor < total and steps < step_limit:
            steps += 1
            span_start, span_end = spans[cursor]
            if span_end <= span_start:
                cursor += 1
                continue

            window_end = min(cursor + max_tokens, total)
            while (
                window_end > cursor
                and spans[window_end - 1][1] <= spans[window_end - 1][0]
            ):
                window_end -= 1

            chosen_end: Optional[int] = None
            piece = ""
            end = window_end
            while end > cursor:
                char_start = min(max(spans[cursor][0], 0), length)
                char_end = min(max(spans[end - 1][1], 0), length)
                if char_end <= char_start:
                    end -= 1
                    continue
                candidate = text[char_start:char_end]
                if candidate.strip() and self.count_tokens(candidate) <= max_tokens:
                    chosen_end = end
                    piece = candidate
                    break
                end -= 1

            if chosen_end is None:
                # Still inside the previous piece: drop the overlap and try
                # the first uncovered span on its own.
                if cursor < previous_end:
                    cursor = previous_end
                    continue
                char_start = min(max(span_start, 0), length)
                char_end = min(max(span_end, 0), length)
                raw = text[char_start:char_end]
                piece = self._fit_prefix(raw, max_tokens)
                if not piece or not piece.strip():
                    cursor += 1
                    continue
                pieces.append(piece)
                consumed = len(piece)
                if char_start + consumed < char_end:
                    spans[cursor] = (char_start + consumed, char_end)
                else:
                    cursor += 1
                    previous_end = max(previous_end, cursor)
                continue

            if chosen_end <= previous_end:
                # Overlap tail cannot take in new tokens. Resume after the
                # previous piece so the tail is not emitted again.
                if cursor < previous_end:
                    cursor = previous_end
                    continue
                break

            pieces.append(piece)
            previous_end = chosen_end
            next_cursor = chosen_end - overlap
            if next_cursor <= cursor:
                next_cursor = cursor + 1
            cursor = next_cursor

        if cursor < total:
            logger.warning(
                "Stopped splitting a long line after %s steps; %s token spans were not covered",
                steps,
                total - cursor,
            )
        return pieces

    def _split_long_line(
        self, line: str, line_no: int, max_tokens: int, overlap: int
    ) -> List[Dict[str, Any]]:
        """Split one over-long line into substrings that fit in max_tokens."""
        spans = self._offset_spans(line)
        if not spans:
            spans = _whitespace_spans(line)
        pieces = (
            self._pieces_from_spans(line, spans, max_tokens, overlap) if spans else []
        )
        chunks: List[Dict[str, Any]] = []
        for piece in pieces:
            if piece.strip():
                chunks.append(
                    {
                        "content": piece,
                        "start_line": line_no,
                        "end_line": line_no,
                    }
                )
        return chunks

    def chunk_code(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk code into smaller pieces that fit the embedding model's context window."""
        if not content.strip():
            return []

        lines = content.split("\n")
        n_lines = len(lines)
        chunks: List[Dict[str, Any]] = []

        # Reserve 2 tokens for special tokens ([CLS], [SEP]) when the window allows.
        max_tokens = self.chunk_size
        if self.max_seq_length > 2:
            max_tokens = min(self.chunk_size, self.max_seq_length - 2)
        if max_tokens < 1:
            max_tokens = 1
        overlap = min(self.chunk_overlap, max(0, max_tokens - 1))

        start_idx = 0
        last_emitted_end = -1
        steps = 0
        while start_idx < n_lines:
            steps += 1
            if steps > n_lines + 2:
                logger.warning(
                    "Stopped chunking %s early at line %s to avoid a non-progress loop",
                    file_path,
                    start_idx + 1,
                )
                break

            single_line_tokens = self.count_tokens(lines[start_idx])
            if single_line_tokens > max_tokens:
                sub_chunks = self._split_long_line(
                    lines[start_idx], start_idx + 1, max_tokens, overlap
                )
                chunks.extend(sub_chunks)
                last_emitted_end = start_idx
                start_idx += 1
                continue

            end_idx = start_idx
            best_end = start_idx
            while end_idx + 1 < n_lines:
                candidate = "\n".join(lines[start_idx : end_idx + 2])
                if self.count_tokens(candidate) <= max_tokens:
                    best_end = end_idx + 1
                    end_idx += 1
                    continue
                break

            # Overlap left a window that cannot pass the previous chunk.
            if best_end <= last_emitted_end:
                next_start = last_emitted_end + 1
                if next_start <= start_idx:
                    next_start = start_idx + 1
                start_idx = next_start
                continue

            chunk_content = "\n".join(lines[start_idx : best_end + 1])
            if chunk_content.strip():
                chunks.append(
                    {
                        "content": chunk_content,
                        "start_line": start_idx + 1,
                        "end_line": best_end + 1,
                    }
                )
            last_emitted_end = best_end

            if best_end >= n_lines - 1:
                break

            next_start = best_end + 1
            if overlap > 0:
                overlap_start = best_end + 1
                for back_idx in range(best_end, start_idx, -1):
                    overlap_candidate = "\n".join(lines[back_idx : best_end + 1])
                    if self.count_tokens(overlap_candidate) <= overlap:
                        overlap_start = back_idx
                    else:
                        break
                # Keep the overlap only when the next chunk can include the
                # following line. Otherwise the overlap is re-emitted as a
                # series of contained tails (lines 75-80, 76-80, 77-80, ...).
                if overlap_start <= best_end and best_end + 1 < n_lines:
                    extended = "\n".join(lines[overlap_start : best_end + 2])
                    next_line_tokens = self.count_tokens(lines[best_end + 1])
                    if (
                        next_line_tokens <= max_tokens
                        and self.count_tokens(extended) <= max_tokens
                    ):
                        next_start = overlap_start

            if next_start <= start_idx:
                next_start = best_end + 1
            start_idx = next_start

        return chunks

    def should_index_file(self, file_path: Path) -> bool:
        """Check if file should be indexed."""
        # Check extension
        if file_path.suffix.lower() not in self.code_extensions:
            return False

        # Skip hidden files and directories
        if any(part.startswith(".") for part in file_path.parts):
            return False

        # Skip common non-code directories
        skip_dirs = {
            "node_modules",
            "venv",
            "env",
            "__pycache__",
            "dist",
            "build",
            ".git",
        }
        if any(d in file_path.parts for d in skip_dirs):
            return False

        return True

    def index_repository(self, repo_url: str, repo_name: str):
        """Index all code files in a repository."""
        logger.info(f"Starting indexing for repository: {repo_name}")

        # Ensure collection exists before indexing
        self.ensure_collection()

        # Clone or pull repository
        repo_path = self.clone_or_pull_repo(repo_url, repo_name)

        # Find all code files
        code_files = []
        for file_path in repo_path.rglob("*"):
            if file_path.is_file() and self.should_index_file(file_path):
                code_files.append(file_path)

        logger.info(f"Found {len(code_files)} code files to index")

        # Process files and create embeddings
        points = []
        current_point_ids: Set[str] = set()

        for file_path in code_files:
            try:
                # Read file content
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()

                # Skip empty files
                if not content.strip():
                    continue

                # Get relative path from repo root
                rel_path = file_path.relative_to(repo_path)
                file_path_str = rel_path.as_posix()

                # Chunk the file
                chunks = self.chunk_code(content, file_path_str)

                for chunk_idx, chunk in enumerate(chunks):
                    # Generate deterministic point ID per repo, path, and chunk
                    point_id = self.generate_point_id(
                        repo_name, file_path_str, chunk_idx
                    )
                    current_point_ids.add(point_id)

                    # Generate embedding
                    embedding = self.embedding_model.encode(chunk["content"]).tolist()

                    # Create unique ID based on content hash
                    content_hash = hashlib.sha256(
                        chunk["content"].encode()
                    ).hexdigest()[:16]

                    # Create point
                    point = PointStruct(
                        id=point_id,
                        vector=embedding,
                        payload={
                            "repository": repo_name,
                            "file_path": file_path_str,
                            "language": (
                                file_path.suffix[1:]
                                if file_path.suffix.startswith(".")
                                else file_path.suffix
                            ),
                            "content": chunk["content"],
                            "start_line": chunk["start_line"],
                            "end_line": chunk["end_line"],
                            "indexed_at": datetime.utcnow().isoformat(),
                            "content_hash": content_hash,
                        },
                    )
                    points.append(point)

                    # Upload in batches
                    if len(points) >= 100:
                        logger.info(f"Uploading batch of {len(points)} embeddings...")
                        self.qdrant.upsert(
                            collection_name=self.collection_name, points=points
                        )
                        points = []

            except Exception as e:
                logger.warning(f"Failed to index {file_path}: {e}")
                continue

        # Upload remaining points
        if points:
            logger.info(f"Uploading final batch of {len(points)} embeddings...")
            self.qdrant.upsert(collection_name=self.collection_name, points=points)

        # Delete stale points for this repository
        self.delete_stale_points(repo_name, current_point_ids)

        logger.info(
            f"Completed indexing {repo_name}: {len(current_point_ids)} chunks indexed"
        )

    def run(self):
        """Run the indexer on all configured repositories."""
        logger.info("Starting code indexing process...")

        # Ensure collection exists
        self.ensure_collection()

        # Get repositories from config
        repositories = self.config.get("repositories", [])
        if not repositories:
            logger.warning("No repositories configured for indexing")
            return

        # Index each repository
        for repo_config in repositories:
            repo_url = repo_config.get("url")
            repo_name = repo_config.get("name")

            if not repo_url or not repo_name:
                logger.warning(f"Invalid repository config: {repo_config}")
                continue

            try:
                self.index_repository(repo_url, repo_name)
            except Exception as e:
                logger.error(f"Failed to index repository {repo_name}: {e}")
                continue

        logger.info("Code indexing completed!")

        # Print stats
        collection_info = self.qdrant.get_collection(self.collection_name)
        logger.info(f"Total vectors in collection: {collection_info.points_count}")


def main():
    """Main entry point."""
    try:
        indexer = CodeIndexer()
        indexer.run()
        sys.exit(0)
    except Exception as e:
        logger.error(f"Indexer failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
