from app.main import fetch_submission_payloads


def test_fetch_submission_payloads_passes_surveycto_date_param(monkeypatch):
    captured = {}

    def fake_get(url, auth=None, params=None, timeout=30):
        captured["url"] = url
        captured["auth"] = auth
        captured["params"] = params
        captured["timeout"] = timeout

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return []

        return FakeResponse()

    monkeypatch.setenv("SURVEYCTO_SERVER", "inicio")
    monkeypatch.setenv("SURVEYCTO_USERNAME", "adesina.adeyemo@inicio-insights.com")
    monkeypatch.setenv("SURVEYCTO_PASSWORD", "Seun22ade#")
    monkeypatch.setenv("SURVEYCTO_MAIN_FORM_ID", "BHT_4_SEASONS_JULY_2026")
    monkeypatch.delenv("SURVEYCTO_DATE", raising=False)
    monkeypatch.setattr("app.main.requests.get", fake_get)

    result = fetch_submission_payloads()

    assert result == []
    assert captured["url"] == "https://inicio.surveycto.com/api/v2/forms/data/wide/json/BHT_4_SEASONS_JULY_2026"
    assert captured["auth"] == ("adesina.adeyemo@inicio-insights.com", "Seun22ade#")
    assert captured["params"]["date"]
    assert captured["params"]["date"].isdigit()
    assert len(captured["params"]["date"]) == 8
