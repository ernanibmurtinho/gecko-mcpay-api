# =============================================================================
# Multi-stage build — gecko-api (Python + uv + uvicorn on port 8000)
#
# Stage 1 (builder): install uv, sync the workspace into a venv with --no-dev.
# Stage 2 (runner):  copy the venv + the source tree we actually need at
#                    runtime (gecko-core + gecko-api). Tests, CLI, demo-agent,
#                    and infra are excluded via .dockerignore.
# Final image: ~250 MB. Acceptable on Fargate; openai+supabase+x402+pgvector
# wheels are the bulk of it.
# =============================================================================

FROM python:3.12-slim AS builder

# uv (Astral): pull the static binary from Astral's official image instead
# of running their install.sh — the slim Python image lacks curl/wget. This
# is Astral's recommended pattern for Docker builds. Version pinned for
# reproducibility.
COPY --from=ghcr.io/astral-sh/uv:0.5.30 /uv /usr/local/bin/uv

WORKDIR /app

# Copy manifests + src for every workspace member that gecko-api transitively
# needs. We ship src in this layer (rather than splitting deps and src into
# separate layers) because uv installs workspace members as editable packages
# pointing at src/ — splitting the install causes the .pth files to be
# written before src exists, leaving gecko-core "installed" but unimportable.
COPY pyproject.toml uv.lock README.md ./
COPY packages/gecko-core/pyproject.toml ./packages/gecko-core/
COPY packages/gecko-api/pyproject.toml  ./packages/gecko-api/
COPY packages/gecko-mcp/pyproject.toml  ./packages/gecko-mcp/
COPY apps/cli/pyproject.toml            ./apps/cli/
COPY apps/demo-agent/pyproject.toml     ./apps/demo-agent/
COPY packages/gecko-core/src ./packages/gecko-core/src
COPY packages/gecko-api/src  ./packages/gecko-api/src
COPY packages/gecko-mcp/src  ./packages/gecko-mcp/src

# Single uv sync pass — installs gecko-api and its workspace dep gecko-core
# as editable packages with .pth files pointing into src/. The root
# pyproject.toml is a pure workspace umbrella; we have to target the package
# explicitly with --package. --reinstall-package gecko-core protects against
# uv's cache treating a stale dist-info from a prior build attempt as
# already-installed (which it does even when the .pth file is missing,
# leaving gecko-core unimportable).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package gecko-api \
            --reinstall-package gecko-core \
            --reinstall-package gecko-mcp \
            --reinstall-package gecko-api

# -----------------------------------------------------------------------------

FROM python:3.12-slim AS runner

# Non-root user — Fargate doesn't enforce it but it costs nothing to add.
RUN useradd --create-home --shell /bin/bash gecko

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/packages/gecko-core/src ./packages/gecko-core/src
COPY --from=builder /app/packages/gecko-api/src  ./packages/gecko-api/src
COPY --from=builder /app/packages/gecko-mcp/src  ./packages/gecko-mcp/src
COPY --from=builder /app/pyproject.toml ./
COPY --from=builder /app/packages/gecko-core/pyproject.toml ./packages/gecko-core/
COPY --from=builder /app/packages/gecko-api/pyproject.toml  ./packages/gecko-api/
COPY --from=builder /app/packages/gecko-mcp/pyproject.toml  ./packages/gecko-mcp/
COPY docker-entrypoint.sh ./

RUN chown -R gecko:gecko /app && chmod +x docker-entrypoint.sh
USER gecko

# Make the workspace venv the default Python.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

# ALB hits /healthz; this matches.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)" \
  || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
