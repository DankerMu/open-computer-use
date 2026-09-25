#!/bin/sh
# Stop only sandbox containers that exceed the permitted continuous runtime.
# Deliberately never removes containers, named volumes, chat data, images, or
# build cache. A later MCP request restarts the stopped sandbox using its
# existing chat workspace volume.

set -eu

max_age_hours=${CONTAINER_MAX_AGE_HOURS:-168}
case "$max_age_hours" in
    ''|*[!0-9]*)
        printf '%s\n' '[retention-guard] CONTAINER_MAX_AGE_HOURS must be a non-negative integer' >&2
        exit 2
        ;;
esac

now_epoch=$(date -u +%s)
max_age_seconds=$((max_age_hours * 3600))

docker ps -q --filter 'label=managed-by=mcp-computer-use-orchestrator' | while IFS= read -r container_id; do
    [ -n "$container_id" ] || continue

    started_at=$(docker inspect --format '{{.State.StartedAt}}' "$container_id" 2>/dev/null || true)
    started_epoch=$(date -u -d "$started_at" +%s 2>/dev/null || true)
    [ -n "$started_epoch" ] || continue

    age_seconds=$((now_epoch - started_epoch))
    if [ "$age_seconds" -lt "$max_age_seconds" ]; then
        continue
    fi

    container_name=$(docker inspect --format '{{.Name}}' "$container_id" 2>/dev/null | sed 's#^/##' || true)
    printf '[retention-guard] stopping %s after %sh of continuous runtime\n' \
        "${container_name:-$container_id}" "$((age_seconds / 3600))"
    docker stop --time 30 "$container_id" >/dev/null || \
        printf '[retention-guard] could not stop %s\n' "${container_name:-$container_id}" >&2
done
