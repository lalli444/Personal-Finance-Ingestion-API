# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install build tools needed for some wheels (e.g. psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only requirements first – maximises layer cache hits
COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: production runtime ────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Install only the runtime PostgreSQL client library
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd --system appgroup \
    && useradd --system --gid appgroup --home /app --shell /sbin/nologin appuser

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application source
COPY app/ ./app/

# Ensure the app directory is owned by the non-root user
RUN chown -R appuser:appgroup /app

USER appuser

# Expose the application port
EXPOSE 8000

# Health check – mirrors the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Production entrypoint: 4 Uvicorn workers, bound to all interfaces
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--log-level", "info"]
