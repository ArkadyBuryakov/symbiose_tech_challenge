# syntax=docker/dockerfile:1.10
#
# One-shot migration image: Alembic for the `catalog` schema plus the role and
# grant bootstrap. It is separate from the service images because it needs the
# migration scripts and a privileged database role that no service should have.
#
# On AWS this same image runs as a Helm pre-upgrade Job.

ARG PYTHON_VERSION=3.12.12
ARG UV_VERSION=0.12.10

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY packages/common/pyproject.toml packages/common/
COPY services/backend/pyproject.toml services/backend/
COPY services/gateway/pyproject.toml services/gateway/
COPY services/worker/pyproject.toml services/worker/
COPY services/edge-verifier/pyproject.toml services/edge-verifier/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package pmp-worker
COPY packages/common packages/common
COPY services/worker services/worker
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package pmp-worker

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ARG GIT_SHA=unknown
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root ops/db ./ops/db
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 GIT_SHA="${GIT_SHA}"
USER app
ENTRYPOINT ["python", "ops/db/bootstrap.py"]
