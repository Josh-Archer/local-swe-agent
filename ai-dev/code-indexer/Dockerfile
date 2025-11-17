FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Use extra-index-url to add PyTorch CPU repo while keeping PyPI for other packages
RUN pip install --no-cache-dir \
    qdrant-client==1.11.3 \
    sentence-transformers==3.2.1 \
    gitpython==3.1.43 \
    pyyaml==6.0.2 \
    transformers==4.46.2 \
    torch==2.5.1 \
    --extra-index-url https://download.pytorch.org/whl/cpu

# Create working directory
WORKDIR /app

# Copy indexer script
COPY index_code.py /app/
COPY config.yaml /app/

# Set permissions
RUN chmod +x /app/index_code.py

# Default command
ENTRYPOINT ["python3", "/app/index_code.py"]
