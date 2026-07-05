# Stage 1: Builder
FROM python:3.13-slim AS builder

# Install uv for dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy only dependency files first for better caching
COPY pyproject.toml uv.lock ./

# Install dependencies only (not the project itself) — includes the [server] extra
RUN uv sync --no-install-project --no-dev --no-editable --extra server

# Copy application code and install the project into the venv (non-editable copy,
# so the runtime stage needs only the venv — not the source tree)
COPY src/ ./src/
COPY README.md ./
RUN uv sync --no-dev --no-editable --extra server

# Stage 2: Production
FROM python:3.13-slim AS production

# Create non-root user for security
RUN groupadd --gid 1000 breeze \
    && useradd --uid 1000 --gid breeze --shell /bin/false --create-home breezeai

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Pre-create writable log dir for the non-root user
RUN mkdir -p /logs && chown -R 1000:1000 /logs && chmod -R 775 /logs

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH" \
    BREEZEAI_COG_LOG_LOCATION=/logs \
    BREEZEAI_COG_PORT=3000

# Switch to non-root user
USER breezeai

# Expose port
EXPOSE 3000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:3000/health', timeout=5.0)" || exit 1

# Run the HTTP service
CMD ["breezeai-cog", "serve", "--host", "0.0.0.0", "--port", "3000"]
