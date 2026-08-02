from __future__ import annotations

import logging
from contextlib import contextmanager
from queue import Empty, LifoQueue
from threading import BoundedSemaphore
from typing import Iterator

import psycopg
from psycopg import pq
from psycopg.rows import dict_row

from backend.app.settings import Settings

logger = logging.getLogger(__name__)
_DB_POOLS: dict[str, LifoQueue[psycopg.Connection]] = {}
_DB_POOL_MAX_SIZE = 6
_DB_POOL_SLOTS: dict[str, BoundedSemaphore] = {}
_DB_POOL_ACQUIRE_TIMEOUT_SECONDS = 15
_DB_CONNECT_TIMEOUT_SECONDS = 10
_DB_STATEMENT_TIMEOUT_MS = 60_000
_DB_LOCK_TIMEOUT_MS = 5_000


PENDING_CHANGE_TABLE_STATEMENTS = (
    """
    CREATE SCHEMA IF NOT EXISTS qc
    """,
    """
    CREATE TABLE IF NOT EXISTS qc.pending_change (
        change_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        instrument_code text NOT NULL DEFAULT 'listing',
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pending_change_submission
        ON qc.pending_change (submission_key, change_status, requested_at DESC)
    """,
)

BHT_MONTHLY_SCHEMA_STATEMENTS = (
    """
    ALTER TABLE IF EXISTS raw.surveycto_submission
        ADD COLUMN IF NOT EXISTS form_id text
    """,
    """
    ALTER TABLE IF EXISTS raw.surveycto_submission
        ADD COLUMN IF NOT EXISTS formdef_version text
    """,
    """
    ALTER TABLE IF EXISTS raw.surveycto_submission
        ADD COLUMN IF NOT EXISTS survey_month text
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_raw_submission_month
        ON raw.surveycto_submission (instrument_code, survey_month, completion_date DESC)
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_form_version_month
        ON reference.form_version (instrument_code, survey_month, is_active)
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_question_version_scope
        ON reference.question_version (form_version_id, answer_scope, panel_code, section_prefix)
    """,
    """
    CREATE TABLE IF NOT EXISTS reference.panel_dictionary (
        panel_code text PRIMARY KEY,
        panel_label text,
        section_prefix text,
        sort_order integer,
        is_active boolean NOT NULL DEFAULT true,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS form_id text
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS formdef_version text
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS survey_month text
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS instance_id text
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS username text
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS city_code text
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS sector_code text
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS address text
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS gps_lat double precision
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS gps_long double precision
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS review_status text
    """,
    """
    ALTER TABLE IF EXISTS clean.main_case ADD COLUMN IF NOT EXISTS review_quality text
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_panel_month
        ON clean.main_case_panel (survey_month, panel_code, is_selected)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_panel_case_panel
        ON clean.main_case_panel (case_id, panel_code, is_selected)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_panel_panel_case
        ON clean.main_case_panel (panel_code, is_selected, case_id)
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_answer_variable_month
        ON clean.main_case_answer (variable_name, survey_month)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_answer_scope_panel
        ON clean.main_case_answer (answer_scope, panel_code, section_prefix, survey_month)
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_media_month
        ON clean.main_case_media (survey_month, media_type)
    """,
    """
    ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS survey_month text
    """,
    """
    ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS formdef_version text
    """,
    """
    ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS city_code text
    """,
    """
    ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS sector_code text
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_survey_month
        ON mart.main_case_dim (survey_month)
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.bht_monthly_kpi (
        survey_month text PRIMARY KEY,
        total_cases integer NOT NULL DEFAULT 0,
        complete_cases integer NOT NULL DEFAULT 0,
        reviewed_cases integer NOT NULL DEFAULT 0,
        approved_cases integer NOT NULL DEFAULT 0,
        rejected_cases integer NOT NULL DEFAULT 0,
        unique_interviewers integer NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.bht_panel_summary (
        survey_month text NOT NULL,
        panel_code text NOT NULL,
        panel_label text,
        case_count integer NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (survey_month, panel_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.bht_omnibus_summary (
        survey_month text NOT NULL,
        variable_name text NOT NULL,
        answer_value text NOT NULL,
        case_count integer NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (survey_month, variable_name, answer_value)
    )
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_bht_overview_distribution_lookup
        ON mart.bht_overview_distribution (category_slug, survey_month, distribution_key)
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_bht_category_kpi_lookup
        ON mart.bht_category_kpi (category_slug, survey_month)
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.main_verbatim_answer (
        case_id text NOT NULL REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
        submission_key text,
        survey_month text,
        category_slug text NOT NULL,
        category_label text NOT NULL,
        section_name text,
        variable_name text NOT NULL,
        question_label text,
        response_text text NOT NULL,
        theme text NOT NULL DEFAULT 'Other',
        region_label text,
        region_respondent_ordinal integer,
        interviewer_id text,
        start_time text,
        submitted_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (case_id, variable_name)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_verbatim_category_submitted
        ON mart.main_verbatim_answer (category_slug, submitted_at DESC, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_verbatim_variable_submitted
        ON mart.main_verbatim_answer (variable_name, submitted_at DESC, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_verbatim_theme
        ON mart.main_verbatim_answer (theme, category_slug)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_verbatim_response_fts
        ON mart.main_verbatim_answer USING gin (to_tsvector('simple', response_text))
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.main_case_queue (
        case_id text PRIMARY KEY,
        submission_key text,
        ea_id text,
        interviewer_id text,
        supervisor_id text,
        approval_stage text,
        submitted_at timestamptz,
        start_time text,
        updated_at timestamptz,
        ea_name text,
        lga_name text,
        state_name text,
        region_label text,
        region_respondent_ordinal integer,
        supacc_confirm text,
        slot_type text,
        username text,
        approved_by text,
        is_auto_approved boolean NOT NULL DEFAULT false,
        final_outcome_code text,
        section_count integer NOT NULL DEFAULT 0,
        open_issue_count integer NOT NULL DEFAULT 0,
        qc_flag_count integer NOT NULL DEFAULT 0,
        pending_change_count integer NOT NULL DEFAULT 0,
        has_callback_history boolean NOT NULL DEFAULT false,
        has_audio_history boolean NOT NULL DEFAULT false,
        callback_assigned_to_user_id text,
        callback_assigned_to_name text,
        audio_assigned_to_user_id text,
        audio_assigned_to_name text,
        selected_panel_codes text[] NOT NULL DEFAULT '{}'::text[],
        selected_panel_labels text,
        search_text text,
        updated_mart_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_queue_submitted
        ON mart.main_case_queue (submitted_at DESC, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_queue_submission_key
        ON mart.main_case_queue (submission_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_queue_stage_submitted
        ON mart.main_case_queue (approval_stage, submitted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_queue_region
        ON mart.main_case_queue (region_label, submitted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_queue_interviewer
        ON mart.main_case_queue (interviewer_id, submitted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_queue_panels
        ON mart.main_case_queue USING gin (selected_panel_codes)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_queue_search
        ON mart.main_case_queue USING gin (to_tsvector('simple', COALESCE(search_text, '')))
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.bht_case_overview_dim (
        case_id text PRIMARY KEY,
        submission_key text,
        survey_month text,
        submitted_at timestamptz,
        start_time text,
        approval_status text,
        category_slugs text[] NOT NULL DEFAULT ARRAY['omnibus']::text[],
        region_code text,
        region_label text,
        sector_code text,
        sector_label text,
        sec_value text,
        sec_label text,
        week_value text,
        week_label text,
        gender_value text,
        gender_label text,
        age_value text,
        age_label text,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.bht_map_point (
        case_id text PRIMARY KEY,
        submission_key text,
        survey_month text,
        ea_id text,
        interviewer_id text,
        username text,
        city_code text,
        sector_code text,
        gps_lat double precision,
        gps_long double precision,
        approval_status text,
        submitted_at timestamptz,
        start_time text,
        week_value text,
        record_city text,
        record_sector text
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.bht_map_point_category (
        category_slug text NOT NULL,
        case_id text NOT NULL,
        PRIMARY KEY (category_slug, case_id)
    )
    """,
    """
    ALTER TABLE IF EXISTS mart.bht_case_overview_dim
        ADD COLUMN IF NOT EXISTS start_time text
    """,
    """
    ALTER TABLE IF EXISTS mart.bht_map_point
        ADD COLUMN IF NOT EXISTS start_time text
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.enumerator_performance (
        enumerator_id text PRIMARY KEY,
        enumerator_name text,
        total_cases integer NOT NULL DEFAULT 0,
        approved_count integer NOT NULL DEFAULT 0,
        rejected_count integer NOT NULL DEFAULT 0,
        pending_count integer NOT NULL DEFAULT 0,
        consent_obtained integer NOT NULL DEFAULT 0,
        consent_refused integer NOT NULL DEFAULT 0,
        avg_duration_minutes numeric(10,2) NOT NULL DEFAULT 0,
        avg_sections_completed numeric(10,2) NOT NULL DEFAULT 0,
        open_issues integer NOT NULL DEFAULT 0,
        total_issues integer NOT NULL DEFAULT 0,
        rule_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_enumerator_performance_total_cases
        ON mart.enumerator_performance (total_cases DESC, enumerator_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.enumerator_productivity_by_date (
        enumerator_id text NOT NULL,
        date_key date NOT NULL,
        case_count integer NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (enumerator_id, date_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_enumerator_productivity_date
        ON mart.enumerator_productivity_by_date (date_key, enumerator_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.city_performance (
        city_id text PRIMARY KEY,
        city_name text,
        total_cases integer NOT NULL DEFAULT 0,
        approved_count integer NOT NULL DEFAULT 0,
        rejected_count integer NOT NULL DEFAULT 0,
        pending_count integer NOT NULL DEFAULT 0,
        consent_obtained integer NOT NULL DEFAULT 0,
        consent_refused integer NOT NULL DEFAULT 0,
        avg_duration_minutes numeric(10,2) NOT NULL DEFAULT 0,
        avg_sections_completed numeric(10,2) NOT NULL DEFAULT 0,
        open_issues integer NOT NULL DEFAULT 0,
        total_issues integer NOT NULL DEFAULT 0,
        rule_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_city_performance_total_cases
        ON mart.city_performance (total_cases DESC, city_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.city_productivity_by_date (
        city_id text NOT NULL,
        date_key date NOT NULL,
        case_count integer NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (city_id, date_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_city_productivity_date
        ON mart.city_productivity_by_date (date_key, city_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.callback_queue (
        callback_id uuid PRIMARY KEY,
        case_id text,
        submission_key text,
        case_label text,
        region_label text,
        interviewer_id text,
        assigned_to_user_id uuid,
        assigned_to_name text,
        outcome_code text,
        created_at timestamptz,
        updated_at timestamptz,
        search_text text,
        updated_mart_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_callback_queue_assignee_status
        ON mart.callback_queue (assigned_to_user_id, outcome_code, updated_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.audio_listening_queue (
        audio_id uuid PRIMARY KEY,
        case_id text,
        submission_key text,
        case_label text,
        region_label text,
        interviewer_id text,
        assigned_to_user_id text,
        assigned_to_name text,
        status text,
        created_at timestamptz,
        reviewed_at timestamptz,
        search_text text,
        updated_mart_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audio_queue_assignee_status
        ON mart.audio_listening_queue (assigned_to_user_id, status, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.accompaniment_interviewer (
        state_name text,
        interviewer_id text,
        total_interviews integer NOT NULL DEFAULT 0,
        accompanied_interviews integer NOT NULL DEFAULT 0,
        pct_accompanied numeric(7,2) NOT NULL DEFAULT 0,
        photo_count integer NOT NULL DEFAULT 0,
        status text,
        assigned_to text,
        assigned_to_user_id text,
        assigned_to_username text,
        latest_submitted_at timestamptz,
        latest_start_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (state_name, interviewer_id)
    )
    """,
    "ALTER TABLE IF EXISTS mart.accompaniment_interviewer ADD COLUMN IF NOT EXISTS photo_count integer NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mart.accompaniment_interviewer ADD COLUMN IF NOT EXISTS assigned_to_user_id text",
    "ALTER TABLE IF EXISTS mart.accompaniment_interviewer ADD COLUMN IF NOT EXISTS assigned_to_username text",
    "ALTER TABLE IF EXISTS mart.accompaniment_interviewer ADD COLUMN IF NOT EXISTS latest_submitted_at timestamptz",
    "ALTER TABLE IF EXISTS mart.accompaniment_interviewer ADD COLUMN IF NOT EXISTS latest_start_at timestamptz",
    """
    CREATE TABLE IF NOT EXISTS mart.accompaniment_photo (
        case_id text,
        submission_key text,
        case_label text,
        region_label text,
        interviewer_id text,
        start_time text,
        variable_name text,
        photo_role text,
        media_url text,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (case_id, variable_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.qc_productivity (
        queue text NOT NULL,
        username text NOT NULL,
        full_name text,
        total_pushed integer NOT NULL DEFAULT 0,
        completed integer NOT NULL DEFAULT 0,
        pending integer NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (queue, username)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.qc_productivity_by_date (
        queue text NOT NULL,
        username text NOT NULL,
        date_key date NOT NULL,
        case_count integer NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (queue, username, date_key)
    )
    """,
)

AUDIT_SCHEMA_PATCH_STATEMENTS = (
    """
    ALTER TABLE IF EXISTS audit.activity_log
        ADD COLUMN IF NOT EXISTS role text
    """,
    """
    ALTER TABLE IF EXISTS audit.activity_log
        ADD COLUMN IF NOT EXISTS success boolean NOT NULL DEFAULT true
    """,
    """
    ALTER TABLE IF EXISTS audit.activity_log
        ADD COLUMN IF NOT EXISTS before_value jsonb
    """,
    """
    ALTER TABLE IF EXISTS audit.activity_log
        ADD COLUMN IF NOT EXISTS after_value jsonb
    """,
    """
    ALTER TABLE IF EXISTS audit.activity_log
        ADD COLUMN IF NOT EXISTS error_message text
    """,
    """
    UPDATE audit.activity_log
    SET success = CASE WHEN lower(coalesce(status, 'success')) IN ('success', 'completed') THEN true ELSE false END
    WHERE success IS DISTINCT FROM CASE WHEN lower(coalesce(status, 'success')) IN ('success', 'completed') THEN true ELSE false END
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_activity_log_module
        ON audit.activity_log (module, occurred_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_activity_log_entity
        ON audit.activity_log (entity_type, entity_id, occurred_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_activity_log_success
        ON audit.activity_log (success, occurred_at DESC)
    """,
    """
    ALTER TABLE IF EXISTS qc.pending_change
        ADD COLUMN IF NOT EXISTS case_id text
    """,
    """
    ALTER TABLE IF EXISTS qc.pending_change
        ADD COLUMN IF NOT EXISTS issue_id uuid
    """,
    """
    ALTER TABLE IF EXISTS qc.pending_change
        ADD COLUMN IF NOT EXISTS requested_device_id text
    """,
    """
    ALTER TABLE IF EXISTS qc.pending_change
        ADD COLUMN IF NOT EXISTS reviewed_device_id text
    """,
    """
    ALTER TABLE IF EXISTS qc.case_status_history
        ADD COLUMN IF NOT EXISTS device_id text
    """,
    """
    ALTER TABLE IF EXISTS qc.data_change_log
        ADD COLUMN IF NOT EXISTS changed_by_device_id text
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pending_change_issue
        ON qc.pending_change (issue_id, requested_at DESC)
    """,
    # mart schema for dashboard aggregates (design doc Section 5)
    """
    CREATE SCHEMA IF NOT EXISTS mart
    """,
    # User credential columns
    """
    ALTER TABLE IF EXISTS app.user_account
        ADD COLUMN IF NOT EXISTS username text
    """,
    """
    ALTER TABLE IF EXISTS app.user_account
        ADD COLUMN IF NOT EXISTS password_hash text
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_account_username
        ON app.user_account (username) WHERE username IS NOT NULL
    """,
    """
    ALTER TABLE IF EXISTS app.user_account
        ALTER COLUMN email DROP NOT NULL
    """,
    """
    UPDATE app.user_account
    SET email = NULL
    WHERE email IS NOT NULL AND btrim(email) = ''
    """,
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM information_schema.table_constraints
            WHERE table_schema = 'app'
              AND table_name = 'user_account'
              AND constraint_name = 'user_account_email_key'
        ) THEN
            ALTER TABLE app.user_account DROP CONSTRAINT user_account_email_key;
        END IF;
    END $$;
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_account_email_nonblank
        ON app.user_account (lower(email))
        WHERE email IS NOT NULL AND btrim(email) <> ''
    """,
    # listing_case_status — design doc Section 3.1 dedicated status tracking table
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_listing_case_status_ea
        ON clean.listing_case_status (ea_id, current_status)
    """,
    # Back-fill listing_case_status from existing hh_listing_long rows
    """
    INSERT INTO clean.listing_case_status (submission_key, ea_id, boundary_id, current_status)
    SELECT DISTINCT ON (submission_key) submission_key, ea_id, boundary_id, approval_status
    FROM clean.hh_listing_long
    WHERE submission_key IS NOT NULL
    ON CONFLICT (submission_key) DO NOTHING
    """,
    # Ensure is_callback_required column exists on main_case (added after initial table creation)
    """
    ALTER TABLE clean.main_case
        ADD COLUMN IF NOT EXISTS is_callback_required boolean NOT NULL DEFAULT false
    """,
    # Ensure is_flagged column exists on main_case (for QC flagging)
    """
    ALTER TABLE clean.main_case
        ADD COLUMN IF NOT EXISTS is_flagged boolean NOT NULL DEFAULT false
    """,
    # callback_outcome — design doc Section 4.3 / qc_callback_outcome table
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_callback_outcome_case
        ON qc.callback_outcome (case_id, outcome_code)
    """,
    # main_case_roster — design doc Section 4.1 ms_roster_* tables for repeating modules
    """
    CREATE TABLE IF NOT EXISTS clean.main_case_roster (
        roster_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id     text NOT NULL REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
        roster_type text NOT NULL,
        row_no      integer NOT NULL DEFAULT 1,
        record      jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now(),
        UNIQUE (case_id, roster_type, row_no)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_roster_case
        ON clean.main_case_roster (case_id, roster_type)
    """,
    # Migrate existing multi-row section data into roster table
    """
    INSERT INTO clean.main_case_roster (case_id, roster_type, row_no, record, created_at, updated_at)
    SELECT case_id, section_name, row_no, record, created_at, updated_at
    FROM clean.main_case_section
    WHERE row_no > 1
    ON CONFLICT (case_id, roster_type, row_no) DO NOTHING
    """,
    """
    CREATE TABLE IF NOT EXISTS mart.main_case_dim (
        case_id text PRIMARY KEY REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
        submission_key text,
        approval_stage text,
        state_name text,
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
    )
    """,
    "ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS ea_id text",
    "ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS ea_name text",
    "ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS final_outcome_code text",
    "ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS slot_type text",
    "ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS supacc_confirm text",
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_state ON mart.main_case_dim (state_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_gender ON mart.main_case_dim (gender)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_gender_case
        ON mart.main_case_dim (gender, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_age_case
        ON mart.main_case_dim (age_group, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_sec_case
        ON mart.main_case_dim (sec_class, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_month ON mart.main_case_dim (interview_month)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_month_state_case
        ON mart.main_case_dim (interview_month, state_name, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_state_case
        ON mart.main_case_dim (state_name, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_state_ea
        ON mart.main_case_dim (state_name, ea_id, ea_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mart_main_dim_outcome_approval
        ON mart.main_case_dim (final_outcome_code, approval_stage, state_name, ea_id)
    """,
    # audio_listening table - for QC audio review assignments
    """
    CREATE SCHEMA IF NOT EXISTS clean
    """,
    """
    CREATE TABLE IF NOT EXISTS clean.audio_listening (
        audio_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id text NOT NULL,
        assigned_to_user_id text,
        assigned_to_role text,
        status text DEFAULT 'pending',
        quality_rating text,
        reviewer_note text,
        created_at timestamptz DEFAULT NOW(),
        reviewed_at timestamptz
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audio_listening_case ON clean.audio_listening(case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audio_listening_status ON clean.audio_listening(status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audio_listening_assigned ON clean.audio_listening(assigned_to_user_id, assigned_to_role)
    """,
    """
    ALTER TABLE IF EXISTS clean.audio_listening
        ADD COLUMN IF NOT EXISTS audio_url text
    """,
    # deleted_main_cases table - for soft delete tracking
    """
    CREATE TABLE IF NOT EXISTS clean.deleted_main_cases (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        submission_key text NOT NULL UNIQUE,
        case_id text,
        deleted_by text NOT NULL,
        deleted_at timestamptz DEFAULT NOW(),
        reason text
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deleted_main_cases_submission ON clean.deleted_main_cases(submission_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deleted_main_cases_case_id ON clean.deleted_main_cases(case_id)
    """,
    # deleted_listing_long table - for listing records where bld_last_another = 0 (treated as deleted)
    """
    CREATE TABLE IF NOT EXISTS clean.deleted_listing_long (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        submission_key text NOT NULL,
        caseid text,
        ea_id text,
        enumerator_id text,
        deleted_at timestamptz DEFAULT NOW(),
        deleted_by text DEFAULT 'system',
        reason text DEFAULT 'bld_last_another = 0',
        record jsonb
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deleted_listing_long_submission ON clean.deleted_listing_long(submission_key)
    """,
    # -----------------------------------------------------------------------
    # Performance indexes for the main survey review queue search.
    # The correlated subqueries on case_status_history were causing timeouts
    # because there was no index on (instrument_code, submission_key).
    # -----------------------------------------------------------------------
    """
    CREATE INDEX IF NOT EXISTS idx_case_status_history_main_join
        ON qc.case_status_history (instrument_code, submission_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_case_status_history_changed_by
        ON qc.case_status_history (changed_by_user_id)
    """,
    # GIN indexes on the main_case JSONB record for ILIKE search fields
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_record_username
        ON clean.main_case ((record->>'username'))
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_record_ea_name
        ON clean.main_case ((record->>'ea_name'))
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_record_slot_type
        ON clean.main_case ((record->>'slot_type'))
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_record_final_outcome
        ON clean.main_case ((record->>'final_outcome_code'))
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_final_outcome_norm
        ON clean.main_case ((lower(trim(coalesce(record->>'final_outcome_code', '')))))
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_approval_outcome
        ON clean.main_case (approval_stage, (lower(trim(coalesce(record->>'final_outcome_code', '')))), ea_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_state_ea_expr
        ON clean.main_case (
            (nullif(trim(record->>'state_name'), '')),
            ea_id,
            (nullif(trim(record->>'ea_name'), ''))
        )
    """,
    # Index on ea_id for EA-scoped queries
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_ea_id
        ON clean.main_case (ea_id)
    """,
    # Index on interviewer_id and supervisor_id for search
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_interviewer
        ON clean.main_case (interviewer_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_supervisor
        ON clean.main_case (supervisor_id)
    """,
    # Additional query-path indexes used across listing, main review, callback, audio, export, and audit screens.
    """
    CREATE INDEX IF NOT EXISTS idx_user_role_role_user
        ON app.user_role (role_code, user_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_raw_submission_instrument_key_completion
        ON raw.surveycto_submission (instrument_code, submission_key, completion_date DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_geo_boundaries_state_lga_ward
        ON reference.geo_boundaries_ea (state_name, lga_name, ward_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_geo_boundaries_ea_id
        ON reference.geo_boundaries_ea (ea_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_sampling_ea_status_completion
        ON clean.hh_sampling_ea (approval_status, completion_date DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_sampling_ea_ea_status
        ON clean.hh_sampling_ea (ea_id, approval_status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_sampling_ea_supervisor_status
        ON clean.hh_sampling_ea (supervisor_id, approval_status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_sampling_ea_interviewer
        ON clean.hh_sampling_ea (interviewer_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_listing_submission_row_type
        ON clean.hh_listing_long (submission_key, row_type)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_listing_submission_sample
        ON clean.hh_listing_long (submission_key, sample_flag)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_listing_status_submission
        ON clean.hh_listing_long (approval_status, submission_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_listing_ea_row_sample
        ON clean.hh_listing_long (ea_id, row_type, sample_flag)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_listing_selected_join
        ON clean.hh_listing_long (selected_join_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hh_listing_sample_case
        ON clean.hh_listing_long (sample_case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_issue_queue_listing_open_submission
        ON qc.issue_queue (submission_key, created_at DESC)
        WHERE instrument_code = 'listing' AND issue_status <> 'resolved'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_issue_queue_main_open_submission
        ON qc.issue_queue (submission_key, created_at DESC)
        WHERE instrument_code = 'main' AND issue_status <> 'resolved'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_issue_queue_assignee_status
        ON qc.issue_queue (assigned_to_user_id, issue_status, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_issue_queue_role_status
        ON qc.issue_queue (assigned_to_role, issue_status, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_issue_queue_status_created
        ON qc.issue_queue (issue_status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rule_result_status_created
        ON qc.rule_result (instrument_code, result_status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pending_change_status_requested
        ON qc.pending_change (instrument_code, change_status, requested_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pending_change_case_status
        ON qc.pending_change (case_id, change_status, requested_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pending_change_requested_by
        ON qc.pending_change (requested_by_user_id, requested_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_case_status_history_submission_changed
        ON qc.case_status_history (instrument_code, submission_key, changed_at DESC, status_history_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_case_status_history_case_changed
        ON qc.case_status_history (instrument_code, case_id, changed_at DESC, status_history_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_case_status_history_new_status_changed
        ON qc.case_status_history (instrument_code, new_status, changed_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_stage_submitted_case
        ON clean.main_case (approval_stage, submitted_at DESC, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_submitted_case
        ON clean.main_case (submitted_at DESC, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_gps_submitted
        ON clean.main_case (gps_lat, gps_long, submitted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_status_updated
        ON clean.main_case (current_status, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_feed_case
        ON clean.main_case (case_id, submission_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_data_change_log_submission_changed
        ON qc.data_change_log (submission_key, field_name, changed_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_data_error_audit_submission_cleaned
        ON clean.main_data_error_audit (submission_key, variable_name, cleaned_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_callback_stage
        ON clean.main_case (is_callback_required, approval_stage, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_record_gin
        ON clean.main_case USING gin (record)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_section_name_case
        ON clean.main_case_section (section_name, case_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_main_case_section_record_gin
        ON clean.main_case_section USING gin (record)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_callback_outcome_assignee_outcome
        ON qc.callback_outcome (assigned_to_user_id, outcome_code, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_callback_outcome_case_attempt
        ON qc.callback_outcome (case_id, attempt_no DESC, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_callback_outcome_sampled
        ON qc.callback_outcome (sampled_flag, outcome_code, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audio_listening_case_created
        ON clean.audio_listening (case_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audio_listening_assignee_status_created
        ON clean.audio_listening (assigned_to_user_id, status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_export_job_status_created
        ON export.export_job (job_status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_export_job_requested_created
        ON export.export_job (requested_by_user_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_file_catalog_active_generated
        ON export.file_catalog (instrument_code, export_profile, is_active, generated_at DESC)
    """,


)
@contextmanager
def db_connection(settings: Settings) -> Iterator[psycopg.Connection]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured.")

    pool = _DB_POOLS.setdefault(settings.database_url, LifoQueue(maxsize=_DB_POOL_MAX_SIZE))
    slots = _DB_POOL_SLOTS.setdefault(settings.database_url, BoundedSemaphore(_DB_POOL_MAX_SIZE))
    if not slots.acquire(timeout=_DB_POOL_ACQUIRE_TIMEOUT_SECONDS):
        raise RuntimeError("Database connection pool is busy. Please retry shortly.")

    conn: psycopg.Connection | None = None
    try:
        try:
            conn = pool.get_nowait()
            if conn.closed:
                conn = None
        except Empty:
            pass

        if conn is None:
            conn = psycopg.connect(
                settings.database_url,
                row_factory=dict_row,
                connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS,
                options=(
                    f"-c statement_timeout={_DB_STATEMENT_TIMEOUT_MS} "
                    f"-c lock_timeout={_DB_LOCK_TIMEOUT_MS}"
                ),
            )

        try:
            yield conn
        except Exception:
            try:
                if not conn.closed:
                    conn.rollback()
            finally:
                conn.close()
            raise
    finally:
        if conn is not None and not conn.closed:
            try:
                if conn.info.transaction_status != pq.TransactionStatus.IDLE:
                    conn.rollback()
                pool.put_nowait(conn)
            except Exception:
                conn.close()
        slots.release()


def _execute_bootstrap_statement(cur: psycopg.Cursor, statement: str, label: str) -> bool:
    cur.execute("SAVEPOINT bootstrap_stmt")
    try:
        cur.execute(statement)
        cur.execute("RELEASE SAVEPOINT bootstrap_stmt")
        return True
    except psycopg.Error as exc:
        cur.execute("ROLLBACK TO SAVEPOINT bootstrap_stmt")
        cur.execute("RELEASE SAVEPOINT bootstrap_stmt")
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate in {"40P01", "55P03", "57014"}:
            logger.warning("Skipping bootstrap statement due to transient database lock (%s): %s", label, exc)
            return False
        raise


def database_ready_for_startup(settings: Settings) -> bool:
    required_tables = (
        ("app", "user_account"),
        ("raw", "sync_state"),
        ("raw", "surveycto_submission"),
        ("clean", "hh_sampling_ea"),
        ("clean", "main_case"),
        ("clean", "main_case_answer"),
        ("clean", "main_case_panel"),
        ("reference", "form_version"),
        ("reference", "question_version"),
        ("reference", "panel_dictionary"),
        ("mart", "main_case_dim"),
        ("mart", "bht_monthly_kpi"),
        ("mart", "bht_panel_summary"),
        ("mart", "bht_omnibus_summary"),
        ("mart", "bht_overview_distribution"),
        ("mart", "bht_category_kpi"),
        ("qc", "rule_definition"),
        ("qc", "rule_result"),
        ("qc", "issue_queue"),
        ("qc", "case_status_history"),
        ("qc", "callback_outcome"),
    )
    required_columns = (
        ("raw", "surveycto_submission", "survey_month"),
        ("raw", "surveycto_submission", "form_id"),
        ("clean", "main_case", "survey_month"),
        ("clean", "main_case", "formdef_version"),
        ("mart", "main_case_dim", "ea_id"),
        ("mart", "main_case_dim", "ea_name"),
        ("mart", "main_case_dim", "final_outcome_code"),
        ("mart", "main_case_dim", "slot_type"),
        ("mart", "main_case_dim", "supacc_confirm"),
        ("mart", "main_case_dim", "survey_month"),
    )

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            for schema_name, table_name in required_tables:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = %s
                          AND table_name = %s
                    ) AS present
                    """,
                    (schema_name, table_name),
                )
                row = cur.fetchone() or {}
                if not bool(row.get("present")):
                    return False
            for schema_name, table_name, column_name in required_columns:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = %s
                          AND table_name = %s
                          AND column_name = %s
                    ) AS present
                    """,
                    (schema_name, table_name, column_name),
                )
                row = cur.fetchone() or {}
                if not bool(row.get("present")):
                    return False
    return True


def bootstrap_database(settings: Settings) -> None:
    from backend.app.auth import hash_password

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SELECT pg_try_advisory_lock(2025051301) AS acquired")
            lock_row = cur.fetchone() or {}
            if not bool(lock_row.get("acquired")):
                logger.info("Database bootstrap already running in another process; skipping this worker.")
                return
            try:
                # Run the full platform schema DDL first so all schemas and base
                # tables (including app.user_account) exist on a fresh database.
                schema_sql_path = settings.root_dir / "sql" / "platform_schema.sql"
                if schema_sql_path.exists():
                    _execute_bootstrap_statement(cur, schema_sql_path.read_text(encoding="utf-8"), "platform_schema.sql")
                for index, statement in enumerate(PENDING_CHANGE_TABLE_STATEMENTS, start=1):
                    _execute_bootstrap_statement(cur, statement, f"pending_change_patch_{index}")
                for index, statement in enumerate(BHT_MONTHLY_SCHEMA_STATEMENTS, start=1):
                    _execute_bootstrap_statement(cur, statement, f"bht_monthly_patch_{index}")
                for index, statement in enumerate(AUDIT_SCHEMA_PATCH_STATEMENTS, start=1):
                    _execute_bootstrap_statement(cur, statement, f"audit_patch_{index}")

                cur.execute(
                    """
                    INSERT INTO app.role (role_code, role_name)
                    VALUES
                        ('SUPERADMIN', 'SUPERADMIN'),
                        ('INICIO-ADMIN', 'INICIO-ADMIN'),
                        ('PDM-ADMIN', 'PDM-ADMIN'),
                        ('PDM-QC', 'PDM-QC')
                    ON CONFLICT (role_code) DO NOTHING
                    """
                )
                cur.execute(
                    """
                    INSERT INTO app.user_role (user_id, role_code)
                    SELECT
                        user_id,
                        CASE role_code
                            WHEN 'admin' THEN 'SUPERADMIN'
                            WHEN 'data_engineer' THEN 'PDM-ADMIN'
                            WHEN 'qc_reviewer' THEN 'PDM-QC'
                            WHEN 'supervisor' THEN 'PDM-ADMIN'
                            WHEN 'client' THEN 'INICIO-ADMIN'
                            WHEN 'INICIO-PM' THEN 'INICIO-ADMIN'
                            WHEN 'PDM-PM' THEN 'PDM-ADMIN'
                        END
                    FROM app.user_role
                    WHERE role_code IN ('admin', 'data_engineer', 'qc_reviewer', 'supervisor', 'client', 'INICIO-PM', 'PDM-PM')
                    ON CONFLICT (user_id, role_code) DO NOTHING
                    """
                )
                cur.execute(
                    """
                    DELETE FROM app.user_role
                    WHERE role_code IN ('admin', 'data_engineer', 'qc_reviewer', 'supervisor', 'client', 'INICIO-PM', 'PDM-PM')
                    """
                )
                cur.execute(
                    """
                    DELETE FROM app.role
                    WHERE role_code IN ('admin', 'data_engineer', 'qc_reviewer', 'supervisor', 'client', 'INICIO-PM', 'PDM-PM')
                    """
                )
                cur.execute(
                    """
                    UPDATE app.user_account
                    SET
                        full_name = replace(full_name, 'EF' || 'I' || 'nA', 'INICIO'),
                        email = replace(email, 'efina' || '.local', 'inicio.local')
                    WHERE full_name ILIKE '%' || ('EF' || 'I' || 'nA') || '%'
                       OR email ILIKE '%' || ('efina' || '.local') || '%'
                    """
                )

            # Seed the initial Superadmin user only when the database has no credentialed users.
                cur.execute("SELECT COUNT(*) AS cnt FROM app.user_account WHERE username IS NOT NULL")
                count = (cur.fetchone() or {}).get("cnt", 0)
                if count == 0:
                    hashed = hash_password(settings.admin_seed_password)
                    cur.execute(
                        """
                        INSERT INTO app.user_account (username, email, full_name, password_hash, is_active)
                        VALUES (%s, %s, %s, %s, true)
                        ON CONFLICT (username) DO UPDATE SET
                            email = EXCLUDED.email,
                            password_hash = EXCLUDED.password_hash,
                            full_name = EXCLUDED.full_name,
                            is_active = true
                        RETURNING user_id
                        """,
                        (
                            settings.admin_seed_username,
                            f"{settings.admin_seed_username}@inicio.local",
                            "Superadmin",
                            hashed,
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute(
                            """
                            INSERT INTO app.user_role (user_id, role_code)
                            VALUES (%s, 'SUPERADMIN')
                            ON CONFLICT (user_id, role_code) DO NOTHING
                            """,
                            (row["user_id"],),
                        )
                        print(f"Seeded Superadmin user '{settings.admin_seed_username}'.")

                cur.execute(
                    """
                    SELECT COUNT(*)::bigint AS c FROM information_schema.tables
                    WHERE table_schema = 'mart' AND table_name = 'main_case_dim'
                    """
                )
                if (cur.fetchone() or {}).get("c", 0):
                    cur.execute("SELECT COUNT(*)::int AS c FROM clean.main_case")
                    cc = (cur.fetchone() or {}).get("c", 0) or 0
                    if cc > 0:
                        cur.execute(
                            """
                            INSERT INTO mart.main_case_dim (
                                case_id, submission_key, approval_stage, state_name,
                                gender, age_group, sec_class, interview_month,
                                ea_id, ea_name, final_outcome_code, slot_type, supacc_confirm,
                                updated_at
                            )
                            SELECT
                                m.case_id,
                                m.submission_key,
                                m.approval_stage,
                                COALESCE(
                                    NULLIF(TRIM(m.record->>'state_name'), ''),
                                    NULLIF(TRIM(m.record->>'sd_STATE_NAME'), ''),
                                    NULLIF(TRIM(m.record->>'STATE_NA'), ''),
                                    NULLIF(TRIM(g.state_name), ''),
                                    NULLIF(TRIM(g.properties->>'sd_STATE_NAME'), ''),
                                    'Unknown'
                                ),
                                NULLIF(TRIM(ed.record->>'E1'), ''),
                                NULLIF(TRIM(ed.record->>'E_agegroup'), ''),
                                NULLIF(TRIM(ed.record->>'sec'), ''),
                                to_char(m.submitted_at AT TIME ZONE 'UTC', 'YYYY-MM'),
                                COALESCE(NULLIF(TRIM(m.ea_id), ''), NULLIF(TRIM(m.record->>'ea_id'), '')),
                                COALESCE(
                                    NULLIF(TRIM(m.record->>'ea_name'), ''),
                                    NULLIF(TRIM(m.record->>'sd_EA_NAME'), ''),
                                    NULLIF(TRIM(m.record->>'EA_NAME'), ''),
                                    NULLIF(TRIM(g.properties->>'sd_EA_NAME'), ''),
                                    NULLIF(TRIM(m.record->>'name'), ''),
                                    REGEXP_REPLACE(COALESCE(NULLIF(TRIM(m.ea_id), ''), NULLIF(TRIM(m.record->>'ea_id'), '')), '\\.0+$', '')
                                ),
                                TRIM(LOWER(COALESCE(m.record->>'final_outcome_code', ''))),
                                TRIM(LOWER(COALESCE(m.record->>'slot_type', ''))),
                                LOWER(COALESCE(NULLIF(TRIM(m.record->>'accomp'), ''), NULLIF(TRIM(m.record->>'supacc_confirm'), ''), '')),
                                now()
                            FROM clean.main_case m
                            LEFT JOIN reference.geo_boundaries_ea g
                                ON g.ea_id = REGEXP_REPLACE(COALESCE(NULLIF(TRIM(m.ea_id), ''), NULLIF(TRIM(m.record->>'ea_id'), '')), '\\.0+$', '')
                            LEFT JOIN LATERAL (
                                SELECT s.record
                                FROM clean.main_case_section s
                                WHERE s.case_id = m.case_id
                                  AND s.section_name = 'E. DEMOGRAPHICS'
                                  AND s.row_no = 1
                                LIMIT 1
                            ) ed ON true
                            ON CONFLICT (case_id) DO UPDATE SET
                                submission_key = EXCLUDED.submission_key,
                                approval_stage = EXCLUDED.approval_stage,
                                state_name = EXCLUDED.state_name,
                                gender = EXCLUDED.gender,
                                age_group = EXCLUDED.age_group,
                                sec_class = EXCLUDED.sec_class,
                                interview_month = EXCLUDED.interview_month,
                                ea_id = EXCLUDED.ea_id,
                                ea_name = EXCLUDED.ea_name,
                                final_outcome_code = EXCLUDED.final_outcome_code,
                                slot_type = EXCLUDED.slot_type,
                                supacc_confirm = EXCLUDED.supacc_confirm,
                                updated_at = now()
                            """
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                try:
                    cur.execute("SELECT pg_advisory_unlock(2025051301)")
                    conn.commit()
                except Exception:
                    conn.rollback()
