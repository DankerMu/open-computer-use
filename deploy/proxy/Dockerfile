# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
FROM python:3.12-alpine

RUN apk add --no-cache nginx \
    && adduser -D -u 1000 ocu-proxy \
    && mkdir -p /opt/ocu-proxy /var/lib/nginx /var/log/nginx /run/nginx \
    && chown -R ocu-proxy:ocu-proxy /opt/ocu-proxy /var/lib/nginx /var/log/nginx /run/nginx

WORKDIR /opt/ocu-proxy
COPY render.py /opt/ocu-proxy/render.py
COPY nginx.conf.in /opt/ocu-proxy/nginx.conf.in
COPY routes.json /opt/ocu-proxy/routes.json
COPY entrypoint.sh /opt/ocu-proxy/entrypoint.sh
RUN chown -R ocu-proxy:ocu-proxy /opt/ocu-proxy \
    && chmod 0555 /opt/ocu-proxy/entrypoint.sh

USER ocu-proxy
EXPOSE 8082
ENTRYPOINT ["/opt/ocu-proxy/entrypoint.sh"]
