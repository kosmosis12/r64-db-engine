#!/usr/bin/env bash
# Spin up an ephemeral local Postgres for r64-db-engine development.
#
# Uses port 5433 by default so it doesn't collide with a system Postgres
# on the standard 5432. Defaults match scripts/seed_postgres.py and the
# hard-coded connection in examples/minimal.yaml + examples/incremental.yaml.
#
# Usage:
#   ./scripts/dev_postgres.sh start    # docker run (idempotent)
#   ./scripts/dev_postgres.sh stop     # docker rm -f
#   ./scripts/dev_postgres.sh reset    # stop + start
#   ./scripts/dev_postgres.sh psql     # interactive psql shell
#   ./scripts/dev_postgres.sh env      # print export lines for sourcing
#
# All defaults overridable via env vars:
#   R64_DB_DEV_NAME, R64_DB_DEV_PORT, R64_DB_DEV_PASSWORD, R64_DB_DEV_IMAGE
# set -euo pipefail removed: poisons sourcing shell

NAME="${R64_DB_DEV_NAME:-r64-db-engine-pg}"
PORT="${R64_DB_DEV_PORT:-5433}"
PASSWORD="${R64_DB_DEV_PASSWORD:-row64dev}"
IMAGE="${R64_DB_DEV_IMAGE:-postgres:16-alpine}"
DATABASE="analytics"
USER_NAME="postgres"

cmd="${1:-start}"

start() {
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
        echo "[dev_postgres] already running"
    else
        docker run --rm -d \
            --name "$NAME" \
            -e POSTGRES_PASSWORD="$PASSWORD" \
            -e POSTGRES_DB="$DATABASE" \
            -p "$PORT:5432" \
            "$IMAGE" >/dev/null
        echo "[dev_postgres] started $NAME on port $PORT"
        if ! wait_ready; then
            echo "[dev_postgres] ERROR: postgres failed to become ready — cleaning up" >&2
            docker rm -f "$NAME" >/dev/null 2>&1 || true
            return 1 2>/dev/null || exit 1
        fi
    fi

    # Export connection details into the current shell when sourced. When
    # the script is executed (not sourced) these still print so the user
    # can copy them.
    export PG_HOST="localhost"
    export PG_PORT="$PORT"
    export PG_DATABASE="$DATABASE"
    export PG_USER="$USER_NAME"
    export PG_PASSWORD="$PASSWORD"
    export PGPASSWORD="$PASSWORD"

    echo
    echo "Connection details:"
    echo "  host:     $PG_HOST"
    echo "  port:     $PG_PORT"
    echo "  database: $PG_DATABASE"
    echo "  user:     $PG_USER"
    echo "  password: $PG_PASSWORD"
    echo
    echo "To use the same vars in this shell, source the script:"
    echo "  source scripts/dev_postgres.sh env"
    echo "Or copy/paste:"
    print_env
}

stop() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "[dev_postgres] stopped"
}

reset() {
    stop
    start
}

psql_shell() {
    docker exec -it "$NAME" psql -U "$USER_NAME" -d "$DATABASE"
}

print_env() {
    echo "  export PG_HOST=localhost"
    echo "  export PG_PORT=$PORT"
    echo "  export PG_DATABASE=$DATABASE"
    echo "  export PG_USER=$USER_NAME"
    echo "  export PG_PASSWORD=$PASSWORD"
    echo "  export PGPASSWORD=$PASSWORD"
}

env_only() {
    # When sourced: actually export. When executed: just print.
    if (return 0 2>/dev/null); then
        export PG_HOST="localhost"
        export PG_PORT="$PORT"
        export PG_DATABASE="$DATABASE"
        export PG_USER="$USER_NAME"
        export PG_PASSWORD="$PASSWORD"
        export PGPASSWORD="$PASSWORD"
    else
        print_env
    fi
}

wait_ready() {
    local deadline=$(( $(date +%s) + 30 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if docker exec "$NAME" pg_isready -U "$USER_NAME" -d "$DATABASE" >/dev/null 2>&1; then
            echo "[dev_postgres] postgres is ready"
            return 0
        fi
        sleep 0.5
    done
    echo "[dev_postgres] timed out waiting for postgres" >&2
    return 1
}

case "$cmd" in
    start) start ;;
    stop)  stop ;;
    reset) reset ;;
    psql)  psql_shell ;;
    env)   env_only ;;
    *) echo "usage: $0 [start|stop|reset|psql|env]" >&2; exit 1 ;;
esac
