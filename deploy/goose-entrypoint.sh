#!/bin/sh
# Fixed-purpose entrypoint for the foundation migration image.
# It exposes only the Goose CLI version path and four whitelisted database
# commands. Raw Goose stdout/stderr is discarded inside the container so a
# connection string or failed SQL statement cannot reach Docker logs.

set -eu

GOOSE_BIN=/usr/local/bin/goose
MIGRATIONS_DIR=/migrations

reject() {
    printf '%s\n' '{"event":"migration","command":"rejected","status":"error"}'
    exit 2
}

# Goose's own -version flag returns before any environment or database access.
case "${1-}" in
    -version|--version)
        [ "$#" -eq 1 ] || reject
        exec "$GOOSE_BIN" -version
        ;;
esac

if [ "$#" -eq 0 ]; then
    command=up
elif [ "$#" -eq 1 ]; then
    case "$1" in
        up|status|version|validate) command=$1 ;;
        *) reject ;;
    esac
else
    reject
fi

# The DSN must come only from the runtime environment. Ignore ambient
# libpq-style variables so they cannot redirect or complete the connection.
unset PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD PGPASSFILE \
    PGSERVICE PGSERVICEFILE PGOPTIONS PGSSLMODE PGSSLCERT PGSSLKEY \
    PGSSLROOTCERT PGSSLPASSWORD PGTARGETSESSIONATTRS PGGSSENCMODE \
    PGCONNECT_TIMEOUT PGAPPNAME PGCLIENTENCODING PGKRBSRVNAME PGGSSLIB \
    PGCHANNELBINDING PGREQUIRESSL

printf '{"event":"migration","command":"%s","status":"started"}\n' "$command"

if "$GOOSE_BIN" -env=none -no-color -timeout=60s -dir="$MIGRATIONS_DIR" "$command" >/dev/null 2>&1; then
    printf '{"event":"migration","command":"%s","status":"ok"}\n' "$command"
    exit 0
else
    rc=$?
    printf '{"event":"migration","command":"%s","status":"failed"}\n' "$command"
    exit "$rc"
fi
