# Market Insights Hub (Frontend + Backend)

This repository is now split into:

- `frontend/` - React dashboard UI (styled after your reference dashboard direction).
- `backend/` - Express API powered by DuckDB over your parquet folders.

## Data Source Defaults

Backend defaults are wired to your paths:

- `C:/Users/Adesina Adeyemo/Downloads/Restructured/Restructured/responses_long_parquet/*/*/*.parquet`
- `C:/Users/Adesina Adeyemo/Downloads/Restructured/Restructured/responses_base_parquet/*/*/*.parquet`

Override with environment variables if needed:

- `DATA_ROOT`
- `LONG_PARQUET_PATH`
- `BASE_PARQUET_PATH`
- `DUCKDB_PATH`
- `MARKET_DB_PATH`
- `BHT_DATAMAP_PATH`

On Render, set `DATA_ROOT=/var/data` and
`DUCKDB_PATH=/var/data/current.duckdb` on a paid web service
with a persistent disk mounted at `/var/data`.

If you upload the market insights snapshot separately, place it at
`/var/data/market_insights.duckdb` and keep `MARKET_DB_PATH` pointed there.
The backend keeps this file separate from `current.duckdb`; it attaches the
market DB read-only and serves both files through combined runtime views.

For the current SurveyCTO form, keep dashboard/database variables on the old
names and map incoming SurveyCTO columns back to them with `Datamap.xlsx`.
Locally, the sync script auto-detects `../Datamap.xlsx` from this repo layout.
On Render, upload the workbook to the disk and set
`BHT_DATAMAP_PATH=/var/data/Datamap.xlsx`.

The SurveyCTO-generated `current.duckdb` is built only from months after the
latest month in `MARKET_DB_PATH`. For example, if `market_insights.duckdb`
ends at `2026-07`, SurveyCTO data starts contributing at `2026-08`, and that
new month appears in the dashboard table and Month filter.

## SurveyCTO Hourly Sync

An hourly Render cron job calls the web service's protected sync endpoint. The
web service owns the persistent disk, fetches a full SurveyCTO snapshot,
reconciles additions, edits, and deletions by `KEY`, regenerates BHT parquet
staging, builds a replacement DuckDB file, then atomically promotes it after
validation. Historical months in `MARKET_DB_PATH` remain unchanged.

Required Render environment variables:

- `SURVEYCTO_SERVER`
- `SURVEYCTO_FORM_ID`
- `SURVEYCTO_USERNAME`
- `SURVEYCTO_PASSWORD`
- `DATA_ROOT=/var/data`
- `DUCKDB_PATH=/var/data/current.duckdb`
- `MARKET_DB_PATH=/var/data/market_insights.duckdb`
- `BHT_DATAMAP_PATH=/var/data/Datamap.xlsx`
- `BHT_METADATA_PATH=/opt/render/project/src/backend/data/Meta Data Rule.xlsx`
- `BHT_CURRENT_METADATA_PATH=/opt/render/project/src/backend/data/Meta Data Rule.xlsx`
- `BHT_RESPONDENT_ID_COLUMN=KEY`
- `SYNC_REPLACE_MONTHS=generated`
- `SURVEYCTO_FETCH_MODE=full`
- `SURVEYCTO_SCHEDULER_MODE=internal`
- `SYNC_INTERVAL_MS=3600000`
- `RUN_SYNC_ON_START=true`
- `SYNC_TRIGGER_TOKEN` (generated in the shared Render environment group)

The sync status endpoint is:

```sh
GET /api/sync/status
```

The Render cron service runs at `0 * * * *` and requires:

- `SYNC_TRIGGER_URL=https://<web-service-host>/api/sync/trigger`
- the shared `market-insights-sync-auth` environment group

Render cron services cannot access the web service's persistent disk directly,
so the cron only sends the authenticated trigger request.

Use `BHT_CURRENT_METADATA_PATH` when the live/current SurveyCTO form has a
different metadata rule workbook from historical months. The sync rebuilds the
generated post-history months with the current metadata and preserves historical
months from the seeded DuckDB.

Seed Render once by placing the existing historical DuckDB at
`/var/data/current.duckdb` after the disk-backed service is live.

For a manually configured Render web service, use the internal scheduler values
above. The backend closes DuckDB before launching the Python sync and briefly
returns HTTP 503 for analytics endpoints to keep the refresh below the web
instance's memory limit. A separately provisioned Render cron can still be used
by setting `SURVEYCTO_SCHEDULER_MODE=external` and configuring the shared trigger
token and URL.

## Run Backend

```sh
cd backend
npm install
$env:DUCKDB_PATH="$PWD/current.duckdb"
$env:MARKET_DB_PATH="C:/Users/Adesina Adeyemo/Videos/Inicio-Four-Seasons-main (7)/market_insights_1.duckdb"
$env:BHT_DATAMAP_PATH="C:/Users/Adesina Adeyemo/Videos/Inicio-Four-Seasons-main (7)/Datamap.xlsx"
npm run server
```

Backend starts on `http://localhost:4000`.
Available scripts: `server`, `dev`, `start`.

## Run Frontend

```sh
cd frontend
npm install
npm run dev
```

Frontend starts on `http://localhost:5173`.

If backend URL differs, set:

```sh
set VITE_API_BASE=http://localhost:4000
```

## Key API Endpoints

- `GET /api/meta/health`
- `GET /api/meta/schema`
- `GET /api/meta/options/:field`
- `POST /api/analytics/distribution`
- `POST /api/analytics/table`
