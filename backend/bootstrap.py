from pathlib import Path
from dotenv import load_dotenv

from app.bootstrap import bootstrap_database_schema


def main() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
    bootstrap_database_schema()
    print("Database bootstrap completed.")


if __name__ == "__main__":
    main()
