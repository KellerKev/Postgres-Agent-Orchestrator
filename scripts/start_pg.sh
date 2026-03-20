#!/usr/bin/env bash
set -euo pipefail

PGDATA="$(pwd)/.pgdata"

if pg_isready -h localhost -p 5432 -q 2>/dev/null; then
  echo "[pg] PostgreSQL is already running"
else
  echo "[pg] Starting PostgreSQL..."
  pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" start -w
  echo "[pg] PostgreSQL started"
fi

# Create the database if it doesn't exist
if ! psql -h localhost -p 5432 -U agent -lqt | cut -d \| -f 1 | grep -qw agentdb; then
  createdb -h localhost -p 5432 -U agent agentdb
  echo "[pg] Created database 'agentdb'"
fi
