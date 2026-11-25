#!/usr/bin/env bash
# Own the cluster and its socket. Never select a developer's database for contract tests.
set -euo pipefail
cd "$(dirname "$0")/.."
# Mint test custody evidence immediately after a new native mount, before any DB or archive exists.
if [[ "${1:-}" != --native-test-volume ]]; then
    test_uid=$(id -u)
    test_gid=$(id -g)
    ns=(unshare --user --map-current-user --keep-caps --mount --propagation private)
    if ! "${ns[@]}" true 2>/dev/null; then
        ns=(sudo -n unshare --mount --propagation private)
    fi
    exec "${ns[@]}" /bin/sh -ec '
        /usr/bin/mount -t tmpfs -o "mode=0700,uid=$1,gid=$2,size=2G" tmpfs /mnt
        /usr/bin/mkdir -m 0700 /mnt/probe-native /mnt/probe-bind /mnt/probe-unregistered
        /usr/bin/mount -t tmpfs -o "mode=0700,uid=$1,gid=$2,size=1m" tmpfs /mnt/probe-native
        /usr/bin/mount --bind /mnt/probe-native /mnt/probe-bind
        /usr/bin/mount -t tmpfs -o "mode=0700,uid=$1,gid=$2,size=1m" tmpfs /mnt/probe-unregistered
        uid=$1 gid=$2
        shift 2
        if [ "$(id -u)" != "$uid" ]; then
            exec /usr/bin/setpriv --reuid "$uid" --regid "$gid" --clear-groups --no-new-privs "$@"
        fi
        exec /usr/bin/setpriv --inh-caps=-all --ambient-caps=-all --no-new-privs "$@"
    ' fixture "$test_uid" "$test_gid" /usr/bin/env "PATH=$PATH" "PYTHONPATH=${PYTHONPATH:-}" \
        "RS_MEASURE_DECISIONS=${RS_MEASURE_DECISIONS:-0}" "RS_MEASURE_RELATIONS=${RS_MEASURE_RELATIONS:-0}" "PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-${XDG_CACHE_HOME:-$HOME/.cache}/ms-playwright}" "RS_TEST_BROWSER_OUT=${RS_TEST_BROWSER_OUT:-}" \
        "RS_TEST_PG_MAJOR=${RS_TEST_PG_MAJOR:-}" "$(command -v python)" -m operational.tests.native_volume \
        bash operational/test-postgres.sh --native-test-volume "$@"
fi
shift
bindir=$(pg_config --bindir)
if [[ -n "${RS_TEST_PG_MAJOR:-}" ]]; then
    bindir="/usr/lib/postgresql/$RS_TEST_PG_MAJOR/bin"
    [[ "$("$bindir/postgres" -V)" == *" $RS_TEST_PG_MAJOR."* ]]
fi
python_bin=$(command -v python)
export PATH="$bindir:$PATH"
scratch=$(mktemp -d)
export PGHOST="$scratch" PGPORT=5432 PGUSER=rs_test PGDATABASE=postgres
unset PGSERVICE PGSERVICEFILE PGHOSTADDR PGOPTIONS PGPASSWORD
cleanup() {
    if [[ -f "$scratch/data/postmaster.pid" ]]; then
        "$bindir/pg_ctl" -D "$scratch/data" -m fast -w stop >/dev/null
    fi
    rm -rf -- "$scratch"
}
trap cleanup EXIT
"$python_bin" -m operational.tests.cluster_auth prepare
export PGPASSFILE="$scratch/harness.pass"
"$bindir/initdb" -D "$scratch/data" --username="$PGUSER" --encoding=UTF8 --no-locale \
    --auth-local=scram-sha-256 --pwfile="$scratch/bootstrap.pass" --auth-host=reject >/dev/null
"$bindir/pg_ctl" -D "$scratch/data" -l "$scratch/server.log" \
    -o "-k '$scratch' -c listen_addresses='' -c log_min_error_statement=panic -c log_min_messages=panic" -w start >/dev/null
"$bindir/psql" -XAtc 'SELECT version()'
"$python_bin" -m operational.tests.cluster_auth create
export PGUSER=rs_migration
export RS_TEST_PG_SOCKET="$scratch"
"$python_bin" -m pytest -o addopts= -q operational/tests "$@"
