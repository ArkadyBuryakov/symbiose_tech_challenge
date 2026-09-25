# Shared settings for proxying to the object store.
#
# The critical behaviour is range passthrough: PMTiles is read with HTTP range
# requests, so the client's `Range` header must reach S3 untouched and the 206
# (with `Content-Range` and `ETag`) must come back unmodified. Buffering is off
# so a range is streamed rather than accumulated.

proxy_http_version 1.1;
proxy_set_header Host $proxy_host;
proxy_set_header Connection "";
proxy_set_header X-Request-Id $request_id_final;

# Do NOT set or clear Range here: nginx forwards it by default, and clearing it
# would silently turn every tile read into a full-archive download.
proxy_buffering off;
proxy_request_buffering off;

proxy_connect_timeout 3s;
proxy_read_timeout 60s;

# Do not leak the object store's identity or its internal request ids.
proxy_hide_header x-amz-request-id;
proxy_hide_header x-amz-id-2;
proxy_hide_header x-amz-meta-server-side-encryption;
proxy_hide_header Server;
# Headers the object store adds that are wrong or meaningless at this edge:
# HSTS over plain HTTP locally, a CORS Vary for requests that are same-origin,
# and the store's own rate-limit accounting.
proxy_hide_header Strict-Transport-Security;
proxy_hide_header X-Xss-Protection;
proxy_hide_header X-Ratelimit-Limit;
proxy_hide_header X-Ratelimit-Remaining;
proxy_hide_header Vary;

# The edge owns caching and range advertisement. Replace, don't append: the
# store sends these too, and a duplicated `Accept-Ranges: bytes, bytes` is
# malformed. Each tile location adds its own Cache-Control.
proxy_hide_header Cache-Control;
proxy_hide_header Accept-Ranges;
add_header Accept-Ranges bytes always;

# Anything the object store says about the object itself is passed through:
# ETag, Content-Range, Content-Length, Last-Modified.
