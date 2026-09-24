# Headers every response carries.
#
# nginx's `add_header` is replace-not-merge across block levels: a location that
# adds any header of its own drops the ones inherited from `server`. Repeating
# this include in each such location is the supported way to keep them.
add_header X-Request-Id $request_id_final always;
add_header X-Content-Type-Options nosniff always;
add_header Referrer-Policy strict-origin-when-cross-origin always;
