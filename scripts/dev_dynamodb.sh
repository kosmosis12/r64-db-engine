#!/usr/bin/env bash
# DynamoDB Local lifecycle helper. Safe to source: no shell-option mutation.

NAME="${R64_DDB_DEV_NAME:-ddb-test}"
IMAGE="${R64_DDB_DEV_IMAGE:-amazon/dynamodb-local:latest}"
ENV_FILE="${R64_DDB_DEV_ENV_FILE:-$HOME/.r64-db-engine/dynamodb-dev.env}"
cmd="${1:-start}"

resolve_endpoint() {
    local configured_endpoint="${DYNAMODB_ENDPOINT_URL:-}"
    local configured_port="${DYNAMODB_PORT:-${R64_DDB_DEV_PORT:-}}"
    if [ -n "$configured_endpoint" ]; then
        local parsed
        if ! parsed=$(.venv/bin/python -c '
import sys
from urllib.parse import urlparse
u = urlparse(sys.argv[1])
if u.scheme != "http" or u.hostname not in {"localhost", "127.0.0.1"} or u.path not in {"", "/"}:
    raise SystemExit("endpoint must be http://localhost:<port> or http://127.0.0.1:<port>")
if u.port is None:
    raise SystemExit("endpoint must include an explicit port")
print(u.port)
' "$configured_endpoint" 2>&1); then
            echo "[dev_dynamodb] invalid DYNAMODB_ENDPOINT_URL: $parsed" >&2
            return 1
        fi
        if [ -n "$configured_port" ] && [ "$configured_port" != "$parsed" ]; then
            echo "[dev_dynamodb] DYNAMODB_PORT=$configured_port conflicts with DYNAMODB_ENDPOINT_URL=$configured_endpoint" >&2
            return 1
        fi
        PORT="$parsed"
        ENDPOINT="$configured_endpoint"
    else
        PORT="${configured_port:-8010}"
        ENDPOINT="http://localhost:$PORT"
    fi
    case "$PORT" in
        ''|*[!0-9]*) echo "[dev_dynamodb] invalid port: $PORT" >&2; return 1 ;;
    esac
    if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
        echo "[dev_dynamodb] invalid port: $PORT" >&2
        return 1
    fi
}

if ! resolve_endpoint; then
    return 1 2>/dev/null || exit 1
fi

write_env_file() {
    mkdir -p "$(dirname "$ENV_FILE")"
    chmod 700 "$(dirname "$ENV_FILE")"
    {
        printf 'export AWS_ACCESS_KEY_ID=%q\n' local
        printf 'export AWS_SECRET_ACCESS_KEY=%q\n' local
        printf 'export AWS_DEFAULT_REGION=%q\n' us-west-2
        printf 'export AWS_REGION=%q\n' us-west-2
        printf 'export DYNAMODB_PORT=%q\n' "$PORT"
        printf 'export DYNAMODB_ENDPOINT_URL=%q\n' "$ENDPOINT"
    } >"$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

wait_ready() {
    local deadline=$(( $(date +%s) + 45 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local AWS_DEFAULT_REGION=us-west-2 \
            .venv/bin/python -c "import boto3; boto3.client('dynamodb', endpoint_url='$ENDPOINT').list_tables(Limit=1)" \
            >/dev/null 2>&1; then
            echo "[dev_dynamodb] dynamodb is ready"
            return 0
        fi
        sleep 0.5
    done
    echo "[dev_dynamodb] timed out waiting for dynamodb" >&2
    return 1
}

port_occupant() {
    local container
    container=$(docker ps --filter "publish=$PORT" --format 'container {{.Names}} ({{.Image}})' | head -n 1)
    if [ -n "$container" ]; then
        printf '%s' "$container"
        return
    fi
    local process
    process=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -n 1)
    if [ -n "$process" ]; then
        printf '%s' "$process"
        return
    fi
    process=$(ss -H -ltnp "sport = :$PORT" 2>/dev/null | head -n 1)
    printf '%s' "${process:-unknown listener}"
}

start() {
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
        local published
        published=$(docker port "$NAME" 8000/tcp 2>/dev/null | head -n 1)
        if [[ "$published" != *":$PORT" ]]; then
            echo "[dev_dynamodb] $NAME is running at ${published:-an unknown port}, not $ENDPOINT" >&2
            return 1 2>/dev/null || exit 1
        fi
        echo "[dev_dynamodb] already running at $ENDPOINT"
    else
        if ss -H -ltn "sport = :$PORT" 2>/dev/null | grep -q .; then
            echo "[dev_dynamodb] port $PORT is already bound by $(port_occupant); refusing to start $NAME" >&2
            return 1 2>/dev/null || exit 1
        fi
        if ! docker run --rm -d --name "$NAME" -p "$PORT:8000" "$IMAGE" \
            -jar DynamoDBLocal.jar -inMemory >/dev/null; then
            echo "[dev_dynamodb] failed to start $NAME on port $PORT" >&2
            return 1 2>/dev/null || exit 1
        fi
        echo "[dev_dynamodb] started $NAME on port $PORT"
        if ! wait_ready; then
            docker rm -f "$NAME" >/dev/null 2>&1 || true
            return 1 2>/dev/null || exit 1
        fi
    fi
    write_env_file
    echo "source $ENV_FILE"
}

stop() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "[dev_dynamodb] stopped"
}

env_only() {
    write_env_file
    if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
        export AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local
        export AWS_DEFAULT_REGION=us-west-2 AWS_REGION=us-west-2
        export DYNAMODB_PORT="$PORT" DYNAMODB_ENDPOINT_URL="$ENDPOINT"
    else
        echo "[dev_dynamodb] wrote $ENV_FILE (mode 0600)"
        echo "source $ENV_FILE"
    fi
}

case "$cmd" in
    start) start ;;
    stop) stop ;;
    reset) stop; start ;;
    env) env_only ;;
    *) echo "usage: $0 [start|stop|reset|env]" >&2; exit 1 ;;
esac
