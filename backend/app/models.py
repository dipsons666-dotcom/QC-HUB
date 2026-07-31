import json
from datetime import datetime
from typing import Any
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
from sqlalchemy.orm import Mapped

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

    raw_submission_id: Mapped[str] = Column(String(36), primary_key=True, default=_uuid_default)
    instrument_code: Mapped[str] = Column(Text, nullable=False)
    form_id: Mapped[str | None] = Column(Text)
    formdef_version: Mapped[str | None] = Column(Text)
    survey_month: Mapped[str | None] = Column(Text)
    submission_key: Mapped[str] = Column(Text, nullable=False)
    submission_version: Mapped[int] = Column(Integer, nullable=False, default=1)
    submission_date: Mapped[datetime | None] = Column(DateTime(timezone=True))
    completion_date: Mapped[datetime | None] = Column(DateTime(timezone=True))
    interviewer_username: Mapped[str | None] = Column(Text)
    device_id: Mapped[str | None] = Column(Text)
    source_hash: Mapped[str] = Column(Text, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = Column(_json_type(), nullable=False)
    fetched_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ImportJob(Base):
    __tablename__ = "import_job"
    __table_args__ = _table_args("app")

    job_id: Mapped[str] = Column(String(36), primary_key=True, default=_uuid_default)
    submission_key: Mapped[str] = Column(Text, nullable=False)
    instrument_code: Mapped[str] = Column(Text, nullable=False)
    status: Mapped[str] = Column(Text, nullable=False, default="queued")
    queued_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed_at: Mapped[datetime | None] = Column(DateTime(timezone=True))
    transformed_at: Mapped[datetime | None] = Column(DateTime(timezone=True))


class MainCase(Base):
    __tablename__ = "main_case"
    __table_args__ = _table_args("clean")

    main_case_id: Mapped[str] = Column(String(36), primary_key=True, default=_uuid_default)
    submission_key: Mapped[str] = Column(Text, nullable=False, unique=True)
    case_id: Mapped[str] = Column(Text, nullable=False, unique=True)
    form_id: Mapped[str | None] = Column(Text)
    formdef_version: Mapped[str | None] = Column(Text)
    survey_month: Mapped[str | None] = Column(Text)
    instance_id: Mapped[str | None] = Column(Text)
    ea_id: Mapped[str | None] = Column(Text)
    interviewer_id: Mapped[str | None] = Column(Text)
    supervisor_id: Mapped[str | None] = Column(Text)
    username: Mapped[str | None] = Column(Text)
    city_code: Mapped[str | None] = Column(Text)
    sector_code: Mapped[str | None] = Column(Text)
    address: Mapped[str | None] = Column(Text)
    gps_lat: Mapped[float | None] = Column(Float)
    gps_long: Mapped[float | None] = Column(Float)
    review_status: Mapped[str | None] = Column(Text)
    review_quality: Mapped[str | None] = Column(Text)
    current_status: Mapped[str] = Column(Text, nullable=False, default="submitted")
    approval_stage: Mapped[str] = Column(Text, nullable=False, default="pending_review")
    submitted_at: Mapped[datetime | None] = Column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = Column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = Column(DateTime(timezone=True))
    is_callback_required: Mapped[bool] = Column(Boolean, nullable=False, default=False)
    record: Mapped[dict[str, Any]] = Column(_json_type(), nullable=False, default={})
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RuleDefinition(Base):
    __tablename__ = "rule_definition"
    __table_args__ = _table_args("qc")

    rule_code: Mapped[str] = Column(String(64), primary_key=True, default=lambda: f"rule-{uuid4().hex[:12]}")
    name: Mapped[str] = Column(Text, nullable=False)
    instrument_code: Mapped[str] = Column(Text, nullable=False)
    target_table: Mapped[str] = Column(Text, nullable=False)
    target_field: Mapped[str | None] = Column(Text)
    severity: Mapped[str] = Column(Text, nullable=False)
    rule_type: Mapped[str] = Column(Text, nullable=False, default="simple")
    description: Mapped[str] = Column(Text, nullable=False)
    operator: Mapped[str | None] = Column(Text)
    threshold: Mapped[float | None] = Column(Float)
    logic_sql: Mapped[str | None] = Column(Text)
    logic_python: Mapped[str | None] = Column(Text)
    recommended_action: Mapped[str | None] = Column(Text)
    is_active: Mapped[bool] = Column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RuleResult(Base):
    __tablename__ = "rule_result"
    __table_args__ = _table_args("qc")

    rule_result_id: Mapped[str] = Column(String(36), primary_key=True, default=_uuid_default)
    rule_code: Mapped[str] = Column(String(64), nullable=False)
    instrument_code: Mapped[str] = Column(Text, nullable=False)
    submission_key: Mapped[str | None] = Column(Text)
    case_id: Mapped[str | None] = Column(Text)
    table_name: Mapped[str] = Column(Text, nullable=False)
    row_identifier: Mapped[str | None] = Column(Text)
    field_name: Mapped[str | None] = Column(Text)
    severity: Mapped[str] = Column(Text, nullable=False)
    result_status: Mapped[str] = Column(Text, nullable=False, default="open")
    result_message: Mapped[str] = Column(Text, nullable=False)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class IssueQueue(Base):
    __tablename__ = "issue_queue"
    __table_args__ = _table_args("qc")

    issue_id: Mapped[str] = Column(String(36), primary_key=True, default=_uuid_default)
    rule_result_id: Mapped[str | None] = Column(String(36))
    instrument_code: Mapped[str] = Column(Text, nullable=False)
    submission_key: Mapped[str | None] = Column(Text)
    case_id: Mapped[str | None] = Column(Text)
    issue_status: Mapped[str] = Column(Text, nullable=False, default="pending_review")
    severity: Mapped[str] = Column(Text, nullable=False, default="medium")
    assigned_to_user_id: Mapped[str | None] = Column(String(36))
    assigned_to_role: Mapped[str | None] = Column(Text)
    issue_summary: Mapped[str] = Column(Text, nullable=False)
    resolution_note: Mapped[str | None] = Column(Text)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at: Mapped[datetime | None] = Column(DateTime(timezone=True))


class StaffMember(Base):
    __tablename__ = "staff_member"
    __table_args__ = _table_args("app")

    staff_id: Mapped[str] = Column(String(36), primary_key=True, default=_uuid_default)
    username: Mapped[str] = Column(Text, nullable=False)
    email: Mapped[str] = Column(Text, nullable=False, unique=True)
    role: Mapped[str] = Column(Text, nullable=False, default="reviewer")
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ActivityLog(Base):
    __tablename__ = "activity_log"
    __table_args__ = _table_args("audit")

    log_id: Mapped[str] = Column(String(36), primary_key=True, default=_uuid_default)
    occurred_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    user_id: Mapped[str | None] = Column(String(36))
    username: Mapped[str | None] = Column(Text)
    role: Mapped[str | None] = Column(Text)
    action: Mapped[str] = Column(Text, nullable=False)
    module: Mapped[str] = Column(Text, nullable=False)
    status: Mapped[str] = Column(Text, nullable=False, default="success")
    success: Mapped[bool] = Column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = Column(Text)
    entity_type: Mapped[str | None] = Column(Text)
    entity_id: Mapped[str | None] = Column(Text)
    before_value: Mapped[dict[str, Any] | None] = Column(_json_type())
    after_value: Mapped[dict[str, Any] | None] = Column(_json_type())
    metadata_payload: Mapped[dict[str, Any]] = Column("metadata", _json_type(), nullable=False, default={})
    error_message: Mapped[str | None] = Column(Text)
    device_id: Mapped[str | None] = Column(Text)
    client_ip: Mapped[str | None] = Column(Text)
