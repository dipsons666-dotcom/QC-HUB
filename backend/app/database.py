import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, NoSuchTableError, ProgrammingError
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


def _load_env_files() -> None:
    current_dir = Path.cwd()
    project_root = Path(__file__).resolve().parent.parent

    for candidate in (current_dir / ".env", project_root / ".env"):
        if candidate.exists():
            load_dotenv(dotenv_path=candidate, override=False)

_engine = None
_SessionLocal = None


def get_database_url() -> str:
    _load_env_files()
    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        return database_url
    return "sqlite:///./qc_hub.db"


def get_engine():
    global _engine
    if _engine is None:
        database_url = get_database_url()
        _engine = create_engine(database_url, echo=False, future=True)
    return _engine


def get_session_local():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    return _SessionLocal


def get_db():
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_issue_queue_columns(engine) -> None:
    try:
        with engine.begin() as connection:
            inspector = inspect(connection)
            schema = "qc" if engine.dialect.name == "postgresql" else None
            table = "qc.issue_queue" if schema else "issue_queue"
            existing_columns = {column["name"] for column in inspector.get_columns("issue_queue", schema=schema)}
            if "severity" not in existing_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN severity text NOT NULL DEFAULT 'medium'"
                )
            if "assignment_remark" not in existing_columns:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN assignment_remark text")
            if engine.dialect.name != "postgresql":
                return
            rule_columns = {column["name"] for column in inspector.get_columns("rule_definition", schema="qc")}
            # Older deployed schemas predate these fields, while the API and
            # QC catalogue already require them.
            if "name" not in rule_columns:
                connection.exec_driver_sql("ALTER TABLE qc.rule_definition ADD COLUMN IF NOT EXISTS name text")
                connection.exec_driver_sql("UPDATE qc.rule_definition SET name = rule_code WHERE name IS NULL")
                connection.exec_driver_sql("ALTER TABLE qc.rule_definition ALTER COLUMN name SET NOT NULL")
            if "operator" not in rule_columns:
                connection.exec_driver_sql("ALTER TABLE qc.rule_definition ADD COLUMN IF NOT EXISTS operator text")
            if "threshold" not in rule_columns:
                connection.exec_driver_sql("ALTER TABLE qc.rule_definition ADD COLUMN IF NOT EXISTS threshold double precision")
            staff_columns = {column["name"] for column in inspector.get_columns("staff_member", schema="app")}
            if "password_hash" not in staff_columns:
                connection.exec_driver_sql("ALTER TABLE app.staff_member ADD COLUMN IF NOT EXISTS password_hash text NOT NULL DEFAULT ''")
            if "is_active" not in staff_columns:
                connection.exec_driver_sql("ALTER TABLE app.staff_member ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true")
    except (IntegrityError, NoSuchTableError, ProgrammingError) as exc:
        logging.warning("Unable to ensure issue queue columns exist: %s", exc)


def create_schemas():
    engine = get_engine()
    if os.getenv("SKIP_SCHEMA_CREATION", "false").strip().lower() in ("1", "true", "yes", "y"):
        return

    if engine.dialect.name == "postgresql":
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS raw")
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS clean")
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS qc")
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS app")
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS audit")
        except (IntegrityError, ProgrammingError):
            # Limited privilege users may not be allowed to create schemas,
            # and multiple workers may race to create the same schema.
            pass

    _ensure_issue_queue_columns(engine)
