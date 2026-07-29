#!/usr/bin/env bash
# DynamoDB Local lifecycle helper. Safe to source: no shell-option mutation.

NAME="${R64_DDB_DEV_NAME:-ddb-test}"
PORT="${R64_DDB_DEV_PORT:-8000}"
IMAGE="${R64_DDB_DEV_IMAGE:-amazon/dynamodb-local:latest}"
ENV_FILE="${R64_DDB_DEV_ENV_FILE:-$HOME/.r64-db-engine/dynamodb-dev.env}"
cmd="${1:-start}"

write_env_file() {
    mkdir -p "$(dirname "$ENV_FILE")"
    chmod 700 "$(dirname "$ENV_FILE")"
    {
        printf 'export AWS_ACCESS_KEY_ID=%q\n' local
        printf 'export AWS_SECRET_ACCESS_KEY=%q\n' local
        printf 'export AWS_DEFAULT_REGION=%q\n' us-west-2
        printf 'export AWS_REGION=%q\n' us-west-2
        printf 'export DYNAMODB_ENDPOINT_URL=%q\n' "http://localhost:$PORT"
    } >"$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

wait_ready() {
    local deadline=$(( $(date +%s) + 45 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local AWS_DEFAULT_REGION=us-west-2 \
            .venv/bin/python -c "import boto3; boto3.client('dynamodb', endpoint_url='http://localhost:$PORT').list_tables(Limit=1)" \
            >/dev/null 2>&1; then
            echo "[dev_dynamodb] dynamodb is ready"
            return 0
        fi
        sleep 0.5
    done
    echo "[dev_dynamodb] timed out waiting for dynamodb" >&2
    return 1
}

start() {
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
        echo "[dev_dynamodb] already running"
    else
        if ! docker run --rm -d --name "$NAME" -p "$PORT:8000" "$IMAGE" \
            -jar DynamoDBLocal.jar -sharedDb >/dev/null; then
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
        export DYNAMODB_ENDPOINT_URL="http://localhost:$PORT"
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
