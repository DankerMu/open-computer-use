# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
FROM onlyoffice/documentserver@sha256:e3da62a847b9a5d51a11f73cfea1d9c13c3be3809614490d4edddcf01dcf919b

# Keep Docker Desktop emulator caches out of committed runtime layers.
RUN --mount=type=bind,target=/ocu-documentserver,readonly --mount=type=tmpfs,target=/root \
    documentserver_mtime="$(stat -c %y /var/www/onlyoffice/documentserver)" && \
    python3 -B /ocu-documentserver/patch.py --root /var/www/onlyoffice/documentserver/web-apps/apps --inventory /ocu-documentserver/patch-inventory.json && \
    cp /ocu-documentserver/OCU-MODIFICATIONS /var/www/onlyoffice/documentserver/OCU-MODIFICATIONS && \
    touch -m -d "$documentserver_mtime" /var/www/onlyoffice/documentserver
