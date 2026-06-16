FROM python:3.12-slim AS base

WORKDIR /app

# System deps for faiss-cpu and numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch CPU first (large, cached layer)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create storage directories
RUN mkdir -p storage/uploads storage/faiss storage/sessions

# Default environment
ENV KAPI_ANALYTICS_PORT=18792
ENV KAPI_AUTH_MODE=local

EXPOSE 18792

CMD ["python", "main.py"]
