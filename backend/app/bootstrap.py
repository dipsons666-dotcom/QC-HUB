from pathlib import Path
from dotenv import load_dotenv

from .database import create_schemas, get_engine
from .models import Base


def bootstrap_database_schema() -> None:
    """Create required DB schemas and tables for the root backend."""
    create_schemas()
    Base.metadata.create_all(bind=get_engine())


def load_environment(env_path: Path | None = None) -> None:
    if env_path is None:
        env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)


if __name__ == "__main__":
    load_environment()
    bootstrap_database_schema()
    print("Database bootstrap completed.")
