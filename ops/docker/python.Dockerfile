# syntax=docker/dockerfile:1.10
#
# One image recipe for every Python service in the workspace.
#
# The four services (gateway, backend, worker, edge-verifier) differ only in
# which workspace package is installed and which module is executed, so they
# share this file: a base-image or hardening change is made once, not four
# times. Build with:
#
#   docker build -f ops/docker/python.Dockerfile \
#       --build-arg PACKAGE=pmp-backend --build-arg SERVICE_DIR=services/backend \
#       --build-arg MODULE=pmp_backend .
#
# The build context is the repository root because uv needs the whole
# workspace (lock file + every member's manifest) to resolve.

ARG PYTHON_VERSION=3.12.12
ARG UV_VERSION=0.12.10

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --------------------------------------------------------------------------
# Builder: resolve and install into a self-contained virtualenv.
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

ARG PACKAGE

# Dependency layer: only manifests, so it is cached across source edits.
# Every workspace member's manifest is needed for uv to validate the workspace.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/common/pyproject.toml packages/common/
COPY services/backend/pyproject.toml services/backend/
COPY services/gateway/pyproject.toml services/gateway/
COPY services/worker/pyproject.toml services/worker/
COPY services/edge-verifier/pyproject.toml services/edge-verifier/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package "${PACKAGE}"

# Source layer.
ARG SERVICE_DIR
COPY packages/common packages/common
COPY ${SERVICE_DIR} ${SERVICE_DIR}
# --no-editable installs real wheels, so the runtime stage needs only the venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

# --------------------------------------------------------------------------
# Runtime: no uv, no build tools, no sources — just the venv.
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG MODULE
ARG GIT_SHA=unknown
ARG HEALTH_PORT=8000

LABEL org.opencontainers.image.source="https://github.com/example/pmtiles-platform" \
      org.opencontainers.image.revision="${GIT_SHA}"

# curl is kept deliberately: the container HEALTHCHECK below uses it, and it is
# the one debugging tool worth the ~1 MB in an incident.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=root:root /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PMP_MODULE="${MODULE}" \
    GIT_SHA="${GIT_SHA}" \
    HTTP_PORT="${HEALTH_PORT}"

USER app
EXPOSE ${HEALTH_PORT}

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${HTTP_PORT}/healthz" || exit 1

# `exec` keeps python as PID 1 so SIGTERM reaches the graceful-shutdown path.
ENTRYPOINT ["/bin/sh", "-c", "exec python -m \"${PMP_MODULE}\" \"$@\"", "--"]
