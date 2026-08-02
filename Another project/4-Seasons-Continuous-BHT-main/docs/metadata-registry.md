# SPSS Metadata Registry

This feature adds a controlled registry layer for SPSS-compatible Data Table Exports. It does not replace the four existing export actions or the workbook template.

## Runtime Flow

1. SurveyCTO/XLSForm metadata refresh runs through the existing sync process.
2. The backend ensures DuckDB registry tables exist.
3. Metadata detection compares the latest XLSForm metadata to active registry records.
4. New or changed brands/questions are written to `metadata_change_queue`.
5. Admin users review changes in `/admin/metadata-review`.
6. Approval writes versioned brand/question records, aliases, snapshots, and audit rows in one DuckDB transaction.
7. The registry generates an export-spec preview for approved active records.
8. Data Table Exports use the registry overlay to append approved non-seed brands/questions to the runtime SPSS export spec, then render through the existing SPSS workbook engine.
9. Export manifest records include registry diagnostics and AUTORECODE snapshot metadata.

## Registry Tables

- `brand_registry`
- `brand_aliases`
- `question_registry`
- `question_response_options`
- `metadata_change_queue`
- `registry_audit_log`
- `registry_snapshots`
- `spss_autorecode_mapping_versions`
- `spss_autorecode_mappings`
- `registry_schema_migrations`

Effective periods use `YYYY-MM`. `effective_from` is inclusive; `effective_to` is nullable and inclusive.

## Migration

Dry-run:

```sh
cd backend
npm run metadata:migrate:dry-run
```

Apply:

```sh
cd backend
npm run metadata:migrate:apply
```

The migration seeds registries from:

- `backend/data/export_table_specs.json`
- `backend/data/spss_export_rules.json`
- `backend/data/xlsform_metadata.json`

Dry-run writes a report and does not insert registry records.

## Admin Review

The review UI is available to existing admin users at:

```text
/admin/metadata-review
```

Mutation endpoints are protected by `ADMIN_REVIEW_TOKEN` when that environment variable is set. The frontend sends `VITE_ADMIN_REVIEW_TOKEN` as `x-admin-review-token`.

Recommended deployment variables:

```text
ADMIN_REVIEW_TOKEN=<shared secret for backend>
VITE_ADMIN_REVIEW_TOKEN=<same shared secret for frontend build>
```

## Diagnostics

Read-only diagnostics:

- `GET /api/admin/metadata/diagnostics`
- `GET /api/admin/metadata/registry/brands`
- `GET /api/admin/metadata/registry/questions`
- `GET /api/admin/metadata/autorecode`
- `POST /api/admin/metadata/autorecode/snapshot`
- `GET /api/admin/metadata/export-spec`
- `GET /api/admin/metadata/review`

Each generated export manifest record also includes `registryDiagnostics`, covering selected category, months, registry period, brand/question counts, and unresolved metadata warnings.

Each generated workbook also includes an `Export_Diagnostics` sheet. It records:

- Export type, scope, selected months, and display period.
- Category sheets generated for the workbook.
- Registry overlay decisions for approved non-seed brands/questions.
- Table row positions for percentage and count sheets.
- Registry brand/question universe counts and unresolved metadata warnings.
- AUTORECODE snapshot metadata used during the export.

`/api/admin/metadata/export-spec` returns the current registry-generated table universe. This is the bridge between approved registry records and full dynamic table generation.

`/api/admin/metadata/autorecode/snapshot` persists the currently available runtime `spss_autorecode_maps` into versioned `spss_autorecode_mapping_versions` and `spss_autorecode_mappings` rows.

## Review Actions

The admin UI supports:

- Approve as new brand/question.
- Add detected brand as alias to an existing brand.
- Merge detected brand with an existing brand.
- Link detected question to an existing question.
- Treat detected question as a replacement.
- Mark detected question as non-reportable.
- Reject detection.

Question approval now persists approved response options into `question_response_options`. Detected XLSForm choice lists are normalised into response options and reserved values such as `Other`, `Don't know`, `None`, and blanks are excluded from automatic approval candidates. Single-response, multiple-response, matrix, and ranking questions cannot be activated without approved response options.

## Export Overlay

Approved registry records now affect exports conservatively:

- Brand records whose source is not the initial `export_specs`/`spss_rules` seed are appended to existing brand-driven BAU/export blocks for their category when missing.
- Question records whose source is not the initial seed are appended as new standard SPSS table blocks when their variable is not already present in the static spec.
- Approved response options drive the rows for registry-generated question blocks.
- Blocks added from registry records omit fixed source rows, so the existing dynamic row placement puts them after prior tables.
- Seeded historical/static records are not re-applied as overlays, preventing the migration from expanding all legacy brand lists unexpectedly.
- Multi-month exports carry active-month metadata for generated brand, question, and response-option records. This allows diagnostics to show whether a row applies to all selected months or only part of a rolling/quarter window.

## Workbook Regression Comparison

Use the workbook comparison utility to validate no-change categories against a prior generated workbook:

```sh
cd backend
npm run workbook:compare -- --expected path/to/baseline.xlsx --actual path/to/new.xlsx --out ../output/workbook-comparison.json
```

The report compares:

- Sheet names and order.
- Hidden sheet state.
- Cell values, formulas, number formats, hyperlinks, and styles.
- Merged cells.
- Row heights and column widths.
- Sheet ranges.

## Current Limitations

- The existing SPSS export engine remains the renderer. Registry-generated table definitions now augment runtime specs, but full replacement of all static SPSS JSON definitions is still staged.
- Dynamic workbook row placement already comes from generated response rows in the current export engine and now records table row bounds in `Export_Diagnostics`; full registry-driven table generation should be expanded after the seeded registry has been reviewed.
- Exact numerical SPSS parity is not claimed unless tested against matching source data and an authoritative SPSS workbook.
