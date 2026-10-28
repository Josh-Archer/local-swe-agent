FROM python:3.11-slim

# Install system dependencies (versions float with base image; see hadolint DL3008 ignore)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Use extra-index-url to add PyTorch CPU repo while keeping PyPI for other packages
# Transitive pins clear certifi GHSA-6q48-6c7w-9gw2, filelock GHSA-v4x8-3wr5-3wq8,
# and httpx2/httpcore2 PYSEC-2026-3845/3844 (2.12+ also clears later highs).
RUN pip install --no-cache-dir \
    qdrant-client==1.11.3 \
    sentence-transformers==6.2.0 \
    gitpython==3.2.1 \
    pyyaml==6.0.2 \
    transformers==5.20.2 \
    torch==2.14.1 \
    certifi==2026.10.23 \
    filelock==4.2.4 \
    httpx2==2.16.0 \
    httpcore2==2.16.0 \
    --extra-index-url https://download.pytorch.org/whl/cpu

# Create working directory
WORKDIR /app

# Copy indexer script and the stdlib context injector
COPY index_code.py inject_context.py /app/
COPY config.yaml /app/

# Set permissions
RUN chmod +x /app/index_code.py

# Default command
ENTRYPOINT ["python3", "/app/index_code.py"]
