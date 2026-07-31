import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
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


def create_schemas():
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS raw")
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS clean")
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS qc")
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS app")
                connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS audit")
        except IntegrityError:
            # Render may boot multiple workers at once. If another worker has already
            # created the named schemas, we should continue startup rather than crash.
            pass
    else:
        # SQLite and other dialects do not support named schemas the same way.
        pass
