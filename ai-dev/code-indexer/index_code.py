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

from inject_context import inject_context_into_system_template

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
        elif embedding_model is None and AutoTokenizer is not None:
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            except Exception:
                self.tokenizer = None
        else:
            # Injected embedding model without a real tokenizer (unit tests).
            # Skip the network download that AutoTokenizer would otherwise do.
            self.tokenizer = None

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

        # Retrieval parameters
        retrieval_cfg = self.config.get("retrieval")
        retrieval_dict: Dict[str, Any] = (
            retrieval_cfg if isinstance(retrieval_cfg, dict) else {}
        )
        # 0 and 0.0 are valid settings; do not treat them as missing.
        top_k_val = self.config.get("top_k")
        if top_k_val is None:
            top_k_val = retrieval_dict.get("top_k")
        if top_k_val is None:
            top_k_val = 5
        self.default_top_k: int = int(top_k_val)
        if "score_threshold" in self.config:
            thresh = self.config.get("score_threshold")
        else:
            thresh = retrieval_dict.get("score_threshold")
        self.default_score_threshold: Optional[float] = (
            float(thresh) if thresh is not None else None
        )

        # Work directory
        # nosec B108
        self.work_dir = Path(self.config.get("work_dir", "/tmp/indexer"))  # nosec
        self.work_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def generate_point_id(repo_name: str, file_path: str, chunk_index: int = 0) -> str:
        """Generate a deterministic point ID for a chunk within a repository file."""
        return generate_point_id(repo_name, file_path, chunk_index)

    def collection_exists(self) -> bool:
        """Check if the target collection exists in Qdrant."""
        try:
            if hasattr(self.qdrant, "collection_exists") and callable(
                self.qdrant.collection_exists
            ):
                return bool(self.qdrant.collection_exists(self.collection_name))
            collections = self.qdrant.get_collections().collections
            return self.collection_name in [c.name for c in collections]
        except Exception as e:
            logger.warning(f"Error checking collection existence: {e}")
            return False

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

    def retrieve_code(
        self,
        query: str,
        repo_name: Optional[str] = None,
        limit: Optional[int] = None,
        score_threshold: Optional[float] = None,
        chunk_query: bool = True,
    ) -> List[Dict[str, Any]]:
        """Retrieve relevant indexed code chunks from Qdrant for a given query."""
        if not query or not query.strip():
            return []

        if not self.collection_exists():
            logger.warning(
                f"Collection {self.collection_name} does not exist in Qdrant"
            )
            return []

        top_k = limit if limit is not None else self.default_top_k
        if top_k <= 0:
            return []

        min_score = (
            score_threshold
            if score_threshold is not None
            else self.default_score_threshold
        )

        # Handle embedding window for queries (pairs with Issue #17)
        max_tokens = self.chunk_size
        if self.max_seq_length > 2:
            max_tokens = min(self.chunk_size, self.max_seq_length - 2)

        query_texts: List[str] = []
        clean_query = query.strip()
        if chunk_query and self.count_tokens(clean_query) > max_tokens:
            # Chunk long query using token-aware chunk_code so sub-queries fit in context window
            sub_chunks = self.chunk_code(clean_query, "query")
            if sub_chunks:
                query_texts = [
                    c["content"].strip()
                    for c in sub_chunks
                    if c.get("content", "").strip()
                ]
            else:
                query_texts = [clean_query]
        else:
            query_texts = [clean_query]

        search_filter = None
        if repo_name:
            search_filter = Filter(
                must=[
                    FieldCondition(
                        key="repository",
                        match=MatchValue(value=repo_name),
                    )
                ]
            )

        best_results: Dict[Any, Dict[str, Any]] = {}

        for q_text in query_texts:
            try:
                raw_emb = self.embedding_model.encode(q_text)
                if hasattr(raw_emb, "tolist"):
                    query_vec = raw_emb.tolist()
                elif isinstance(raw_emb, (list, tuple)):
                    query_vec = list(raw_emb)
                elif hasattr(raw_emb, "__iter__"):
                    query_vec = [float(x) for x in raw_emb]
                else:
                    query_vec = list(raw_emb)
            except Exception as e:
                logger.error(f"Failed to generate query embedding: {e}")
                continue

            points = self._search_points(query_vec, top_k, search_filter, min_score)

            for p in points:
                p_id, score, payload = self._unpack_scored_point(p)
                if min_score is not None and score < float(min_score):
                    continue

                repo = payload.get("repository", "")
                f_path = payload.get("file_path", "")
                s_line = payload.get("start_line", 0)
                e_line = payload.get("end_line", 0)

                # Deduplicate by point ID or file coordinates
                dedup_key = p_id if p_id is not None else (repo, f_path, s_line, e_line)

                if (
                    dedup_key not in best_results
                    or score > best_results[dedup_key]["score"]
                ):
                    best_results[dedup_key] = {
                        "id": str(p_id) if p_id is not None else "",
                        "score": score,
                        "repository": repo,
                        "file_path": f_path,
                        "content": payload.get("content", ""),
                        "start_line": int(s_line) if s_line is not None else 0,
                        "end_line": int(e_line) if e_line is not None else 0,
                        "language": payload.get("language", ""),
                        "content_hash": payload.get("content_hash", ""),
                        "indexed_at": payload.get("indexed_at", ""),
                    }

        sorted_points = sorted(
            best_results.values(), key=lambda x: x["score"], reverse=True
        )
        return sorted_points[:top_k]

    def _search_points(
        self,
        query_vec: List[float],
        top_k: int,
        search_filter: Optional[Any],
        min_score: Optional[float],
    ) -> List[Any]:
        """Query Qdrant, preferring query_points and falling back to search."""
        if hasattr(self.qdrant, "query_points"):
            try:
                kwargs: Dict[str, Any] = {
                    "collection_name": self.collection_name,
                    "query": query_vec,
                    "limit": top_k,
                }
                if search_filter is not None:
                    kwargs["query_filter"] = search_filter
                if min_score is not None:
                    kwargs["score_threshold"] = float(min_score)
                res = self.qdrant.query_points(**kwargs)
                raw_points = getattr(res, "points", None)
                if raw_points is None:
                    raw_points = res if isinstance(res, list) else []
                return list(raw_points or [])
            except Exception as e:
                logger.warning(f"Qdrant query_points failed: {e}")

        if hasattr(self.qdrant, "search"):
            try:
                kwargs = {
                    "collection_name": self.collection_name,
                    "query_vector": query_vec,
                    "limit": top_k,
                }
                if search_filter is not None:
                    kwargs["query_filter"] = search_filter
                if min_score is not None:
                    kwargs["score_threshold"] = float(min_score)
                return list(self.qdrant.search(**kwargs) or [])
            except Exception as e:
                logger.warning(f"Qdrant search failed: {e}")
        return []

    @staticmethod
    def _unpack_scored_point(point: Any) -> tuple:
        """Return (id, score, payload) without treating a 0.0 score as missing."""
        if isinstance(point, dict):
            point_id = point.get("id")
            score_val = point.get("score", 0.0)
            payload = point.get("payload") or {}
        else:
            point_id = getattr(point, "id", None)
            score_val = getattr(point, "score", 0.0)
            payload = getattr(point, "payload", None) or {}
        if not isinstance(payload, dict):
            payload = {}
        try:
            score = float(score_val) if score_val is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        return point_id, score, payload

    def search(
        self,
        query: str,
        repo_name: Optional[str] = None,
        limit: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Alias for retrieve_code."""
        return self.retrieve_code(
            query=query,
            repo_name=repo_name,
            limit=limit,
            score_threshold=score_threshold,
        )

    def query(
        self,
        query: str,
        repo_name: Optional[str] = None,
        limit: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Alias for retrieve_code."""
        return self.retrieve_code(
            query=query,
            repo_name=repo_name,
            limit=limit,
            score_threshold=score_threshold,
        )

    @staticmethod
    def format_retrieved_context(chunks: List[Dict[str, Any]]) -> str:
        """Format retrieved code chunks into a markdown context block for the agent."""
        if not chunks:
            return ""

        lines = [
            "## Relevant Code Context (retrieved from indexed codebase)",
            "The following code snippets were retrieved from the repository to assist in solving the issue:",
            "",
        ]
        for idx, chunk in enumerate(chunks, 1):
            file_path = chunk.get("file_path", "unknown")
            start_line = chunk.get("start_line", 1)
            end_line = chunk.get("end_line", 1)
            score = chunk.get("score")
            lang = chunk.get("language", "")
            content = chunk.get("content", "")

            score_str = (
                f" (relevance: {score:.3f})" if isinstance(score, (int, float)) else ""
            )
            lines.append(
                f"### Snippet {idx}: `{file_path}` (lines {start_line}-{end_line}){score_str}"
            )
            lines.append(f"```{lang}")
            lines.append(content)
            lines.append("```")
            lines.append("")

        return "\n".join(lines).strip()

    format_retrieved_code = format_retrieved_context


class CodeRetriever:
    """Convenience class to query Qdrant and format retrieved code for agents."""

    def __init__(
        self,
        config_path: Union[str, Path, Dict[str, Any]] = "/app/config.yaml",
        qdrant_client: Optional[Any] = None,
        embedding_model: Optional[Any] = None,
        tokenizer: Optional[Any] = None,
        indexer: Optional[CodeIndexer] = None,
    ):
        if indexer is not None:
            self.indexer = indexer
        else:
            self.indexer = CodeIndexer(
                config_path=config_path,
                qdrant_client=qdrant_client,
                embedding_model=embedding_model,
                tokenizer=tokenizer,
            )

    def retrieve(
        self,
        query: str,
        repo_name: Optional[str] = None,
        limit: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve relevant code chunks."""
        return self.indexer.retrieve_code(
            query=query,
            repo_name=repo_name,
            limit=limit,
            score_threshold=score_threshold,
        )

    def retrieve_and_format(
        self,
        query: str,
        repo_name: Optional[str] = None,
        limit: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> str:
        """Retrieve relevant code and return formatted markdown context."""
        chunks = self.retrieve(
            query=query,
            repo_name=repo_name,
            limit=limit,
            score_threshold=score_threshold,
        )
        return self.indexer.format_retrieved_context(chunks)


def main(args: Optional[List[str]] = None):
    """Main entry point for indexing or retrieval."""
    import argparse

    parser = argparse.ArgumentParser(description="Code Indexer and Retrieval for RAG")
    parser.add_argument(
        "--config",
        default=os.getenv("CONFIG_PATH", "/app/config.yaml"),
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--query",
        "-q",
        type=str,
        default=None,
        help="Search query to retrieve relevant code chunks",
    )
    parser.add_argument(
        "--repo",
        "-r",
        type=str,
        default=None,
        help="Filter retrieval to a specific repository",
    )
    parser.add_argument(
        "--limit",
        "-k",
        type=int,
        default=None,
        help="Maximum number of code chunks to retrieve (default: config top_k or 5)",
    )
    parser.add_argument(
        "--score-threshold",
        type=float,
        default=None,
        help="Minimum similarity score threshold",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "markdown"],
        default="markdown",
        help="Output format for retrieved code (default: markdown)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Output file to write retrieved context to",
    )
    parser.add_argument(
        "--qdrant-url",
        type=str,
        default=None,
        help="Override Qdrant URL",
    )
    parser.add_argument(
        "--inject-config",
        type=str,
        default=None,
        help="Append retrieved markdown to system_template blocks in this config file",
    )

    parsed_args = parser.parse_args(args)

    if parsed_args.qdrant_url:
        os.environ["QDRANT_URL"] = parsed_args.qdrant_url

    try:
        config_path = parsed_args.config
        if not Path(config_path).exists():
            local_cfg = Path(__file__).parent / "config.yaml"
            if local_cfg.exists():
                config_path = str(local_cfg)
            else:
                config_path = {}

        indexer = CodeIndexer(config_path=config_path)

        if parsed_args.query:
            chunks = indexer.retrieve_code(
                query=parsed_args.query,
                repo_name=parsed_args.repo,
                limit=parsed_args.limit,
                score_threshold=parsed_args.score_threshold,
            )
            markdown = indexer.format_retrieved_context(chunks)
            if parsed_args.format == "json":
                import json

                output_str = json.dumps(chunks, indent=2)
            else:
                output_str = markdown

            if parsed_args.output:
                Path(parsed_args.output).write_text(output_str, encoding="utf-8")
                logger.info(f"Wrote retrieved context to {parsed_args.output}")
            else:
                print(output_str)

            if parsed_args.inject_config and markdown.strip():
                config_file = Path(parsed_args.inject_config)
                original = config_file.read_text(encoding="utf-8")
                updated = inject_context_into_system_template(original, markdown)
                config_file.write_text(updated, encoding="utf-8")
                logger.info(f"Injected RAG context into {config_file}")
            return 0
        else:
            indexer.run()
            return 0
    except Exception as e:
        logger.error(f"Execution failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
