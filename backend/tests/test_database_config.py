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


def test_ensure_issue_queue_severity_column_adds_missing_column(monkeypatch):
    class DummyDialect:
        name = "postgresql"

    class DummyInspector:
        def get_columns(self, table_name, schema=None):
            assert table_name == "issue_queue"
            assert schema == "qc"
            return [{"name": "issue_id"}]

    class DummyConnection:
        def __init__(self):
            self.executed_sql = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def exec_driver_sql(self, sql):
            self.executed_sql.append(sql)

    class DummyEngine:
        def __init__(self, connection):
            self.dialect = DummyDialect()
            self.connection = connection

        def begin(self):
            return self.connection

    connection = DummyConnection()
    engine = DummyEngine(connection)
    monkeypatch.setattr(database_module, "inspect", lambda _connection: DummyInspector())

    database_module._ensure_issue_queue_severity_column(engine)

    assert connection.executed_sql == [
        "ALTER TABLE qc.issue_queue ADD COLUMN IF NOT EXISTS severity text NOT NULL DEFAULT 'medium'"
    ]
