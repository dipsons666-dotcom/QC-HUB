CREATE EXTENSION IF NOT EXISTS pgcrypto;
DO $$
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS postgis;
    EXCEPTION
        WHEN OTHERS THEN
            RAISE NOTICE 'PostGIS not available; continuing without spatial extension.';
    END;
END $$;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS clean;
CREATE SCHEMA IF NOT EXISTS qc;
CREATE SCHEMA IF NOT EXISTS reference;
CREATE SCHEMA IF NOT EXISTS export;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS app;

-- app tables must be created before any table that holds FK references to app.user_account
CREATE TABLE IF NOT EXISTS app.role (
    role_code text PRIMARY KEY,
    role_name text NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS app.user_account (
    user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text UNIQUE,
    email text,
    full_name text NOT NULL,
    password_hash text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_account_email_nonblank
    ON app.user_account (lower(email))
    WHERE email IS NOT NULL AND btrim(email) <> '';

CREATE TABLE IF NOT EXISTS app.user_role (
    user_id uuid NOT NULL REFERENCES app.user_account (user_id) ON DELETE CASCADE,
    role_code text NOT NULL REFERENCES app.role (role_code) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_code)
);

INSERT INTO app.role (role_code, role_name) VALUES
    ('SUPERADMIN', 'SUPERADMIN'),
    ('INICIO-ADMIN', 'INICIO-ADMIN'),
    ('PDM-ADMIN', 'PDM-ADMIN'),
    ('PDM-QC', 'PDM-QC')
ON CONFLICT (role_code) DO NOTHING;

CREATE TABLE IF NOT EXISTS raw.sync_state (
    instrument_code text PRIMARY KEY,
    last_successful_completion_utc timestamptz,
    last_successful_sync_at timestamptz,
    last_successful_fetch_utc timestamptz,
    last_run_started_at timestamptz,
    last_run_finished_at timestamptz,
    last_status text NOT NULL DEFAULT 'idle',
    last_message text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE IF EXISTS raw.sync_state
    ADD COLUMN IF NOT EXISTS last_successful_sync_at timestamptz;

ALTER TABLE IF EXISTS raw.sync_state
    ADD COLUMN IF NOT EXISTS last_successful_fetch_utc timestamptz;

CREATE TABLE IF NOT EXISTS raw.sync_control (
    control_key text PRIMARY KEY,
    manual_override_active boolean NOT NULL DEFAULT false,
    manual_override_token text,
    manual_override_requested_at timestamptz,
    manual_override_requested_by text,
    manual_override_note text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.surveycto_submission (
    raw_submission_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_code text NOT NULL,
    form_id text,
    formdef_version text,
    survey_month text,
    submission_key text NOT NULL,
    submission_version integer NOT NULL DEFAULT 1,
    submission_date timestamptz,
    completion_date timestamptz,
    interviewer_username text,
    device_id text,
    source_hash text NOT NULL,
    raw_payload jsonb NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instrument_code, submission_key, source_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_submission_instrument_completion
    ON raw.surveycto_submission (instrument_code, completion_date DESC);

CREATE INDEX IF NOT EXISTS idx_raw_submission_month
    ON raw.surveycto_submission (instrument_code, survey_month, completion_date DESC);

CREATE INDEX IF NOT EXISTS idx_raw_submission_payload
    ON raw.surveycto_submission USING gin (raw_payload);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis') THEN
        EXECUTE '
            CREATE TABLE IF NOT EXISTS reference.geo_boundaries_ea (
                ea_id text PRIMARY KEY,
                boundary_id text UNIQUE,
                state_code text,
                lga_code text,
                ward_code text,
                state_name text,
                lga_name text,
                ward_name text,
                geom geometry(MultiPolygon, 4326),
                centroid geometry(Point, 4326),
                properties jsonb NOT NULL DEFAULT ''{}''::jsonb,
                created_at timestamptz NOT NULL DEFAULT now()
            )';
    ELSE
        EXECUTE '
            CREATE TABLE IF NOT EXISTS reference.geo_boundaries_ea (
                ea_id text PRIMARY KEY,
                boundary_id text UNIQUE,
                state_code text,
                lga_code text,
                ward_code text,
                state_name text,
                lga_name text,
                ward_name text,
                geom_geojson jsonb,
                centroid_lat double precision,
                centroid_long double precision,
                properties jsonb NOT NULL DEFAULT ''{}''::jsonb,
                created_at timestamptz NOT NULL DEFAULT now()
            )';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS reference.xlsform_question (
    question_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_code text NOT NULL,
    section_name text,
    repeat_path text,
    variable_name text NOT NULL,
    question_type text,
    question_label text,
    choice_list_name text,
    export_table_name text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instrument_code, variable_name)
);

CREATE TABLE IF NOT EXISTS reference.xlsform_choice (
    choice_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_code text NOT NULL,
    list_name text NOT NULL,
    choice_code text NOT NULL,
    choice_label text,
    sort_order integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instrument_code, list_name, choice_code)
);

CREATE TABLE IF NOT EXISTS reference.other_specify_dictionary (
    dictionary_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_code text NOT NULL,
    variable_name text NOT NULL,
    normalized_text text NOT NULL,
    assigned_code text NOT NULL,
    assigned_label text NOT NULL,
    confidence numeric(5,4),
    decision_source text NOT NULL DEFAULT 'manual',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instrument_code, variable_name, normalized_text)
);

CREATE TABLE IF NOT EXISTS reference.form_version (
    form_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_code text NOT NULL DEFAULT 'main',
    form_id text NOT NULL,
    formdef_version text NOT NULL,
    survey_month text NOT NULL,
    xlsform_file_name text,
    xlsform_sha256 text,
    is_active boolean NOT NULL DEFAULT true,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (instrument_code, form_id, formdef_version, survey_month)
);

CREATE INDEX IF NOT EXISTS idx_form_version_month
    ON reference.form_version (instrument_code, survey_month, is_active);

CREATE TABLE IF NOT EXISTS reference.question_version (
    question_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    form_version_id uuid NOT NULL REFERENCES reference.form_version (form_version_id) ON DELETE CASCADE,
    variable_name text NOT NULL,
    section_prefix text,
    section_name text,
    panel_code text,
    answer_scope text NOT NULL DEFAULT 'common',
    question_type text,
    question_label text,
    choice_list_name text,
    sort_order integer,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (form_version_id, variable_name)
);

CREATE INDEX IF NOT EXISTS idx_question_version_scope
    ON reference.question_version (form_version_id, answer_scope, panel_code, section_prefix);

CREATE TABLE IF NOT EXISTS reference.panel_dictionary (
    panel_code text PRIMARY KEY,
    panel_label text,
    section_prefix text,
    sort_order integer,
    is_active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clean.hh_sampling_ea (
    sampling_ea_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_key text NOT NULL UNIQUE,
    ea_id text,
    boundary_id text,
    interviewer_id text,
    supervisor_id text,
    submission_date timestamptz,
    completion_date timestamptz,
    approval_status text NOT NULL DEFAULT 'submitted',
    record jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clean.hh_selected_long (
    selected_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_key text NOT NULL,
    selected_repeat_no integer NOT NULL,
    selected_join_key text,
    sample_case_id text,
    sample_case_label text,
    slot_type text,
    record jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (submission_key, selected_repeat_no)
);

CREATE TABLE IF NOT EXISTS clean.hh_listing_long (
    listing_row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_key text NOT NULL,
    ea_id text,
    boundary_id text,
    interviewer_id text,
    supervisor_id text,
    building_no integer,
    household_no_within_building integer,
    listing_join_key text,
    selected_join_key text,
    sample_case_id text,
    household_uid text,
    row_type text NOT NULL,
    sample_flag boolean NOT NULL DEFAULT false,
    gps_lat double precision,
    gps_long double precision,
    gps_source text,
    approval_status text NOT NULL DEFAULT 'submitted',
    record jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (submission_key, row_type, building_no, household_no_within_building)
);

CREATE INDEX IF NOT EXISTS idx_clean_listing_ea
    ON clean.hh_listing_long (ea_id, approval_status);

CREATE INDEX IF NOT EXISTS idx_clean_listing_join
    ON clean.hh_listing_long (listing_join_key);

CREATE TABLE IF NOT EXISTS clean.deleted_listing_rows (
    deleted_listing_row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_key text NOT NULL,
    row_type text,
    building_no integer,
    household_no_within_building integer,
    discarded_by text NOT NULL DEFAULT 'etl_filter',
    discard_reason text NOT NULL,
    record jsonb NOT NULL DEFAULT '{}'::jsonb,
    deleted_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_deleted_listing_rows_submission
    ON clean.deleted_listing_rows (submission_key);

CREATE TABLE IF NOT EXISTS clean.listing_case_status (
    status_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_key          text NOT NULL UNIQUE,
    ea_id                   text,
    boundary_id             text,
    current_status          text NOT NULL DEFAULT 'submitted',
    coverage_flag           boolean NOT NULL DEFAULT false,
    review_note             text,
    last_updated_by_user_id uuid REFERENCES app.user_account (user_id) ON DELETE SET NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_listing_case_status_ea
    ON clean.listing_case_status (ea_id, current_status);

CREATE TABLE IF NOT EXISTS clean.main_case (
    main_case_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_key text NOT NULL UNIQUE,
    case_id text NOT NULL UNIQUE,
    form_id text,
    formdef_version text,
    survey_month text,
    instance_id text,
    ea_id text,
    interviewer_id text,
    supervisor_id text,
    username text,
    city_code text,
    sector_code text,
    address text,
    gps_lat double precision,
    gps_long double precision,
    review_status text,
    review_quality text,
    current_status text NOT NULL DEFAULT 'submitted',
    approval_stage text NOT NULL DEFAULT 'pending_review',
    submitted_at timestamptz,
    reviewed_at timestamptz,
    approved_at timestamptz,
    is_callback_required boolean NOT NULL DEFAULT false,
    record jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clean.main_case_section (
    section_row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id text NOT NULL REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
    section_name text NOT NULL,
    row_no integer NOT NULL DEFAULT 1,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, section_name, row_no)
);

CREATE TABLE IF NOT EXISTS clean.main_data_error_audit (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_key text,
    case_id text,
    caseid text,
    variable_name text NOT NULL,
    old_value text,
    new_value text,
    check_flag text,
    imputation_flag text,
    reason text,
    cleaning_rule text,
    synced_at timestamptz,
    cleaned_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_main_data_error_audit_case
    ON clean.main_data_error_audit (case_id, variable_name);

CREATE INDEX IF NOT EXISTS idx_main_data_error_audit_synced
    ON clean.main_data_error_audit (synced_at);

CREATE TABLE IF NOT EXISTS qc.rule_definition (
    rule_code text PRIMARY KEY,
    name text NOT NULL,
    instrument_code text NOT NULL,
    target_table text NOT NULL,
    target_field text,
    severity text NOT NULL,
    rule_type text NOT NULL,
    description text NOT NULL,
    operator text,
    threshold double precision,
    logic_sql text,
    logic_python text,
    recommended_action text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS qc.rule_result (
    rule_result_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_code text NOT NULL REFERENCES qc.rule_definition (rule_code),
    instrument_code text NOT NULL,
    submission_key text,
    case_id text,
    table_name text NOT NULL,
    row_identifier text,
    field_name text,
    severity text NOT NULL,
    result_status text NOT NULL DEFAULT 'open',
    result_message text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_qc_rule_result_lookup
    ON qc.rule_result (instrument_code, submission_key, case_id, result_status);

CREATE TABLE IF NOT EXISTS qc.issue_queue (
    issue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_result_id uuid REFERENCES qc.rule_result (rule_result_id) ON DELETE SET NULL,
    instrument_code text NOT NULL,
    submission_key text,
    case_id text,
    issue_status text NOT NULL DEFAULT 'pending_review',
    assigned_to_user_id uuid,
    assigned_to_role text,
    issue_summary text NOT NULL,
    resolution_note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE TABLE IF NOT EXISTS qc.data_change_log (
    change_log_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_code text NOT NULL,
    submission_key text,
    case_id text,
    table_name text NOT NULL,
    row_identifier text,
    field_name text NOT NULL,
    old_value text,
    new_value text,
    changed_by_user_id uuid,
    change_reason text NOT NULL,
    issue_id uuid REFERENCES qc.issue_queue (issue_id) ON DELETE SET NULL,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS qc.case_status_history (
    status_history_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_code text NOT NULL,
    submission_key text,
    case_id text,
    previous_status text,
    new_status text NOT NULL,
    changed_by_user_id uuid,
    change_note text,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS qc.callback_outcome (
    callback_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id                 text NOT NULL REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
    sampled_flag            boolean NOT NULL DEFAULT false,
    attempt_no              integer NOT NULL DEFAULT 1,
    outcome_code            text NOT NULL DEFAULT 'pending',
    outcome_note            text,
    assigned_to_user_id     uuid REFERENCES app.user_account (user_id) ON DELETE SET NULL,
    completed_by_user_id    uuid REFERENCES app.user_account (user_id) ON DELETE SET NULL,
    completed_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_callback_outcome_case
    ON qc.callback_outcome (case_id, outcome_code);

CREATE TABLE IF NOT EXISTS clean.main_case_roster (
    roster_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id     text NOT NULL REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
    roster_type text NOT NULL,
    row_no      integer NOT NULL DEFAULT 1,
    record      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, roster_type, row_no)
);

CREATE INDEX IF NOT EXISTS idx_main_case_roster_case
    ON clean.main_case_roster (case_id, roster_type);

CREATE TABLE IF NOT EXISTS clean.main_case_panel (
    panel_row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id text NOT NULL REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
    survey_month text,
    formdef_version text,
    panel_code text NOT NULL,
    panel_label text,
    section_prefix text,
    is_selected boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, panel_code)
);

CREATE INDEX IF NOT EXISTS idx_main_case_panel_month
    ON clean.main_case_panel (survey_month, panel_code, is_selected);

CREATE TABLE IF NOT EXISTS clean.main_case_answer (
    answer_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id text NOT NULL REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
    submission_key text,
    survey_month text,
    formdef_version text,
    answer_scope text NOT NULL DEFAULT 'common',
    panel_code text,
    section_prefix text,
    variable_name text NOT NULL,
    value_text text,
    value_numeric numeric,
    value_boolean boolean,
    value_json jsonb,
    is_missing boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, variable_name)
);

CREATE INDEX IF NOT EXISTS idx_main_case_answer_variable_month
    ON clean.main_case_answer (variable_name, survey_month);
CREATE INDEX IF NOT EXISTS idx_main_case_answer_scope_panel
    ON clean.main_case_answer (answer_scope, panel_code, section_prefix, survey_month);

CREATE TABLE IF NOT EXISTS clean.main_case_media (
    media_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id text NOT NULL REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
    submission_key text,
    survey_month text,
    formdef_version text,
    variable_name text NOT NULL,
    media_type text,
    file_name text,
    surveycto_path text,
    proxy_path text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, variable_name)
);

CREATE INDEX IF NOT EXISTS idx_main_case_media_month
    ON clean.main_case_media (survey_month, media_type);

-- Denormalized case dimensions for overview / custom-table filters (refreshed after main ETL)
CREATE TABLE IF NOT EXISTS mart.main_case_dim (
    case_id text PRIMARY KEY REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
    submission_key text,
    survey_month text,
    formdef_version text,
    approval_stage text,
    state_name text,
    city_code text,
    sector_code text,
    gender text,
    age_group text,
    sec_class text,
    interview_month text,
    ea_id text,
    ea_name text,
    final_outcome_code text,
    slot_type text,
    supacc_confirm text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS ea_id text;
ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS ea_name text;
ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS final_outcome_code text;
ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS slot_type text;
ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS supacc_confirm text;
ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS survey_month text;
ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS formdef_version text;
ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS city_code text;
ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS sector_code text;

CREATE INDEX IF NOT EXISTS idx_mart_main_dim_state
    ON mart.main_case_dim (state_name);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_gender
    ON mart.main_case_dim (gender);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_gender_case
    ON mart.main_case_dim (gender, case_id);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_age_case
    ON mart.main_case_dim (age_group, case_id);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_sec_case
    ON mart.main_case_dim (sec_class, case_id);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_month
    ON mart.main_case_dim (interview_month);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_survey_month
    ON mart.main_case_dim (survey_month);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_month_state_case
    ON mart.main_case_dim (interview_month, state_name, case_id);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_state_case
    ON mart.main_case_dim (state_name, case_id);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_state_ea
    ON mart.main_case_dim (state_name, ea_id, ea_name);
CREATE INDEX IF NOT EXISTS idx_mart_main_dim_outcome_approval
    ON mart.main_case_dim (final_outcome_code, approval_stage, state_name, ea_id);

CREATE TABLE IF NOT EXISTS mart.bht_monthly_kpi (
    survey_month text PRIMARY KEY,
    total_cases integer NOT NULL DEFAULT 0,
    complete_cases integer NOT NULL DEFAULT 0,
    reviewed_cases integer NOT NULL DEFAULT 0,
    approved_cases integer NOT NULL DEFAULT 0,
    rejected_cases integer NOT NULL DEFAULT 0,
    unique_interviewers integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mart.bht_panel_summary (
    survey_month text NOT NULL,
    panel_code text NOT NULL,
    panel_label text,
    case_count integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (survey_month, panel_code)
);

CREATE TABLE IF NOT EXISTS mart.bht_omnibus_summary (
    survey_month text NOT NULL,
    variable_name text NOT NULL,
    answer_value text NOT NULL,
    case_count integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (survey_month, variable_name, answer_value)
);

CREATE TABLE IF NOT EXISTS mart.bht_overview_distribution (
    survey_month text NOT NULL,
    category_slug text NOT NULL,
    panel_code text,
    distribution_key text NOT NULL,
    distribution_title text NOT NULL,
    variable_name text NOT NULL,
    answer_value text NOT NULL,
    answer_label text NOT NULL,
    case_count integer NOT NULL DEFAULT 0,
    base_count integer NOT NULL DEFAULT 0,
    pct numeric(7,4) NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (survey_month, category_slug, distribution_key, answer_value)
);

CREATE INDEX IF NOT EXISTS idx_bht_overview_distribution_lookup
    ON mart.bht_overview_distribution (category_slug, survey_month, distribution_key);

CREATE TABLE IF NOT EXISTS mart.bht_category_kpi (
    survey_month text NOT NULL,
    category_slug text NOT NULL,
    panel_code text,
    total_case_count integer NOT NULL DEFAULT 0,
    category_case_count integer NOT NULL DEFAULT 0,
    omnibus_answer_count integer NOT NULL DEFAULT 0,
    media_file_count integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (survey_month, category_slug)
);

CREATE INDEX IF NOT EXISTS idx_bht_category_kpi_lookup
    ON mart.bht_category_kpi (category_slug, survey_month);

CREATE TABLE IF NOT EXISTS qc.pending_change (
    change_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_code text NOT NULL DEFAULT 'main',
    submission_key text,
    case_id text,
    table_name text NOT NULL,
    row_identifier text,
    field_name text NOT NULL,
    current_value text,
    proposed_value text,
    change_reason text NOT NULL,
    change_status text NOT NULL DEFAULT 'pending',
    issue_id uuid,
    requested_by_user_id uuid REFERENCES app.user_account (user_id) ON DELETE SET NULL,
    reviewed_by_user_id uuid REFERENCES app.user_account (user_id) ON DELETE SET NULL,
    requested_device_id text,
    reviewed_device_id text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz,
    review_note text
);

CREATE TABLE IF NOT EXISTS clean.audio_listening (
    audio_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id text NOT NULL,
    assigned_to_user_id text,
    assigned_to_role text,
    status text DEFAULT 'pending',
    quality_rating text,
    reviewer_note text,
    audio_url text,
    created_at timestamptz DEFAULT now(),
    reviewed_at timestamptz
);

CREATE TABLE IF NOT EXISTS export.export_job (
    export_job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_code text NOT NULL,
    export_profile text NOT NULL,
    export_format text NOT NULL,
    run_scope text NOT NULL DEFAULT 'approved_only',
    job_status text NOT NULL DEFAULT 'queued',
    requested_by_user_id uuid,
    started_at timestamptz,
    finished_at timestamptz,
    job_message text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS export.file_catalog (
    file_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    export_job_id uuid REFERENCES export.export_job (export_job_id) ON DELETE SET NULL,
    instrument_code text NOT NULL,
    export_profile text NOT NULL,
    export_format text NOT NULL,
    file_name text NOT NULL,
    file_path text NOT NULL,
    row_count bigint,
    byte_size bigint,
    generated_at timestamptz NOT NULL DEFAULT now(),
    is_active boolean NOT NULL DEFAULT true
);

-- app tables defined earlier in this file (before FK references to app.user_account)

CREATE TABLE IF NOT EXISTS qc.callback_verification_question (
    case_id         text NOT NULL REFERENCES clean.main_case(case_id) ON DELETE CASCADE,
    position        smallint NOT NULL CHECK (position BETWEEN 1 AND 5),
    section_name    text NOT NULL,
    variable_name   text NOT NULL,
    question_label  text NOT NULL,
    respondent_answer_label text NOT NULL,
    callback_answer text,
    is_correct      boolean,
    verified_at     timestamptz,
    PRIMARY KEY (case_id, position)
);


CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS audit.activity_log (
    log_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    user_id uuid REFERENCES app.user_account (user_id) ON DELETE SET NULL,
    username text,
    role text,
    action text NOT NULL,
    module text NOT NULL,
    status text NOT NULL DEFAULT 'success',
    success boolean NOT NULL DEFAULT true,
    description text,
    entity_type text,
    entity_id text,
    before_value jsonb,
    after_value jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    device_id text,
    client_ip text
);

ALTER TABLE IF EXISTS audit.activity_log
    ADD COLUMN IF NOT EXISTS role text;

ALTER TABLE IF EXISTS audit.activity_log
    ADD COLUMN IF NOT EXISTS success boolean NOT NULL DEFAULT true;

ALTER TABLE IF EXISTS audit.activity_log
    ADD COLUMN IF NOT EXISTS before_value jsonb;

ALTER TABLE IF EXISTS audit.activity_log
    ADD COLUMN IF NOT EXISTS after_value jsonb;

ALTER TABLE IF EXISTS audit.activity_log
    ADD COLUMN IF NOT EXISTS error_message text;

CREATE INDEX IF NOT EXISTS idx_activity_log_occurred_at ON audit.activity_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_user ON audit.activity_log (user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_action ON audit.activity_log (action, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_module ON audit.activity_log (module, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_entity ON audit.activity_log (entity_type, entity_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_success ON audit.activity_log (success, occurred_at DESC);


CREATE INDEX IF NOT EXISTS idx_main_case_review_stage_submitted
    ON clean.main_case (approval_stage, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_main_case_submission_case
    ON clean.main_case (submission_key, case_id);
CREATE INDEX IF NOT EXISTS idx_main_case_section_case_name
    ON clean.main_case_section (case_id, section_name);
CREATE INDEX IF NOT EXISTS idx_qc_issue_queue_main_join
    ON qc.issue_queue (instrument_code, submission_key, case_id, issue_status);
CREATE INDEX IF NOT EXISTS idx_qc_pending_change_main_join
    ON qc.pending_change (instrument_code, submission_key, case_id, change_status);
CREATE INDEX IF NOT EXISTS idx_callback_outcome_case_id
    ON qc.callback_outcome (case_id);
CREATE INDEX IF NOT EXISTS idx_audio_listening_case_id
    ON clean.audio_listening (case_id);

-- -----------------------------------------------------------------------
-- Query-path indexes
-- Keep these aligned with backend/app/database.py so fresh schema installs
-- and existing deployments benefit from the same planner improvements.
-- -----------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_user_role_role_user
    ON app.user_role (role_code, user_id);

CREATE INDEX IF NOT EXISTS idx_raw_submission_instrument_key_completion
    ON raw.surveycto_submission (instrument_code, submission_key, completion_date DESC);

CREATE INDEX IF NOT EXISTS idx_geo_boundaries_state_lga_ward
    ON reference.geo_boundaries_ea (state_name, lga_name, ward_name);
CREATE INDEX IF NOT EXISTS idx_geo_boundaries_ea_id
    ON reference.geo_boundaries_ea (ea_id);

CREATE INDEX IF NOT EXISTS idx_hh_sampling_ea_status_completion
    ON clean.hh_sampling_ea (approval_status, completion_date DESC);
CREATE INDEX IF NOT EXISTS idx_hh_sampling_ea_ea_status
    ON clean.hh_sampling_ea (ea_id, approval_status);
CREATE INDEX IF NOT EXISTS idx_hh_sampling_ea_supervisor_status
    ON clean.hh_sampling_ea (supervisor_id, approval_status);
CREATE INDEX IF NOT EXISTS idx_hh_sampling_ea_interviewer
    ON clean.hh_sampling_ea (interviewer_id);

CREATE INDEX IF NOT EXISTS idx_hh_listing_submission_row_type
    ON clean.hh_listing_long (submission_key, row_type);
CREATE INDEX IF NOT EXISTS idx_hh_listing_submission_sample
    ON clean.hh_listing_long (submission_key, sample_flag);
CREATE INDEX IF NOT EXISTS idx_hh_listing_status_submission
    ON clean.hh_listing_long (approval_status, submission_key);
CREATE INDEX IF NOT EXISTS idx_hh_listing_ea_row_sample
    ON clean.hh_listing_long (ea_id, row_type, sample_flag);
CREATE INDEX IF NOT EXISTS idx_hh_listing_selected_join
    ON clean.hh_listing_long (selected_join_key);
CREATE INDEX IF NOT EXISTS idx_hh_listing_sample_case
    ON clean.hh_listing_long (sample_case_id);

CREATE INDEX IF NOT EXISTS idx_issue_queue_listing_open_submission
    ON qc.issue_queue (submission_key, created_at DESC)
    WHERE instrument_code = 'listing' AND issue_status <> 'resolved';
CREATE INDEX IF NOT EXISTS idx_issue_queue_main_open_submission
    ON qc.issue_queue (submission_key, created_at DESC)
    WHERE instrument_code = 'main' AND issue_status <> 'resolved';
CREATE INDEX IF NOT EXISTS idx_issue_queue_assignee_status
    ON qc.issue_queue (assigned_to_user_id, issue_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_issue_queue_role_status
    ON qc.issue_queue (assigned_to_role, issue_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_issue_queue_status_created
    ON qc.issue_queue (issue_status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_rule_result_status_created
    ON qc.rule_result (instrument_code, result_status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pending_change_status_requested
    ON qc.pending_change (instrument_code, change_status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_pending_change_case_status
    ON qc.pending_change (case_id, change_status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_pending_change_requested_by
    ON qc.pending_change (requested_by_user_id, requested_at DESC);

CREATE INDEX IF NOT EXISTS idx_case_status_history_submission_changed
    ON qc.case_status_history (instrument_code, submission_key, changed_at DESC, status_history_id DESC);
CREATE INDEX IF NOT EXISTS idx_case_status_history_case_changed
    ON qc.case_status_history (instrument_code, case_id, changed_at DESC, status_history_id DESC);
CREATE INDEX IF NOT EXISTS idx_case_status_history_new_status_changed
    ON qc.case_status_history (instrument_code, new_status, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_main_case_stage_submitted_case
    ON clean.main_case (approval_stage, submitted_at DESC, case_id);
CREATE INDEX IF NOT EXISTS idx_main_case_status_updated
    ON clean.main_case (current_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_main_case_feed_case
    ON clean.main_case (case_id, submission_key);
CREATE INDEX IF NOT EXISTS idx_data_change_log_submission_changed
    ON qc.data_change_log (submission_key, field_name, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_main_data_error_audit_submission_cleaned
    ON clean.main_data_error_audit (submission_key, variable_name, cleaned_at DESC);
CREATE INDEX IF NOT EXISTS idx_main_case_callback_stage
    ON clean.main_case (is_callback_required, approval_stage, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_main_case_record_gin
    ON clean.main_case USING gin (record);
CREATE INDEX IF NOT EXISTS idx_main_case_final_outcome_norm
    ON clean.main_case ((lower(trim(coalesce(record->>'final_outcome_code', '')))));
CREATE INDEX IF NOT EXISTS idx_main_case_approval_outcome
    ON clean.main_case (approval_stage, (lower(trim(coalesce(record->>'final_outcome_code', '')))), ea_id);
CREATE INDEX IF NOT EXISTS idx_main_case_state_ea_expr
    ON clean.main_case (
        (nullif(trim(record->>'state_name'), '')),
        ea_id,
        (nullif(trim(record->>'ea_name'), ''))
    );

CREATE INDEX IF NOT EXISTS idx_main_case_section_name_case
    ON clean.main_case_section (section_name, case_id);
CREATE INDEX IF NOT EXISTS idx_main_case_section_record_gin
    ON clean.main_case_section USING gin (record);

CREATE INDEX IF NOT EXISTS idx_callback_outcome_assignee_outcome
    ON qc.callback_outcome (assigned_to_user_id, outcome_code, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_callback_outcome_case_attempt
    ON qc.callback_outcome (case_id, attempt_no DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_callback_outcome_sampled
    ON qc.callback_outcome (sampled_flag, outcome_code, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audio_listening_case_created
    ON clean.audio_listening (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audio_listening_assignee_status_created
    ON clean.audio_listening (assigned_to_user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_export_job_status_created
    ON export.export_job (job_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_export_job_requested_created
    ON export.export_job (requested_by_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_file_catalog_active_generated
    ON export.file_catalog (instrument_code, export_profile, is_active, generated_at DESC);
