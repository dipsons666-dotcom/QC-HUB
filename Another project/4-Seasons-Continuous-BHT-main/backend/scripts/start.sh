#!/bin/bash
set -euo pipefail

# Prepare persistent directories, then hand off to Node. SurveyCTO scheduling
# is owned by the Node process so DuckDB can be closed before Python starts.
mkdir -p /var/data
mkdir -p /var/data/{raw,state,staging,build,backups}

if [ ! -f "${DUCKDB_PATH:-/var/data/current.duckdb}" ] && [ -f "${DUCKDB_PATH:-/var/data/current.duckdb}.tmp" ]; then
  echo "[startup] recovering ${DUCKDB_PATH:-/var/data/current.duckdb} from leftover tmp file"
  mv "${DUCKDB_PATH:-/var/data/current.duckdb}.tmp" "${DUCKDB_PATH:-/var/data/current.duckdb}"
fi

exec node src/server.js
