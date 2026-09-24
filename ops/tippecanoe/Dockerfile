# syntax=docker/dockerfile:1.10
#
# tippecanoe, built from source and pinned to a tag.
#
# No maintained tippecanoe image is published to a public registry (the
# felt/tippecanoe ghcr repository is not readable anonymously, and the old
# klokantech image predates PMTiles output), so this builds it once and caches
# it locally as `pmp/tippecanoe:<version>`. Used only by
# scripts/make-synthetic-pmtiles.sh — it is a developer tool, not part of the
# running platform.

ARG TIPPECANOE_VERSION=2.79.0

FROM debian:bookworm-slim AS build
ARG TIPPECANOE_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential git libsqlite3-dev zlib1g-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${TIPPECANOE_VERSION}" \
        https://github.com/felt/tippecanoe.git /src \
    && make -C /src -j"$(nproc)" \
    && make -C /src install

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsqlite3-0 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/bin/tippecanoe* /usr/local/bin/
COPY --from=build /usr/local/bin/tile-join /usr/local/bin/
WORKDIR /data
ENTRYPOINT ["tippecanoe"]
