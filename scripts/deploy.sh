#!/bin/sh
set -eu

PROJECT_NAME="${DEPLOY_PROJECT_NAME:-cyrano}"
COMPOSE_FILE_NAME="docker-compose.yml"
LOCK_FILE=".git/deploy.lock"
MAIN_REF="origin/main"
HEALTHCHECK_SERVICES="app worker postgres redis"
MIGRATE_SERVICE="migrate"
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-240}"
HEALTH_INTERVAL="${DEPLOY_HEALTH_INTERVAL:-5}"
SHA_LENGTH=40
SHORT_LENGTH=7

EXIT_FAILED=1
EXIT_BAD_ARGUMENT=2
EXIT_LOCKED=3
EXIT_NOT_ON_MAIN=4
EXIT_PRECONDITION=5

if [ "$#" -ne 1 ]; then
    echo "deploy: expected exactly one argument, the commit sha"
    exit "$EXIT_BAD_ARGUMENT"
fi

sha="$1"
case "$sha" in
    *[!0-9a-f]*)
        echo "deploy: the argument must be 40 lowercase hex characters"
        exit "$EXIT_BAD_ARGUMENT"
        ;;
esac
if [ "${#sha}" -ne "$SHA_LENGTH" ]; then
    echo "deploy: the argument must be 40 lowercase hex characters"
    exit "$EXIT_BAD_ARGUMENT"
fi

for value in "$HEALTH_TIMEOUT" "$HEALTH_INTERVAL"; do
    case "$value" in
        ''|*[!0-9]*|0)
            echo "deploy: the health wait settings must be positive whole numbers"
            exit "$EXIT_PRECONDITION"
            ;;
    esac
done

root=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$root"

if [ ! -d .git ]; then
    echo "deploy: $root is not a repository checkout"
    exit "$EXIT_PRECONDITION"
fi

if ! command -v flock >/dev/null 2>&1; then
    echo "deploy: flock is not installed, refusing to run without a lock"
    exit "$EXIT_PRECONDITION"
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "deploy: another deploy is running"
    exit "$EXIT_LOCKED"
fi

export COMPOSE_FILE="$COMPOSE_FILE_NAME"
export COMPOSE_PROJECT_NAME="$PROJECT_NAME"

short_sha() {
    printf '%s' "$1" | cut -c1-"$SHORT_LENGTH"
}

container_id() {
    docker compose ps -q "$1" 2>/dev/null | head -n 1
}

any_container_id() {
    docker compose ps -a -q "$1" 2>/dev/null | head -n 1
}

poll_services() {
    pending=""
    broken=""
    for service in $HEALTHCHECK_SERVICES; do
        container=$(container_id "$service")
        if [ -z "$container" ]; then
            broken="$broken $service"
            continue
        fi
        status=$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null) || status=""
        case "$status" in
            healthy) ;;
            unhealthy) broken="$broken $service" ;;
            *) pending="$pending $service" ;;
        esac
    done
    container=$(any_container_id "$MIGRATE_SERVICE")
    if [ -n "$container" ]; then
        code=$(docker inspect --format '{{.State.ExitCode}}' "$container" 2>/dev/null) || code=""
        if [ "$code" != "0" ]; then
            broken="$broken $MIGRATE_SERVICE"
        fi
    fi
}

wait_healthy() {
    elapsed=0
    while :; do
        poll_services
        if [ -n "$broken" ]; then
            failed="$broken"
            return 1
        fi
        if [ -z "$pending" ]; then
            return 0
        fi
        if [ "$elapsed" -ge "$HEALTH_TIMEOUT" ]; then
            failed="$pending"
            return 1
        fi
        sleep "$HEALTH_INTERVAL"
        elapsed=$((elapsed + HEALTH_INTERVAL))
    done
}

bring_up() {
    if ! docker compose up -d --build --remove-orphans; then
        failed=" compose-up"
        return 1
    fi
    wait_healthy
}

previous=$(git rev-parse HEAD)

if ! git fetch --quiet origin; then
    echo "deploy: git fetch failed"
    exit "$EXIT_PRECONDITION"
fi

if ! git cat-file -e "${sha}^{commit}" 2>/dev/null; then
    echo "deploy: commit $(short_sha "$sha") is unknown here"
    exit "$EXIT_NOT_ON_MAIN"
fi
if ! git merge-base --is-ancestor "$sha" "$MAIN_REF"; then
    echo "deploy: commit $(short_sha "$sha") is not reachable from $MAIN_REF"
    exit "$EXIT_NOT_ON_MAIN"
fi

if ! git checkout --quiet --detach "$sha"; then
    echo "deploy: checkout of $(short_sha "$sha") failed, nothing was changed"
    exit "$EXIT_PRECONDITION"
fi

failed=""
echo "deploy: starting $(short_sha "$sha"), previous $(short_sha "$previous")"
if bring_up; then
    echo "deploy: ok $(short_sha "$sha") $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    docker compose ps
    exit 0
fi

echo "deploy: failed services:$failed"
echo "deploy: rolling back to $(short_sha "$previous")"
if ! git checkout --quiet --detach "$previous"; then
    echo "deploy: rollback checkout failed, the server needs a manual look"
    exit "$EXIT_FAILED"
fi
failed=""
if bring_up; then
    echo "deploy: rollback came up, the server runs $(short_sha "$previous") again"
    exit "$EXIT_FAILED"
fi
echo "deploy: rollback failed services:$failed"
echo "deploy: the server needs a manual look"
exit "$EXIT_FAILED"
