# syntax=docker/dockerfile:1.10
#
# Auth service (Node 22 + TypeScript, compiled ahead of time).
#
# The build context is `services/auth`: unlike the Python services this one has
# no workspace dependencies, so it does not need the repository root.

ARG NODE_VERSION=22-slim

# --------------------------------------------------------------------------
# Dependencies (production only) — cached separately from the build.
# --------------------------------------------------------------------------
FROM node:${NODE_VERSION} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --omit=dev --ignore-scripts

# --------------------------------------------------------------------------
# Build: full dependency tree, type-check and emit JavaScript.
# --------------------------------------------------------------------------
FROM node:${NODE_VERSION} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY tsconfig.json ./
COPY src ./src
# `tsc` type-checks as it emits, so a type error fails the image build.
RUN npx tsc --build

# --------------------------------------------------------------------------
# Runtime: compiled JS + production dependencies only. No compiler, no sources.
# --------------------------------------------------------------------------
FROM node:${NODE_VERSION} AS runtime

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=deps  --chown=root:root /app/node_modules ./node_modules
COPY --from=build --chown=root:root /app/dist ./dist
COPY --chown=root:root package.json ./

ENV NODE_ENV=production \
    GIT_SHA=${GIT_SHA} \
    HTTP_PORT=3000

# `node` is a pre-existing non-root user in the official image.
USER node
EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${HTTP_PORT}/healthz" || exit 1

# Node as PID 1 so SIGTERM reaches the graceful-shutdown handler.
CMD ["node", "dist/index.js"]
