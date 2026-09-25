FROM docker:27-cli

RUN apk add --no-cache coreutils

COPY stop-overage.sh /usr/local/bin/stop-overage.sh
RUN chmod 0555 /usr/local/bin/stop-overage.sh

CMD ["/bin/sh", "-c", "while :; do /usr/local/bin/stop-overage.sh; sleep ${RETENTION_CHECK_INTERVAL_SECONDS:-3600}; done"]
