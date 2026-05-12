#!/usr/bin/env bash
# Spin up a local Postgres for development.
# Usage: ./scripts/dev_postgres.sh [start|stop|reset|psql]
set -euo pipefail

NAME="${R64_DB_DEV_NAME:-r64-db-engine-pg}"
PORT="${R64_DB_DEV_PORT:-55432}"
PASSWORD="${R64_DB_DEV_PASSWORD:-row64dev}"
IMAGE="${R64_DB_DEV_IMAGE:-postgres:16-alpine}"

cmd="${1:-start}"

start() {
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
        echo "[dev_postgres] already running"
    else
        docker run --rm -d \
            --name "$NAME" \
            -e POSTGRES_PASSWORD="$PASSWORD" \
            -e POSTGRES_DB=analytics \
            -p "$PORT:5432" \
            "$IMAGE" >/dev/null
        echo "[dev_postgres] started $NAME on port $PORT"
    fi

    echo
    echo "Connection details:"
    echo "  host:     localhost"
    echo "  port:     $PORT"
    echo "  database: analytics"
    echo "  user:     postgres"
    echo "  password: $PASSWORD"
    echo
    echo "Suggested env for examples/minimal.yaml:"
    echo "  export PG_HOST=localhost PG_USER=postgres PG_PASSWORD=$PASSWORD"
}

stop() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "[dev_postgres] stopped"
}

reset() {
    stop
    start
}

psql() {
    docker exec -it "$NAME" psql -U postgres -d analytics
}

case "$cmd" in
    start) start ;;
    stop)  stop ;;
    reset) reset ;;
    psql)  psql ;;
    *) echo "usage: $0 [start|stop|reset|psql]" >&2; exit 1 ;;
esac
