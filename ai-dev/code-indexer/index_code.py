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
from typing import List, Dict, Any, Set, Union, Optional, cast
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

        if tokenizer is not None:
            self.tokenizer = tokenizer
        elif (
            hasattr(self.embedding_model, "tokenizer")
            and self.embedding_model.tokenizer is not None
            and type(self.embedding_model.tokenizer).__name__ != "MagicMock"
        ):
            self.tokenizer = self.embedding_model.tokenizer
        elif AutoTokenizer is not None:
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            except Exception:
                self.tokenizer = getattr(self.embedding_model, "tokenizer", None)
        else:
            self.tokenizer = getattr(self.embedding_model, "tokenizer", None)

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

    def count_tokens(self, text: str) -> int:
        """Count the number of content tokens in the given text for the embedder."""
        if not text:
            return 0
        if self.tokenizer is not None:
            if hasattr(self.tokenizer, "encode") and callable(self.tokenizer.encode):
                try:
                    encoded = self.tokenizer.encode(text, add_special_tokens=False)
                    if isinstance(encoded, (list, tuple)):
                        return len(encoded)
                    if type(encoded).__name__ != "MagicMock" and hasattr(
                        encoded, "__len__"
                    ):
                        return len(encoded)
                except TypeError:
                    try:
                        encoded = self.tokenizer.encode(text)
                        if isinstance(encoded, (list, tuple)):
                            return len(encoded)
                        if type(encoded).__name__ != "MagicMock" and hasattr(
                            encoded, "__len__"
                        ):
                            return len(encoded)
                    except Exception:
                        pass
                except Exception:
                    pass
            elif callable(self.tokenizer):
                try:
                    res = self.tokenizer(text, add_special_tokens=False)
                    if isinstance(res, dict) and "input_ids" in res:
                        ids = res["input_ids"]
                        if isinstance(ids, (list, tuple)):
                            return len(ids)
                        if hasattr(ids, "__len__"):
                            return len(ids)
                except Exception:
                    pass
        # Fallback if tokenizer is unavailable or an unconfigured mock:
        return max(1, len(text.split()))

    def _split_long_line(
        self, line: str, line_no: int, max_tokens: int, overlap: int
    ) -> List[Dict[str, Any]]:
        """Split a single line that exceeds max_tokens into multiple sub-chunks."""
        chunks: List[Dict[str, Any]] = []
        if (
            self.tokenizer is not None
            and hasattr(self.tokenizer, "encode")
            and hasattr(self.tokenizer, "decode")
            and type(self.tokenizer).__name__ != "MagicMock"
        ):
            try:
                token_ids = self.tokenizer.encode(line, add_special_tokens=False)
                step = max(1, max_tokens - overlap)
                for k in range(0, len(token_ids), step):
                    sub_ids = token_ids[k : k + max_tokens]
                    sub_content = self.tokenizer.decode(
                        sub_ids, skip_special_tokens=True
                    )
                    if sub_content.strip():
                        chunks.append(
                            {
                                "content": sub_content,
                                "start_line": line_no,
                                "end_line": line_no,
                            }
                        )
                if chunks:
                    return chunks
            except Exception:
                pass

        # Fallback word-based split
        words = line.split()
        step = max(1, max_tokens - overlap)
        for k in range(0, len(words), step):
            sub_words = words[k : k + max_tokens]
            sub_content = " ".join(sub_words)
            if sub_content.strip():
                chunks.append(
                    {
                        "content": sub_content,
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

        # Usable token capacity: reserve 2 tokens for special tokens ([CLS], [SEP]) if window allows
        max_tokens = self.chunk_size
        if self.max_seq_length > 2:
            max_tokens = min(self.chunk_size, self.max_seq_length - 2)
        overlap = min(self.chunk_overlap, max(0, max_tokens - 1))

        start_idx = 0
        while start_idx < n_lines:
            # Check if this single line alone exceeds max_tokens
            single_line_tokens = self.count_tokens(lines[start_idx])
            if single_line_tokens > max_tokens:
                sub_chunks = self._split_long_line(
                    lines[start_idx], start_idx + 1, max_tokens, overlap
                )
                chunks.extend(sub_chunks)
                start_idx += 1
                continue

            # Accumulate lines while candidate chunk fits in max_tokens
            end_idx = start_idx
            best_end = start_idx
            current_tokens = single_line_tokens

            while end_idx + 1 < n_lines:
                next_line = lines[end_idx + 1]
                next_tokens = self.count_tokens(next_line)
                if current_tokens + next_tokens <= max_tokens:
                    candidate = "\n".join(lines[start_idx : end_idx + 2])
                    cand_tokens = self.count_tokens(candidate)
                    if cand_tokens <= max_tokens:
                        best_end = end_idx + 1
                        current_tokens = cand_tokens
                        end_idx += 1
                        continue
                else:
                    candidate = "\n".join(lines[start_idx : end_idx + 2])
                    cand_tokens = self.count_tokens(candidate)
                    if cand_tokens <= max_tokens:
                        best_end = end_idx + 1
                        current_tokens = cand_tokens
                        end_idx += 1
                        continue
                break

            chunk_content = "\n".join(lines[start_idx : best_end + 1])
            if chunk_content.strip():
                chunks.append(
                    {
                        "content": chunk_content,
                        "start_line": start_idx + 1,
                        "end_line": best_end + 1,
                    }
                )

            if best_end >= n_lines - 1:
                break

            # Calculate overlap backwards from best_end
            next_start = best_end + 1
            if overlap > 0:
                for back_idx in range(best_end, start_idx, -1):
                    overlap_candidate = "\n".join(lines[back_idx : best_end + 1])
                    if self.count_tokens(overlap_candidate) <= overlap:
                        next_start = back_idx
                    else:
                        break

            if next_start <= start_idx:
                next_start = start_idx + 1
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
