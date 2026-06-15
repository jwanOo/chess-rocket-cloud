# Chess Rocket — backend container (deployed to Fly.io).
#
# Bundles: Python 3.12, Stockfish, the project source, uv-managed deps.
# Listens on $PORT (Fly sets this to 8080) and binds 0.0.0.0 so the
# Fly proxy can reach us.

FROM python:3.12-slim AS base

# Stockfish ships in Debian's main repo as a precompiled binary — saves us
# building from source. `tini` reaps zombies (uv subprocesses) cleanly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        stockfish \
        tini \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# uv is the project's chosen package manager (mirrors local dev). Pin it so
# the image is reproducible.
RUN pip install "uv==0.5.13"

WORKDIR /app

# Dependency layer first — `uv sync --frozen` is cached as long as the lock
# file doesn't change.
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev || uv sync --no-dev

# App source.
COPY scripts/ ./scripts/
COPY puzzles/ ./puzzles/
# `data/` is gitignored runtime state — create it inside the image so the
# server can write to it; it'll be ephemeral (Fly volumes are optional).
RUN mkdir -p ./data

# Tell GameManager where Stockfish lives; ChessEngine reads STOCKFISH_PATH.
ENV STOCKFISH_PATH=/usr/games/stockfish
ENV PATH="/usr/games:${PATH}"

# Fly sets PORT in the env; default 8080 for local `docker run` testing.
ENV PORT=8080 \
    HOST=0.0.0.0

EXPOSE 8080

# Health check the container itself can answer (Fly also pings /healthz
# from outside via fly.toml).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uv", "run", "python", "scripts/dashboard_server.py"]
