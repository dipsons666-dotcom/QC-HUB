import os
from pathlib import Path

# Use a stable SQLite file for tests and ensure it is active before importing application modules.
base_path = Path(__file__).resolve().parent
database_path = base_path / ".pytest_db" / "test.db"
database_path.parent.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{database_path}"

from app.database import Base, get_engine


def pytest_configure(config):
    engine = get_engine()
    Base.metadata.create_all(engine)


def pytest_runtest_setup(item):
    engine = get_engine()
    Base.metadata.drop_all(engine, checkfirst=True)
    Base.metadata.create_all(engine)
