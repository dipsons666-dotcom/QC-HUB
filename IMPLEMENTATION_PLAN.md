# QC Flags Application Implementation Plan

## Recommendation

Build the application as a data-driven QC workflow platform around an internal canonical schema, using the existing PostgreSQL design in [platform_schema.sql](platform_schema.sql) as a strong starting point.

Yes, MariaDB can be used instead of PostgreSQL, but the current schema would need to be adapted because it uses PostgreSQL-specific features such as JSONB, PostGIS, and PostgreSQL-style UUID helpers. For a first implementation, PostgreSQL is the easiest path. If MariaDB is required, the system should still be designed so that the application logic remains database-agnostic and only the persistence layer changes.

The QC Flags document describes an operational rules engine, not just a dashboard. The best implementation is:

1. Ingest raw SurveyCTO submissions into the raw schema.
2. Transform them into the clean schema.
3. Run QC rules from the qc schema and persist results.
4. Expose a review workflow for triage, issue resolution, and audit history.

## Why this approach fits the documentation

The document defines 37 operational flags with severity and threshold logic. That maps naturally to a rules engine with:

- rule definitions
- rule execution results
- issue queue management
- change/audit logging

The schema already provides the core tables for this flow:

- raw.surveycto_submission for source records
- clean.main_case and clean.hh_listing_long for transformed records
- qc.rule_definition and qc.rule_result for flag evaluation
- qc.issue_queue and qc.pending_change for review workflow
- audit.activity_log for traceability

## Recommended technical stack

- Backend: Python + FastAPI
- ORM: SQLAlchemy
- Database: PostgreSQL (recommended for the current schema) or MariaDB/MySQL (possible with schema adaptation)
- Frontend: React or simple server-rendered UI
- Background jobs: Celery or FastAPI background tasks
- Validation: Pydantic

## Proposed architecture

### Import API and SurveyCTO ingestion
- Expose a secure import endpoint on your QC platform that receives SurveyCTO data through an authenticated API.
- Store the original payload exactly as received in a raw import table before any transformation.
- Validate the payload, detect duplicates, and enqueue a background job for transformation and QC execution.
- Keep the ingestion path separate from the QC engine so imports remain fast and reliable.

This design supports the flow:
1. SurveyCTO API provides survey data.
2. Your import API receives and stores the data.
3. Transformation and QC rules run in the background.
4. Reviewers interact with the resulting QC findings through the platform.

### Database adaptation if MariaDB is chosen
If MariaDB is selected, the plan should include these changes:
- Replace PostgreSQL-specific JSONB usage with MariaDB JSON support.
- Replace PostGIS spatial features with a simpler spatial strategy or remove them for the first phase.
- Use UUID values stored as strings or BINARY(16) instead of PostgreSQL UUID helpers.
- Rework any PostgreSQL-only SQL syntax such as ON CONFLICT and generated UUID functions.

## Suggested module structure

### 1. Data ingestion layer
- Read SurveyCTO payloads into raw.surveycto_submission
- Normalize and map them into clean tables
- Store source hashes and submission metadata

### 2. QC rules engine
- Load rules from qc.rule_definition
- Evaluate rules for each batch or submission
- Persist outcomes to qc.rule_result
- Create queue items in qc.issue_queue when a rule fails

### 3. Review workflow
- Show flagged cases to reviewers
- Support status transitions: pending review, in progress, resolved, rejected
- Record changes through qc.pending_change and qc.data_change_log

### 4. Audit and security
- Log every action in audit.activity_log
- Enforce role-based access through app.user_account and app.user_role

## Suggested module structure

- app/api -> FastAPI endpoints
- app/services -> ingestion, QC evaluation, review workflow
- app/models -> SQLAlchemy models
- app/schemas -> Pydantic request/response schemas
- app/jobs -> scheduled or background rule execution
- app/ui -> frontend dashboard

## Implementation phases

### Phase 1: Foundation
- Create backend application skeleton
- Connect to PostgreSQL
- Load schema and verify migrations

### Phase 2: Ingestion & transformation
- Import raw submissions into the raw schema
- Populate clean.main_case and related tables

### Phase 3: QC engine
- Implement the Main Survey flags first
- Add household listing rules next
- Store results in qc.rule_result

### Phase 4: Review workflow
- Build issue queue and case details view
- Add change logging and approvals

### Phase 5: Dashboard & export
- Show summary KPIs and distribution views
- Support export jobs for prepared datasets

## Recommended development order

1. FastAPI app shell
2. Database connection and health checks
3. Rule-definition CRUD endpoints
4. Rule evaluation for the first 3 flags
5. Review queue UI
6. Expand to remaining flags

## Compatibility check with the recommended architecture principles

These recommendations are strongly compatible with the plan, and most of them are already aligned with the existing schema and proposed architecture.

### 1. Raw-first ingestion
This fits the current direction well. The plan should explicitly preserve raw payloads in the raw layer before any transformation into clean tables. The existing schema already supports this with raw.surveycto_submission and raw_payload.

### 2. Separate import service from QC engine
This should be a core design rule. Importing data and running QC should be separate workflows. The import service should validate and store data, while the QC engine should consume the transformed internal model in the background.

### 3. Incremental sync
This is a very good fit and should be implemented using a sync-state mechanism. The existing raw.sync_state table is already the right place to track progress, last successful runs, and batch timestamps.

### 4. Use source-platform UUIDs
The plan should preserve external identifiers from the survey platform such as submission IDs, case IDs, and form identifiers rather than depending only on local surrogate keys. This aligns well with the existing submission_key and case_id conventions.

### 5. Keep raw JSON
This is already strongly supported by the schema. The system should store the full original payload in addition to any extracted fields so that disputes and audits remain possible without re-requesting data from the source platform.

### 6. Build a QC scheduler
The platform should support scheduled and on-demand QC runs. This is important because thresholds may change over time and historical reprocessing should be possible. The scheduler can run as a background job layer in the backend.

### 7. Version QC rules
This should be added explicitly. Every QC rule execution should record which rule version was applied. This can be implemented by adding a version field to rule definitions or by storing a rule_version_id in each execution result.

### 8. Make QC independent of the source platform
This is one of the strongest recommendations and should be treated as a design principle. The system should not depend directly on SurveyCTO or any other platform. Instead, it should use an internal canonical model and a mapper layer that translates source payloads into that model.

### 9. Create project configuration
Every survey project should have configurable settings for rules, thresholds, workflow routing, and data mappings. The plan should include a project configuration layer so the platform can support multiple projects without hardcoding behavior.

### 10. Modular QC engine
The QC engine should be built as a set of independent modules, such as timing checks, duplication checks, GPS checks, and listing checks. This keeps the system extensible and makes it easier to add new QC rules without changing core logic.

### 11. Maintain interview states
The plan should explicitly model lifecycle states such as received, transformed, qc-pending, qc-reviewed, resolved, escalated, and archived. This is more useful than a simple flagged/not flagged model and fits the existing status fields in the schema.

### 12. Support reprocessing
This should be a first-class feature. The platform should allow reviewers or administrators to rerun QC for a case, batch, or project without re-importing raw data. This is especially important when logic changes or a bug is fixed.

### 13. Build a REST API for the platform
This is already aligned with the proposed FastAPI backend. The API should expose endpoints for ingestion, QC execution, review actions, dashboard summaries, and reporting.

### 14. Store QC results separately
This is already reflected in the proposed design. QC findings should live in the qc schema separate from the imported or cleaned case data so that source data remains intact and audit histories remain trustworthy.

### 15. Secure import API instead of direct database writes
This is an excellent recommendation and should be adopted as the ingestion strategy. The platform should expose a secure import endpoint that accepts submissions, validates them, logs the import, detects duplicates, and then hands off processing to the import service.

## High-impact enhancements to make the platform stand out

These additions would elevate the system from a basic QC tool into a polished, production-grade data quality platform.

### 1. Explainable QC results
- Every flag should show why it fired, the evidence behind it, and the recommended next action.
- Display thresholds, comparison values, and the relevant interview context for each issue.
- This makes the system more trustworthy for reviewers and supervisors.

### 2. Reviewer workbench
- Build a case-focused review screen showing:
  - the full case record
  - flagged fields and reasons
  - a timeline of actions and history
  - linked media or supporting evidence where available
- This improves reviewer speed and confidence.

### 3. Smart workflow automation
- Auto-route issues to the right role based on severity, type, or geography.
- Add SLA-based reminders for overdue reviews.
- Support status transitions such as pending, in progress, escalated, and resolved.

### 4. Batch analytics and trend monitoring
- Show flag rates by interviewer, supervisor, month, and geography.
- Highlight unusual spikes or drifting patterns across survey batches.
- This turns QC from a reactive review system into a proactive quality management tool.

### 5. Data quality scoring
- Create a simple quality score per case, interviewer, or batch.
- Use this to prioritize review workload and identify high-risk teams or regions.

### 6. Mobile-friendly and offline review support
- Make the review queue accessible on tablets or phones for supervisors in the field.
- Add lightweight offline support for remote review workflows.

### 7. AI-assisted review support
- Use AI to summarize why a case is flagged and suggest likely actions.
- Keep the reviewer in control by making AI suggestions optional and auditable.

### 8. Stronger audit and governance features
- Version every review decision and change.
- Provide exportable audit logs for supervisors, donors, or compliance needs.
- Add configurable approval rules for sensitive changes.

## Suggested roadmap for standout delivery

### Phase 6: Reviewer experience
- Build the reviewer dashboard and issue detail page
- Add case history, evidence, and recommended action views

### Phase 7: Analytics & intelligence
- Add batch trend dashboards and quality scorecards
- Introduce flag-rate monitoring and anomaly detection

### Phase 8: Operational excellence
- Add role-based routing, SLA reminders, and mobile-friendly review
- Prepare export and audit reporting features for governance

## Next step

The next implementation step should be a working backend that can:
- connect to the database
- expose health and rule-definition endpoints
- execute one simple QC rule end to end
