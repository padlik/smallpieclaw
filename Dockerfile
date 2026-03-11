# Lightweight Dockerfile optimized for Raspberry Pi
FROM python:3.11-slim-bullseye

# Metadata
LABEL maintainer="TM-Agent"
LABEL description="Lightweight Autonomous Telegram Agent for Raspberry Pi"
LABEL version="1.0"

# Set working directory
WORKDIR /app

# Install system dependencies (minimal)
RUN apt-get update && apt-get install -y \
    bash \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p tools/tools_generated \
    && chmod +x agent/tools/*.sh \
    && chmod +x tools/*.sh

# Create non-root user for security
RUN useradd -r -s /bin/false tmagent && \
    chown -R tmagent:tmagent /app

USER tmagent

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Expose no ports (bot operates via Telegram API)

# Default command
CMD ["python", "-m", "agent.telegram_bot"]

