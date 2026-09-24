#!/usr/bin/env bash
# Wait for every one-shot initialisation container and verify it exited 0.
#
# `docker compose up --wait` treats any exited container as a failure, so the
# one-shots are excluded from that wait and checked here, where a non-zero exit
# can be reported together with its logs.
set -uo pipefail

ONESHOTS=${ONESHOTS:-"migrate kafka-init s3-init"}
COMPOSE=${COMPOSE_CMD:-"docker compose"}
TIMEOUT=${ONESHOT_TIMEOUT:-180}
status=0

for service in $ONESHOTS; do
    cid=$($COMPOSE ps -aq "$service" 2>/dev/null | head -1)
    if [[ -z "$cid" ]]; then
        echo "  $service: not started" >&2
        status=1
        continue
    fi

    deadline=$((SECONDS + TIMEOUT))
    while [[ "$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)" == "running" ]]; do
        if (( SECONDS > deadline )); then
            echo "  $service: TIMED OUT after ${TIMEOUT}s" >&2
            $COMPOSE logs --tail=40 "$service" >&2
            status=1
            break
        fi
        sleep 1
    done

    code=$(docker inspect -f '{{.State.ExitCode}}' "$cid" 2>/dev/null)
    state=$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)
    [[ "$state" == "running" ]] && continue

    if [[ "$code" == "0" ]]; then
        echo "  $service: completed"
    else
        echo "  $service: FAILED (exit $code)" >&2
        $COMPOSE logs --tail=40 "$service" >&2
        status=1
    fi
done

exit $status
