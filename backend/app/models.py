import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON as JSONType

from .database import Base, get_engine


def _uuid_default() -> str:
    return str(uuid4())


def _json_type():
    return JSONB if get_engine().dialect.name == "postgresql" else JSONType


def _schema(schema_name: str) -> dict[str, str] | tuple[()]:
    if get_engine().dialect.name == "postgresql":
        return {"schema": schema_name}
    return {}


def _table_args(schema_name: str, *constraints):
    schema_kwargs = _schema(schema_name)
    return tuple(constraints + ({"extend_existing": True, **schema_kwargs},))


class RawSurveyCTOSubmission(Base):
    __tablename__ = "surveycto_submission"
    __table_args__ = _table_args("raw", UniqueConstraint("instrument_code", "submission_key", "source_hash"))

    raw_submission_id = Column(String(36), primary_key=True, default=_uuid_default)
    instrument_code = Column(Text, nullable=False)
    form_id = Column(Text)
    formdef_version = Column(Text)
    survey_month = Column(Text)
    submission_key = Column(Text, nullable=False)
    submission_version = Column(Integer, nullable=False, default=1)
    submission_date = Column(DateTime(timezone=True))
    completion_date = Column(DateTime(timezone=True))
    interviewer_username = Column(Text)
    device_id = Column(Text)
    source_hash = Column(Text, nullable=False)
    raw_payload = Column(_json_type(), nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ImportJob(Base):
    __tablename__ = "import_job"
    __table_args__ = _table_args("app")

    job_id = Column(String(36), primary_key=True, default=_uuid_default)
    submission_key = Column(Text, nullable=False)
    instrument_code = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="queued")
    queued_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed_at = Column(DateTime(timezone=True))
    transformed_at = Column(DateTime(timezone=True))


class MainCase(Base):
    __tablename__ = "main_case"
    __table_args__ = _table_args("clean")

    main_case_id = Column(String(36), primary_key=True, default=_uuid_default)
    submission_key = Column(Text, nullable=False, unique=True)
    case_id = Column(Text, nullable=False, unique=True)
    form_id = Column(Text)
    formdef_version = Column(Text)
    survey_month = Column(Text)
    instance_id = Column(Text)
    ea_id = Column(Text)
    interviewer_id = Column(Text)
    supervisor_id = Column(Text)
    username = Column(Text)
    city_code = Column(Text)
    sector_code = Column(Text)
    address = Column(Text)
    gps_lat = Column(Float)
    gps_long = Column(Float)
    review_status = Column(Text)
    review_quality = Column(Text)
    current_status = Column(Text, nullable=False, default="submitted")
    approval_stage = Column(Text, nullable=False, default="pending_review")
    submitted_at = Column(DateTime(timezone=True))
    reviewed_at = Column(DateTime(timezone=True))
    approved_at = Column(DateTime(timezone=True))
    is_callback_required = Column(Boolean, nullable=False, default=False)
    record = Column(_json_type(), nullable=False, default={})
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RuleDefinition(Base):
    __tablename__ = "rule_definition"
    __table_args__ = _table_args("qc")

    rule_code = Column(String(64), primary_key=True, default=lambda: f"rule-{uuid4().hex[:12]}")
    name = Column(Text, nullable=False)
    instrument_code = Column(Text, nullable=False)
    target_table = Column(Text, nullable=False)
    target_field = Column(Text)
    severity = Column(Text, nullable=False)
    rule_type = Column(Text, nullable=False, default="simple")
    description = Column(Text, nullable=False)
    operator = Column(Text)
    threshold = Column(Float)
    logic_sql = Column(Text)
    logic_python = Column(Text)
    recommended_action = Column(Text)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RuleResult(Base):
    __tablename__ = "rule_result"
    __table_args__ = _table_args("qc")

    rule_result_id = Column(String(36), primary_key=True, default=_uuid_default)
    rule_code = Column(String(64), nullable=False)
    instrument_code = Column(Text, nullable=False)
    submission_key = Column(Text)
    case_id = Column(Text)
    table_name = Column(Text, nullable=False)
    row_identifier = Column(Text)
    field_name = Column(Text)
    severity = Column(Text, nullable=False)
    result_status = Column(Text, nullable=False, default="open")
    result_message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class IssueQueue(Base):
    __tablename__ = "issue_queue"
    __table_args__ = _table_args("qc")

    issue_id = Column(String(36), primary_key=True, default=_uuid_default)
    rule_result_id = Column(String(36))
    instrument_code = Column(Text, nullable=False)
    submission_key = Column(Text)
    case_id = Column(Text)
    issue_status = Column(Text, nullable=False, default="pending_review")
    severity = Column(Text, nullable=False, default="medium")
    assigned_to_user_id = Column(String(36))
    assigned_to_role = Column(Text)
    issue_summary = Column(Text, nullable=False)
    resolution_note = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at = Column(DateTime(timezone=True))


class ActivityLog(Base):
    __tablename__ = "activity_log"
    __table_args__ = _table_args("audit")

    log_id = Column(String(36), primary_key=True, default=_uuid_default)
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    user_id = Column(String(36))
    username = Column(Text)
    role = Column(Text)
    action = Column(Text, nullable=False)
    module = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="success")
    success = Column(Boolean, nullable=False, default=True)
    description = Column(Text)
    entity_type = Column(Text)
    entity_id = Column(Text)
    before_value = Column(_json_type())
    after_value = Column(_json_type())
    metadata_payload = Column("metadata", _json_type(), nullable=False, default={})
    error_message = Column(Text)
    device_id = Column(Text)
    client_ip = Column(Text)
