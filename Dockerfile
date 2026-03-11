FROM python:3.11-slim

# ── system tools available to tool scripts ────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    bc \
    curl \
    iproute2 \
    iputils-ping \
    net-tools \
    procps \
    # docker CLI (for docker_status.sh — talks to host socket)
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY *.py ./
COPY tools/ ./tools/

# ── Writable runtime dirs ─────────────────────────────────────────────────────
RUN mkdir -p tools_generated data && chmod 777 tools_generated data

# ── Non-root user for safety ──────────────────────────────────────────────────
RUN useradd -m -u 1000 agent && chown -R agent:agent /app
USER agent

CMD ["python", "main.py"]
