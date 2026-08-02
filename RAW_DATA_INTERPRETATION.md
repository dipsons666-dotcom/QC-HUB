# Raw data interpretation guide

Location: next to [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)

Purpose
-------
This document explains how raw SurveyCTO/Survey platform payloads are turned into user-friendly fields in the Seasons-Continuous-BHT project, and how SPSS rules and other mappings are applied. Read this to replicate the same interpretation in QC-HUB: where to find mapping sources, how transforms run, and how the frontend consumes interpreted values.

Key files to inspect
--------------------
- Datamap & ingestion: [4-Seasons-Continuous-BHT-main/backend/scripts/surveycto_bht_sync.py](4-Seasons-Continuous-BHT-main/backend/scripts/surveycto_bht_sync.py)
- XLSForm metadata extraction: [4-Seasons-Continuous-BHT-main/backend/scripts/xlsform_metadata.py](4-Seasons-Continuous-BHT-main/backend/scripts/xlsform_metadata.py)
- Metadata JSON (choices/questions): [4-Seasons-Continuous-BHT-main/backend/data/xlsform_metadata.json](4-Seasons-Continuous-BHT-main/backend/data/xlsform_metadata.json)
- Metadata normalization & registry (Node): [4-Seasons-Continuous-BHT-main/backend/src/metadata-registry.js](4-Seasons-Continuous-BHT-main/backend/src/metadata-registry.js)
- Export normalization & runtime rules: [4-Seasons-Continuous-BHT-main/backend/src/server.js](4-Seasons-Continuous-BHT-main/backend/src/server.js)
- SPSS rules and export specs: [4-Seasons-Continuous-BHT-main/backend/data/spss_export_rules.json](4-Seasons-Continuous-BHT-main/backend/data/spss_export_rules.json) and [4-Seasons-Continuous-BHT-main/backend/data/export_table_specs.json](4-Seasons-Continuous-BHT-main/backend/data/export_table_specs.json)
- Frontend display helpers: [4-Seasons-Continuous-BHT-main/frontend/src/pages/MetadataReview.tsx](4-Seasons-Continuous-BHT-main/frontend/src/pages/MetadataReview.tsx), [4-Seasons-Continuous-BHT-main/frontend/src/pages/Dashboard.tsx](4-Seasons-Continuous-BHT-main/frontend/src/pages/Dashboard.tsx)

High-level flow (how raw → display happens)
-------------------------------------------
1. Ingest raw JSON rows
   - Raw payloads arrive from SurveyCTO (nested JSON). The ingestion script reads each submission and flattens keys into a tabular row.
   - See `surveycto_bht_sync.py` for flattening and initial parsing (date parsing helper functions are used here).

2. Load Datamap and XLSForm metadata
   - A Datamap workbook (user-provided) defines desired output column names, aliases, and special handling (e.g., multiselect parents).
   - XLSForm metadata (questions + choices) is extracted into JSON (via `xlsform_metadata.py`) and used as the authoritative map from coded values to human labels.

3. Apply Datamap transformations
   - The ingestion pipeline applies the datamap: rename raw keys → canonical output columns, drop unneeded vars, and expand multi-select parent columns into child boolean/text columns.
   - Look for functions named like `apply_datamap_columns` and `expand_datamap_multiselect_parent_columns` in `surveycto_bht_sync.py`.

4. Normalize cell values
   - For each cell, normalization functions map coded answers to labels using the XLSForm metadata, coerce numeric/text types, normalize booleans, and format date/GPS fields.
   - In the Node runtime, `metadata-registry.js` contains helpers `normalizeText`, `normalizeKey`, `responseOptionsForQuestion`, and `normalizeExportCellValue` that replicate this logic for exports and runtime API responses.

5. Apply SPSS/export rules and recodes
   - The SPSS rules JSON lists recodes, label sets, and numeric→label rules used for analytic exports. This ensures exported files use human-readable labels or numeric codes consistently.
   - The server-side export helpers (`server.js`) consult `spss_export_rules.json` and `export_table_specs.json` to generate labeled export columns and apply recode functions like `exportAnswerByNumericCode`.

6. Frontend consumption
   - The frontend does not re-derive labels; it receives normalized rows or uses small helper functions to format multi-selects and open-texts.
   - UI files (Dashboard, MetadataReview) call `displayValue`/formatters to render multi-select arrays, dates, and mapped labels.

Detailed concepts you must understand
------------------------------------
- Datamap: a mapping workbook mapping raw variable names to desired column names and indicating multi-select parents. Modify this to change how raw fields are exposed.
- XLSForm metadata: canonical mapping of question names and choice lists. Example entry: a question key `q1` with choices `{code: '1', label: 'Yes'}` used to replace raw `1` → `Yes`.
- Multi-select expansion: multi-selects often store a single parent string (space-separated or JSON array). The pipeline expands these into separate boolean/text columns for each choice.
- SPSS rules: recode tables that map numeric codes to labels and define missing or special values. These are applied at export time.
- Normalization: trimming, case normalization, date parsing (ISO/SurveyCTO formats), GPS parsing (latitude/longitude extraction), and JSON→string for object cells.

Step-by-step to reproduce the interpretation locally (example)
----------------------------------------------------------
1. Inspect the Datamap and XLSForm metadata files used by the Seasons project:
   - Open [4-Seasons-Continuous-BHT-main/backend/data/xlsform_metadata.json](4-Seasons-Continuous-BHT-main/backend/data/xlsform_metadata.json)
   - Open the Datamap workbook referenced by `surveycto_bht_sync.py` (path is configured inside that script).

2. Run the metadata extraction (if needed):
   - Run the Python script that generates the metadata JSON. Typical command (from the Seasons project root):

```bash
python backend/scripts/export_xlsform_metadata.py --survey-file path/to/survey.xlsx --out backend/data/xlsform_metadata.json
```

3. Run the ingestion/transform script on a sample raw payload file (or a single submission):

```bash
python backend/scripts/surveycto_bht_sync.py --input sample_raw.json --datamap path/to/datamap.xlsx --out transformed.csv
```

4. Inspect the transformed CSV: check that coded answers are replaced by labels, multi-selects are expanded, and date/gps fields are formatted.

How to change mappings for QC-HUB (practical guide)
--------------------------------------------------
1. Add or update XLSForm metadata
   - If you have the form definition (XLSForm), regenerate `xlsform_metadata.json` using the provided export script. This is the authoritative code→label map.

2. Update Datamap for desired output columns
   - Edit the Datamap workbook to rename raw keys, mark multi-select parents, and specify derived fields. Re-run the ingestion script.

3. Modify SPSS/expo rt rules if you need different recodes
   - Edit [4-Seasons-Continuous-BHT-main/backend/data/spss_export_rules.json](4-Seasons-Continuous-BHT-main/backend/data/spss_export_rules.json) to change label sets or numeric recodes used for analytics exports.

4. Tweak normalization logic (if edge-cases):
   - Edit the normalization helpers in `surveycto_bht_sync.py` (Python) or `metadata-registry.js` (Node) when you need a custom parse or label rule (e.g., special date formats or GPS parsing logic).

Common pitfalls & troubleshooting
--------------------------------
- If labels don’t appear: confirm the variable name in the transformed CSV matches the question key in `xlsform_metadata.json`.
- If multi-selects are empty: check datamap marks the parent variable correctly and confirm the ingestion expansion function matches your multi-select format.
- If exports show numeric codes: confirm SPSS rules cover that question or that the frontend export path calls the normalization helper.

Where to ask for help inside the codebase
----------------------------------------
- For datamap logic: review [4-Seasons-Continuous-BHT-main/backend/scripts/surveycto_bht_sync.py](4-Seasons-Continuous-BHT-main/backend/scripts/surveycto_bht_sync.py)
- For choice/label lookups: review [4-Seasons-Continuous-BHT-main/backend/scripts/xlsform_metadata.py](4-Seasons-Continuous-BHT-main/backend/scripts/xlsform_metadata.py)
- For export/recoding rules: review [4-Seasons-Continuous-BHT-main/backend/data/spss_export_rules.json](4-Seasons-Continuous-BHT-main/backend/data/spss_export_rules.json) and [4-Seasons-Continuous-BHT-main/backend/src/server.js](4-Seasons-Continuous-BHT-main/backend/src/server.js)

Next steps I can take (pick one)
--------------------------------
- Extract the exact code snippets for multiselect expansion, XLSForm lookup, and SPSS recodes and comment them inline.
- Prototype a small adapter in QC-HUB that reuses the Seasons project's XLSForm metadata JSON to label raw rows.
- Run a sample raw JSON through the Seasons ingestion pipeline and produce a labeled CSV for inspection.

---
Created to sit beside [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)


**Exact mapping logic (QC-HUB implementation)**

This project includes a small mapping helper that re-uses the Seasons project's `xlsform_metadata.json` to convert coded answers into human labels. The behaviour implemented in `backend/app/main.py` is summarized below:

- At startup the server attempts to load `4-Seasons-Continuous-BHT-main/backend/data/xlsform_metadata.json`.
- A lightweight alias map is built from each question's `aliases` (if present) and the question name itself so lookups tolerate common aliasing.
- For each flattened payload cell the mapper does:
  - Look up the question metadata by key (or by upper-cased key as fallback).
  - If the question references a `list_name` (choice list), consult `lists[list_name]` to translate codes to labels.
  - For `select_multiple` questions: accept a JSON array or a whitespace-separated string of choice keys; map each token to a label (tries the raw token, then a float-string form like `1.0`), and join with `, `.
  - For `select_one` questions: try the raw token, then a float-like token, and return the matching label if found; otherwise return the original value.

Example (conceptual) Python-like pseudocode:

```python
def map_cell_value(key, value):
   # load metadata once
   meta = alias_map.get(key) or alias_map.get(key.upper())
   if not meta or not meta.get('list_name'):
      return value
   choices = metadata['lists'].get(meta['list_name'], {})
   if meta.get('kind') == 'select_multiple':
      tokens = value if isinstance(value, list) else str(value).split()
      labels = [choices.get(t) or choices.get(str(float(t))) or t for t in tokens]
      return ', '.join(labels)
   if meta.get('kind') == 'select_one':
      return choices.get(value) or choices.get(str(float(value))) or value
   return value
```

Notes and caveats:
- The mapper is intentionally permissive: it attempts a float-style key transformation (e.g. `'1' -> '1.0'`) because some exported choice keys are stored with decimal formatting in the Seasons metadata.
- If no metadata is available on the server, the mapper is a no-op and original values are returned.
- This helper is suitable for presenting respondent-facing labels in the admin UI; analytics-grade recoding (SPSS/export rules) should still use the `spss_export_rules.json` pipeline for consistent numeric coding.

If you want, I can also extract the exact Seasons helper snippets (multiselect expansion, alias resolution, and SPSS recode application) and include them verbatim with line references from the Seasons repo.
