FROM python:3.11-slim

# Install system dependencies (versions float with base image; see hadolint DL3008 ignore)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Use extra-index-url to add PyTorch CPU repo while keeping PyPI for other packages
RUN pip install --no-cache-dir \
    qdrant-client==1.11.3 \
    sentence-transformers==6.2.0 \
    gitpython==3.2.1 \
    pyyaml==6.0.2 \
    transformers==5.20.2 \
    torch==2.14.1 \
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
