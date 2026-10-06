# CMN-C2-277 - Unit tests: FreeeClient service (freee accounting REST API shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.freee_client import FreeeApiError, FreeeClient


def test_find_journal_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"manual_journal": {"id": "4021", "issue_date": "2026-07-01", "details": []}}

    client = FreeeClient("https://freee.example.test/api/1/", get=get)
    resp = client.find_journal("4021", "tok123")
    assert resp["manual_journal"]["id"] == "4021"
    assert captured["url"] == "https://freee.example.test/api/1/manual_journals/4021"
    # freee REST API auth: OAuth2 bearer token per call.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["journal_id"] == "4021"


def test_company_id_travels_as_query_param():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        return 200, {"manual_journal": {"id": "4021"}}

    client = FreeeClient("https://freee.example.test/api/1", company_id="777", get=get)
    client.find_journal("4021", "tok")
    # Live freee endpoints require company_id as a query parameter.
    assert captured["url"] == "https://freee.example.test/api/1/manual_journals/4021?company_id=777"


def test_create_journal_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"manual_journal": {"id": "9002"}}

    client = FreeeClient("https://freee.example.test/api/1", post=post)
    payload = {"manual_journal": {"details": [{"entry_side": "debit", "account_title": "supplies", "amount": 5000}]}}
    resp = client.create_journal(payload, "tok")
    assert resp["manual_journal"]["id"] == "9002"
    assert captured["url"] == "https://freee.example.test/api/1/manual_journals"
    assert captured["body"] == payload


def test_get_balance_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"trial_bs": {"balances": [{"account_item_name": "cash", "closing_balance": 5000}]}}

    client = FreeeClient("https://freee.example.test/api/1", get=get)
    resp = client.get_balance("cash", "tok")
    assert resp["trial_bs"]["balances"][0]["closing_balance"] == 5000
    assert captured["url"] == "https://freee.example.test/api/1/reports/trial_bs"
    assert captured["body"]["account"] == "cash"


def test_non_2xx_raises_freee_api_error():
    def post(url, headers, body):
        return 400, {"errors": ["manual_journal is malformed"]}

    client = FreeeClient("https://freee.example.test/api/1", post=post)
    with pytest.raises(FreeeApiError) as exc:
        client.create_journal({"manual_journal": {}}, "tok")
    assert exc.value.status_code == 400
    assert "manual_journal is malformed" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> deterministic, network-free stub.
    client = FreeeClient()
    assert client.uses_stub_transport is True
    resp = client.find_journal("4021", "tok")
    assert resp.get("_stub") is True
    journal = resp["manual_journal"]
    assert journal["id"] == "4021"
    assert isinstance(journal["details"], list)


def test_default_stub_transport_create_returns_synthetic_id():
    client = FreeeClient()
    resp = client.create_journal({"manual_journal": {"details": []}}, "tok")
    assert resp.get("_stub") is True
    assert resp["manual_journal"]["id"]
    assert resp["manual_journal"]["id"].isdigit()


def test_default_stub_transport_balance_shape():
    client = FreeeClient()
    resp = client.get_balance("cash", "tok")
    assert resp.get("_stub") is True
    row = resp["trial_bs"]["balances"][0]
    assert row["account_item_name"] == "cash"
    assert isinstance(row["closing_balance"], int)


def test_injected_transport_disables_stub_flag():
    client = FreeeClient(get=lambda url, headers, body: (200, {"manual_journal": {}}))
    assert client.uses_stub_transport is False
