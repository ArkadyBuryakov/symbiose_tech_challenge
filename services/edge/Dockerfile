# syntax=docker/dockerfile:1.10
#
# Edge: nginx serving the static map pages and routing everything else.
#
# This image is the local stand-in for CloudFront and is deleted when the
# platform moves to AWS (see docs/aws-mapping.md); it is still built properly so
# it behaves like the real thing while it exists.
#
# Build context is the repository root: the image bundles `web/`.

FROM nginx:1.29-alpine AS runtime

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}"

# The official entrypoint renders /etc/nginx/templates/*.template through
# envsubst into /etc/nginx/conf.d/ at start-up, substituting only variables that
# are actually set — nginx's own $variables are left alone.
COPY services/edge/nginx.conf /etc/nginx/nginx.conf
COPY services/edge/templates/ /etc/nginx/templates/
COPY services/edge/conf.d/ /etc/nginx/conf.d/
COPY web/ /usr/share/nginx/html/

ENV GIT_SHA=${GIT_SHA}

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -qO- http://127.0.0.1/healthz >/dev/null || exit 1
