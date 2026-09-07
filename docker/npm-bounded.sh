#!/bin/sh
set -u

attempts=${NPM_BOUNDED_ATTEMPTS:-3}
timeout_seconds=${NPM_BOUNDED_TIMEOUT_SECONDS:-300}
kill_after_seconds=${NPM_BOUNDED_KILL_AFTER_SECONDS:-15}

case "$attempts:$timeout_seconds:$kill_after_seconds" in
    *[!0-9:]*|0:*|*:0:*|*:0)
        echo "npm-bounded requires positive integer limits" >&2
        exit 2
        ;;
esac

attempt=1
while [ "$attempt" -le "$attempts" ]; do
    if timeout --signal=TERM --kill-after="${kill_after_seconds}s" \
        "${timeout_seconds}s" npm \
        --fetch-retries=3 \
        --fetch-retry-mintimeout=1000 \
        --fetch-retry-maxtimeout=5000 \
        --fetch-timeout=30000 \
        "$@"; then
        exit 0
    else
        status=$?
    fi
    if [ "$attempt" -ge "$attempts" ]; then
        echo "npm-bounded failed after ${attempts} attempt(s), status ${status}" >&2
        exit "$status"
    fi
    echo "npm-bounded attempt ${attempt}/${attempts} failed (status ${status}); retrying in 5s" >&2
    sleep 5
    attempt=$((attempt + 1))
done
