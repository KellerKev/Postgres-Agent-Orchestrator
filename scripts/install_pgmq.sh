#!/usr/bin/env bash
set -euo pipefail

PGMQ_DIR="$(pwd)/.pgmq-src"

# Check if pgmq is already installed
if psql -h localhost -p 5432 -U agent -d agentdb -c "SELECT 1 FROM pg_extension WHERE extname='pgmq'" -tA 2>/dev/null | grep -q 1; then
  echo "[pgmq] Already installed, skipping"
  exit 0
fi

echo "[pgmq] Installing pgmq extension from source..."

# Clone if needed
if [ ! -d "$PGMQ_DIR" ]; then
  git clone --depth 1 https://github.com/tembo-io/pgmq.git "$PGMQ_DIR"
fi

# Build and install using PGXS
cd "$PGMQ_DIR/pgmq-extension"
make
make install

# Create the extension in our database
psql -h localhost -p 5432 -U agent -d agentdb -c "CREATE EXTENSION IF NOT EXISTS pgmq CASCADE;"

echo "[pgmq] Installed successfully"
