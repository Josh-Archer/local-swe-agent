#!/usr/bin/env python3
"""
Code Indexer for RAG System
Clones repositories, chunks code files, generates embeddings, and uploads to Qdrant.
"""

import os
import re
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

from inject_context import inject_context_into_system_template, redact_secrets

MAX_TOP_K: int = 25
DEFAULT_MAX_CONTEXT_BYTES: int = 16384  # 16 KiB
DEFAULT_MAX_CONTEXT_TOKENS: int = 2048
DEFAULT_MAX_CHUNK_BYTES: int = 8192
DEFAULT_MAX_CHUNK_TOKENS: int = 512

SECRET_FILE_PATTERNS = [
    r"(^|/|\.)secrets?\.(yaml|yml|json|env|txt|sh)$",
    r"(^|/|\.)credentials?\.(yaml|yml|json|ini|env|txt|sh)$",
    r"(^|/)env\.sh$",
    r"(^|/)\.env(\.[a-zA-Z0-9_\-]+)?$",
    r"(^|/)[a-zA-Z0-9_\-]*\.env$",
    r"(^|/)keys?(\.(yaml|yml|json|txt|pem|key))?$",
    r"(^|/)(?:[a-zA-Z0-9_\-]*[_\-\.])?(key|keys)\.(py|yaml|yml|json|txt|pem|key|sh)$",
    r"(^|/)[a-zA-Z0-9_\-]*_key\.(pem|key|pub)$",
    r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$",
    r"\.tfvars(\.json)?$",
    r"\.(pem|p12|pfx|pkcs12|keystore|key)$",
    r"(^|/)(token|tokens)\.(yaml|yml|json|txt)$",
    r"(^|/)(passwd|password|passwords)(\.(txt|yaml|yml|json))?$",
    r"(^|/)\.npmrc$",
    r"(^|/|\.)kubeconfig.*$",
    r"(^|/)(\.kube|kube)/config(\.(yaml|yml|json))?$",
    r"(^|/)\.?netrc$",
    r"(^|/)\.?pypirc$",
    r"(^|/)\.?htpasswd.*$",
    r"(^|/)(\.docker|docker)/config\.json$",
    r"(^|/)service[-_]account.*\.json$",
    r"(^|/)aws_credentials(\..+)?$",
    r"(^|/)\.git-credentials$",
    r"(^|/)\.pgpass$",
    r"(^|/)\.dockercfg$",
    r"(^|/)[^/]*\.tfstate[^/]*$",
    r"(^|/)gcp-credentials\.json$",
    r"(^|/)serviceaccount\.json$",
    r"(^|/)\.s3cfg$",
    r"(^|/)\.vault-token$",
    r"(^|/)secrets\.toml$",
    r"(^|/)[^/]*\.ppk$",
]


def is_secret_path(file_path: Union[str, Path]) -> bool:
    """Return True if file_path matches known secret filename or credential patterns."""
    p = Path(file_path)
    posix_path = p.as_posix().lower()
    name = p.name.lower()

    for pattern in SECRET_FILE_PATTERNS:
        if re.search(pattern, posix_path, re.IGNORECASE) or re.search(
            pattern, name, re.IGNORECASE
        ):
            return True

    parts = [part.lower() for part in p.parts]
    if any(part in ("secrets", "credentials", ".ssh", ".gnupg") for part in parts):
        return True
    if any(part in (".kube", "kube") for part in parts) and (
        name == "config" or name.startswith("config.") or name.startswith("kubeconfig")
    ):
        return True
    if any(part in (".docker", "docker") for part in parts) and name == "config.json":
        return True
    if any(part in (".aws", "aws") for part in parts) and name in (
        "credentials",
        "config",
    ):
        return True

    if name in (
        ".npmrc",
        ".netrc",
        "netrc",
        ".pypirc",
        "pypirc",
        "aws_credentials",
        "kubeconfig",
    ):
        return True
    if name.startswith("kubeconfig"):
        return True
    if name.startswith(".htpasswd") or name.startswith("htpasswd"):
        return True
    if (
        name.startswith("service-account") or name.startswith("service_account")
    ) and name.endswith(".json"):
        return True

    if re.search(r"(\b|_)secret(\b|_)", name, re.IGNORECASE) and p.suffix.lower() in (
        ".yaml",
        ".yml",
        ".json",
        ".env",
        ".sh",
        ".txt",
        ".ini",
    ):
        return True
    if name in ("env.sh", ".env", "keys", "id_rsa") or name.startswith(".env"):
        return True

    return False


def is_secret_content(content: str) -> bool:
    """Return True if content matches known secret manifests (k8s Secret, private key, etc.)."""
    if not content:
        return False
    if re.search(r"^\s*kind:\s*Secret\b", content, re.MULTILINE):
        return True
    if re.search(
        r"-----(?:BEGIN|END)\s+(?:[A-Z0-9_]+\s+)*PRIVATE KEY(?:\s+BLOCK)?-----",
        content,
        re.IGNORECASE,
    ):
        return True
    return False


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


def _embedding_dimension(model: Any) -> Optional[int]:
    """Return the model's embedding dimension, or None when it is unknown.

    sentence-transformers 6.x renamed ``get_sentence_embedding_dimension`` to
    ``get_embedding_dimension`` and deprecated the old name. Prefer the new
    method and fall back to the old one for older releases.
    """
    for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        method = getattr(model, name, None)
        if not callable(method):
            continue
        try:
            dim = method()
        except Exception:
            continue
        if isinstance(dim, int) and not isinstance(dim, bool) and dim > 0:
            return dim
    return None


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

        # Initialize embedding model (lazy to avoid model loading before Qdrant check)
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
        self._embedding_model = embedding_model
        self.embedding_dim: int = int(qdrant_dict.get("vector_size", 384))
        self.max_seq_length: int = 256
        if embedding_model is not None:
            dim = _embedding_dimension(embedding_model)
            if dim is not None:
                self.embedding_dim = dim
            max_seq_len = getattr(embedding_model, "max_seq_length", None)
            if isinstance(max_seq_len, int) and max_seq_len > 0:
                self.max_seq_length = max_seq_len

        # Context window and tokenizer (Issue #17)
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
        elif self._embedding_model is not None:
            model_tokenizer = getattr(self._embedding_model, "tokenizer", None)
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
        self.default_top_k: int = min(int(top_k_val), MAX_TOP_K)
        max_top_k_cfg = self.config.get("max_top_k") or retrieval_dict.get("max_top_k")
        self.max_top_k: int = (
            min(int(max_top_k_cfg), MAX_TOP_K)
            if max_top_k_cfg is not None
            else MAX_TOP_K
        )
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

    @property
    def embedding_model(self) -> Any:
        """Lazily load SentenceTransformer so Qdrant can be checked before model load."""
        if self._embedding_model is None:
            logger.info(f"Loading embedding model: {self._embedding_model_name}")
            self._embedding_model = SentenceTransformer(self._embedding_model_name)
            dim = _embedding_dimension(self._embedding_model)
            if dim is not None:
                self.embedding_dim = dim
            max_seq_len = getattr(self._embedding_model, "max_seq_length", None)
            if isinstance(max_seq_len, int) and max_seq_len > 0:
                self.max_seq_length = max_seq_len
        return self._embedding_model

    @embedding_model.setter
    def embedding_model(self, model: Any) -> None:
        self._embedding_model = model
        if model is not None:
            dim = _embedding_dimension(model)
            if dim is not None:
                self.embedding_dim = dim
            max_seq_len = getattr(model, "max_seq_length", None)
            if isinstance(max_seq_len, int) and max_seq_len > 0:
                self.max_seq_length = max_seq_len

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
        if self._embedding_model is not None:
            model_tok = getattr(self._embedding_model, "tokenizer", None)
            if _usable_tokenizer(model_tok):
                self.tokenizer = model_tok
                return self.tokenizer
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

        # Skip secret files and credentials
        if is_secret_path(file_path):
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

                # Skip empty files or secret content
                if not content.strip() or is_secret_content(content):
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
        top_k = min(top_k, self.max_top_k)

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
                if is_secret_path(f_path):
                    continue

                content = payload.get("content", "")
                if is_secret_content(content):
                    continue

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
                        "content": content,
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
    def truncate_snippet_content(
        content: str,
        max_bytes: int,
        max_tokens: int,
        truncation_marker: str = "\n... [chunk truncated to limit]",
    ) -> Tuple[str, bool]:
        """Truncate code snippet to fit within max_bytes and max_tokens.

        Preserves newlines and indentation (does not flatten with .split()).
        """
        if not content:
            return "", False

        encoded = content.encode("utf-8")
        words = content.split()
        if len(encoded) <= max_bytes and len(words) <= max_tokens:
            return content, False

        marker_bytes = len(truncation_marker.encode("utf-8"))
        marker_words = len(truncation_marker.split())
        if max_bytes < marker_bytes or max_tokens < marker_words:
            return "", True
        target_bytes = max(0, max_bytes - marker_bytes)
        target_words = max(0, max_tokens - marker_words)

        lines = content.splitlines(keepends=True)
        kept_lines: List[str] = []
        curr_bytes = 0
        curr_words = 0

        for line in lines:
            line_bytes = len(line.encode("utf-8"))
            line_words = len(line.split())
            if (curr_bytes + line_bytes <= target_bytes) and (
                curr_words + line_words <= target_words
            ):
                kept_lines.append(line)
                curr_bytes += line_bytes
                curr_words += line_words
            else:
                if not kept_lines:
                    raw_line = line.encode("utf-8")[:target_bytes].decode(
                        "utf-8", errors="ignore"
                    )
                    w = raw_line.split()
                    if len(w) > target_words:
                        raw_line = " ".join(w[:target_words])
                    if raw_line:
                        kept_lines.append(raw_line)
                break

        result = "".join(kept_lines).rstrip("\r\n") + truncation_marker
        return result, True

    @staticmethod
    def format_retrieved_context(
        chunks: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> str:
        """Format retrieved code chunks into a markdown context block for the agent."""
        if not chunks:
            return ""

        if max_bytes is None:
            env_bytes = os.environ.get("RAG_MAX_BYTES")
            max_bytes = (
                int(env_bytes)
                if env_bytes and env_bytes.isdigit()
                else DEFAULT_MAX_CONTEXT_BYTES
            )
        if max_tokens is None:
            env_tokens = os.environ.get("RAG_MAX_TOKENS")
            max_tokens = (
                int(env_tokens)
                if env_tokens and env_tokens.isdigit()
                else DEFAULT_MAX_CONTEXT_TOKENS
            )

        header_lines = [
            "## Relevant Code Context (retrieved from indexed codebase)",
            "The following code snippets were retrieved from the repository to assist in solving the issue:",
            "",
        ]
        header_str = "\n".join(header_lines) + "\n"
        current_bytes = len(header_str.encode("utf-8"))
        current_words = len(header_str.split())

        snippet_blocks: List[str] = []
        snippet_idx = 1

        for chunk in chunks:
            file_path = chunk.get("file_path", "unknown")
            if is_secret_path(file_path):
                continue

            content = chunk.get("content", "")
            if is_secret_content(content):
                continue

            # Redact any secrets in snippet content before emission
            content = redact_secrets(content)

            repo = chunk.get("repository", "")
            repo_label = f"[{repo}] " if repo else ""
            start_line = chunk.get("start_line", 1)
            end_line = chunk.get("end_line", 1)
            score = chunk.get("score")
            lang = chunk.get("language", "")
            score_str = (
                f" (relevance: {score:.3f})" if isinstance(score, (int, float)) else ""
            )

            # Determine fence length: at least 3, and strictly longer than any run of backticks in content
            runs = re.findall(r"`+", content)
            fence_len = 3
            if runs:
                fence_len = max(3, max(len(r) for r in runs) + 1)
            fence = "`" * fence_len

            snippet_head = (
                f"### Snippet {snippet_idx}: {repo_label}`{file_path}` "
                f"(lines {start_line}-{end_line}){score_str}\n{fence}{lang}\n"
            )
            snippet_tail = f"\n{fence}\n\n"

            overhead_bytes = len(snippet_head.encode("utf-8")) + len(
                snippet_tail.encode("utf-8")
            )
            overhead_words = len(snippet_head.split()) + len(snippet_tail.split())

            if (current_bytes + overhead_bytes > max_bytes) or (
                current_words + overhead_words > max_tokens
            ):
                break

            rem_bytes = max_bytes - (current_bytes + overhead_bytes)
            rem_words = max_tokens - (current_words + overhead_words)

            allowed_bytes = min(rem_bytes, DEFAULT_MAX_CHUNK_BYTES)
            allowed_tokens = min(rem_words, DEFAULT_MAX_CHUNK_TOKENS)

            content, _ = CodeIndexer.truncate_snippet_content(
                content,
                max_bytes=allowed_bytes,
                max_tokens=allowed_tokens,
                truncation_marker="\n... [chunk truncated to limit]",
            )

            block_str = f"{snippet_head}{content}{snippet_tail}"
            block_bytes = len(block_str.encode("utf-8"))
            block_words = len(block_str.split())

            snippet_blocks.append(block_str)
            current_bytes += block_bytes
            current_words += block_words
            snippet_idx += 1

        if not snippet_blocks:
            return ""

        result = "\n".join(header_lines) + "\n" + "".join(snippet_blocks)
        return result.strip()

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
        help=f"Maximum number of code chunks to retrieve (default: config top_k or 5, capped at {MAX_TOP_K})",
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

    if parsed_args.query and not parsed_args.repo:
        logger.warning("Repository filter is required for retrieval; skipping query.")
        if parsed_args.output:
            Path(parsed_args.output).write_text("", encoding="utf-8")
        return 0

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

                safe_chunks = []

                def redact_json_value(value):
                    if isinstance(value, str):
                        return redact_secrets(value)
                    if isinstance(value, list):
                        return [redact_json_value(item) for item in value]
                    if isinstance(value, dict):
                        result = {}
                        for key, item in value.items():
                            safe_key = redact_secrets(str(key))
                            if isinstance(item, str):
                                marker_probe = redact_secrets(f"{key}={item}")
                                if "[REDACTED_" in marker_probe:
                                    safe_value = marker_probe.split("=", 1)[-1]
                                    safe_value = safe_value.strip("'\"` ")
                                else:
                                    safe_value = redact_secrets(item)
                            else:
                                safe_value = redact_json_value(item)
                            result[safe_key] = safe_value
                        return result
                    return value

                for chunk in chunks:
                    file_path = chunk.get("file_path", "")
                    content = chunk.get("content", "")
                    if is_secret_path(file_path) or is_secret_content(content):
                        continue
                    safe_chunks.append(redact_json_value(chunk))
                output_str = json.dumps(safe_chunks, indent=2)
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
