#!/usr/bin/env bash
set -euo pipefail

PGDATA="$(pwd)/.pgdata"

if [ -d "$PGDATA" ]; then
  echo "[setup] .pgdata already exists, skipping initdb"
  exit 0
fi

echo "[setup] Initializing PostgreSQL data directory..."
initdb -D "$PGDATA" --username=agent --auth=trust --no-locale --encoding=UTF8

# Configure for local dev: listen on localhost, trust local connections
cat >> "$PGDATA/postgresql.conf" <<EOF
listen_addresses = 'localhost'
port = 5432
log_statement = 'all'
EOF

cat >> "$PGDATA/pg_hba.conf" <<EOF
# Allow local TCP connections without password for dev
host all all 127.0.0.1/32 trust
host all all ::1/128 trust
EOF

echo "[setup] Done. Run 'pixi run start-pg' to start the server."
