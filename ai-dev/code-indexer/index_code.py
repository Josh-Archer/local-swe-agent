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
from typing import List, Dict, Any
from datetime import datetime
import hashlib

import git
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from sentence_transformers import SentenceTransformer

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class CodeIndexer:
    """Indexes code repositories into Qdrant vector database."""

    def __init__(self, config_path: str = "/app/config.yaml"):
        """Initialize the code indexer."""
        logger.info("Initializing Code Indexer...")

        # Load configuration
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        # Initialize Qdrant client
        qdrant_url = os.getenv(
            "QDRANT_URL", self.config.get("qdrant_url", "http://qdrant:6333")
        )
        self.qdrant = QdrantClient(url=qdrant_url)
        logger.info(f"Connected to Qdrant at {qdrant_url}")

        # Initialize embedding model
        model_name = self.config.get(
            "embedding_model", "sentence-transformers/all-MiniLM-L6-v2"
        )
        logger.info(f"Loading embedding model: {model_name}")
        self.embedding_model = SentenceTransformer(model_name)
        self.embedding_dim = self.embedding_model.get_sentence_embedding_dimension()

        # Collection name
        self.collection_name = self.config.get("collection_name", "code_embeddings")

        # Code file extensions to index
        self.code_extensions = set(
            self.config.get(
                "code_extensions",
                [
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
                ],
            )
        )

        # Chunking parameters
        self.chunk_size = self.config.get("chunk_size", 500)
        self.chunk_overlap = self.config.get("chunk_overlap", 50)

        # Work directory
        # nosec B108 - /tmp is appropriate in containerized environment
        self.work_dir = Path(self.config.get("work_dir", "/tmp/indexer"))  # nosec
        self.work_dir.mkdir(parents=True, exist_ok=True)

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

    def chunk_code(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk code into smaller pieces with overlap."""
        lines = content.split("\n")
        chunks = []

        # Simple line-based chunking
        chunk_lines = self.chunk_size
        overlap_lines = self.chunk_overlap

        for i in range(0, len(lines), chunk_lines - overlap_lines):
            chunk_content = "\n".join(lines[i : i + chunk_lines])
            if chunk_content.strip():
                chunks.append(
                    {
                        "content": chunk_content,
                        "start_line": i + 1,
                        "end_line": min(i + chunk_lines, len(lines)),
                    }
                )

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
        point_id = 0

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

                # Chunk the file
                chunks = self.chunk_code(content, str(rel_path))

                for chunk in chunks:
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
                            "file_path": str(rel_path),
                            "language": file_path.suffix[1:],  # Remove leading dot
                            "content": chunk["content"],
                            "start_line": chunk["start_line"],
                            "end_line": chunk["end_line"],
                            "indexed_at": datetime.utcnow().isoformat(),
                            "content_hash": content_hash,
                        },
                    )
                    points.append(point)
                    point_id += 1

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

        logger.info(f"Completed indexing {repo_name}: {point_id} chunks indexed")

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
