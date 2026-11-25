#!/usr/bin/env bash
# Create loopback before any service or client starts. Exit if namespace setup fails.
set -euo pipefail
uid=$(id -u)
gid=$(id -g)
ns=(unshare --user --map-current-user --keep-caps --net)
if ! "${ns[@]}" true 2>/dev/null; then
    ns=(sudo -n unshare --net)
fi
exec "${ns[@]}" /bin/bash -ec '
    ip link set lo up
    uid=$1 gid=$2
    shift 2
    if [ "$(id -u)" != "$uid" ]; then
        exec setpriv --reuid "$uid" --regid "$gid" --clear-groups "$@"
    fi
    exec "$@"
' isolated "$uid" "$gid" /usr/bin/env "PATH=$PATH" "PYTHONPATH=${PYTHONPATH:-}" \
    "HOME=$HOME" "TMPDIR=${TMPDIR:-/tmp}" "RS_TEST_PG_MAJOR=${RS_TEST_PG_MAJOR:-}" \
    "RS_MEASURE_DECISIONS=${RS_MEASURE_DECISIONS:-0}" "PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}" \
    "RS_TEST_BROWSER_OUT=${RS_TEST_BROWSER_OUT:-}" bash operational/test-postgres.sh "$@"
