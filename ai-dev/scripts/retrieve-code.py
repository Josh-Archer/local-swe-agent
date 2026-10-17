#!/usr/bin/env python3
"""
Retrieve relevant indexed code from Qdrant and format as context for the agent.
Used prior to SWE-agent runs to inject RAG context into the agent's prompt.
"""

import sys
from pathlib import Path

# Add code-indexer to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
AI_DEV_DIR = SCRIPT_DIR.parent

for candidate_dir in [
    AI_DEV_DIR / "code-indexer",
    Path("/app"),
    Path("/config"),
    SCRIPT_DIR,
]:
    if candidate_dir.exists() and str(candidate_dir) not in sys.path:
        sys.path.insert(0, str(candidate_dir))

try:
    from index_code import CodeIndexer, CodeRetriever, main

    __all__ = ["CodeIndexer", "CodeRetriever", "main"]
except ImportError:
    CodeIndexer = None  # type: ignore
    CodeRetriever = None  # type: ignore
    main = None  # type: ignore


def run():
    if main is None:
        sys.stderr.write("ERROR: index_code module could not be found or imported.\n")
        return 1
    result = main(sys.argv[1:])
    return 0 if result is None else int(result)


if __name__ == "__main__":
    sys.exit(run())
