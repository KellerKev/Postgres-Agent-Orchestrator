#!/usr/bin/env bash
set -euo pipefail

PGDATA="$(pwd)/.pgdata"

if pg_isready -h localhost -p 5432 -q 2>/dev/null; then
  echo "[pg] Stopping PostgreSQL..."
  pg_ctl -D "$PGDATA" stop -m fast
  echo "[pg] Stopped"
else
  echo "[pg] PostgreSQL is not running"
fi
