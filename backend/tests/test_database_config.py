import importlib

from app import database as database_module


def test_get_database_url_loads_dotenv_postgres_connection(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://qc_user:secret@db.example:5432/qc_hub\n",
        encoding="utf-8",
    )

    importlib.reload(database_module)

    assert database_module.get_database_url() == "postgresql://qc_user:secret@db.example:5432/qc_hub"
