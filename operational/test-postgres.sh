#!/usr/bin/env bash
# Own the cluster and its socket. Never select a developer's database for contract tests.
set -euo pipefail
cd "$(dirname "$0")/.."
bindir=$(pg_config --bindir)
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
"$bindir/initdb" -D "$scratch/data" --username="$PGUSER" --encoding=UTF8 --no-locale \
    --auth-local=trust --auth-host=reject >/dev/null
"$bindir/pg_ctl" -D "$scratch/data" -l "$scratch/server.log" \
    -o "-k '$scratch' -c listen_addresses='' -c log_min_error_statement=panic -c log_min_messages=panic" -w start >/dev/null
"$bindir/psql" -XAtc 'SELECT version()'
export RS_TEST_PG_SOCKET="$scratch"
"$python_bin" -m pytest -o addopts= -q operational/tests "$@"
