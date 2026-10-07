"""`integrations/scm/http.py`: the shared httpx wrapper — retry wiring, error mapping
without body leakage, JSON/text helpers, and the pagination ceiling."""

from __future__ import annotations

import logging

import httpx
import pytest

from breezeai_cog.config import Settings
from breezeai_cog.integrations.scm.errors import SCMAPIError
from breezeai_cog.integrations.scm.http import SCMHttpClient

_SECRET_BODY = '{"message": "Bad credentials for token ghp_SECRET123"}'


def _client(handler, **settings_overrides) -> SCMHttpClient:
    settings = Settings(_env_file=None, scm_api_retry_backoff_seconds=0, **settings_overrides)
    return SCMHttpClient(
        provider="Test",
        base_url="https://api.example/v1/",
        headers={"Authorization": "Bearer t"},
        settings=settings,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


def test_path_is_joined_to_base_and_full_url_is_used_as_is() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, json={})

    c = _client(handler)
    c.get("/repos/x")
    c.get("repos/y", {"a": "1"})
    c.get("https://other.example/next?page=2")
    assert seen == [
        "https://api.example/v1/repos/x",
        "https://api.example/v1/repos/y?a=1",
        "https://other.example/next?page=2",
    ]


def test_default_headers_sent_and_per_request_headers_merged() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer t"
        assert req.headers["Accept"] == "text/plain"
        return httpx.Response(200, text="raw")

    assert _client(handler).get_text("/f", headers={"Accept": "text/plain"}) == "raw"


def test_transient_failure_is_retried_via_shared_helper() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": 1})

    assert _client(handler).get_json("/x") == {"ok": 1}
    assert calls["n"] == 3


def test_error_maps_to_scm_api_error_without_body(caplog: pytest.LogCaptureFixture) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=_SECRET_BODY)

    with caplog.at_level(logging.DEBUG, logger="breezeai_cog.integrations.scm.http"):
        with pytest.raises(SCMAPIError) as info:
            _client(handler).get_json("/x")
    err = info.value
    assert err.status_code == 502 and err.http_status == 401
    assert "ghp_SECRET123" not in str(err)
    assert "HTTP 401" in str(err) and "credential was rejected" in str(err)
    assert err.__cause__ is None  # no chained httpx error carrying the response
    assert any("ghp_SECRET123" in r.getMessage() for r in caplog.records)  # DEBUG only


def test_transport_error_maps_to_scm_api_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(SCMAPIError) as info:
        _client(handler, scm_api_retry_max=0).get("/x")
    assert info.value.http_status is None and "ConnectError" in str(info.value)


def test_non_json_body_is_an_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    with pytest.raises(SCMAPIError, match="non-JSON"):
        _client(handler).get_json("/x")


def test_paginate_follows_next_until_none() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        body = {"values": [page], "next": f"https://api.example/v1/p?page={page + 1}" if page < 3 else None}
        return httpx.Response(200, json=body)

    c = _client(handler)
    pages = list(c.paginate("/p", lambda r: r.json().get("next")))
    assert [r.json()["values"] for r in pages] == [[1], [2], [3]]


def test_paginate_stops_at_cap_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        return httpx.Response(200, json={"next": f"https://api.example/v1/p?page={page + 1}"})

    c = _client(handler, scm_max_pages=4)
    with caplog.at_level(logging.WARNING, logger="breezeai_cog.integrations.scm.http"):
        pages = list(c.paginate("/p", lambda r: r.json()["next"]))
    assert len(pages) == 4
    assert any("breezeai_scm_pagination_capped" in r.getMessage() for r in caplog.records)


def test_first_page_params_are_not_repeated_on_next_urls() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        nxt = "https://api.example/v1/p?cursor=abc" if len(seen) == 1 else None
        return httpx.Response(200, json={"next": nxt})

    list(_client(handler).paginate("/p", lambda r: r.json()["next"], params={"pagelen": 100}))
    assert seen == ["https://api.example/v1/p?pagelen=100", "https://api.example/v1/p?cursor=abc"]


def test_close_is_idempotent() -> None:
    c = _client(lambda _r: httpx.Response(200, json={}))
    c.get("/x")
    c.close()
    c.close()
    c.get("/x")  # reopens
    c.close()


def test_post_json_sends_body_and_decodes_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"id": 9})

    assert _client(handler).post_json("/x", {"body": "hi"}, {"api-version": "7.1"}) == {"id": 9}
    assert seen[0].method == "POST" and seen[0].url.params["api-version"] == "7.1"
    assert seen[0].headers["Content-Type"] == "application/json" and b'"body":' in seen[0].content


def test_post_json_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(SCMAPIError) as info:
        _client(handler).post_json("/x", {})
    assert calls["n"] == 1 and info.value.http_status == 503


def test_post_json_error_has_no_body_and_empty_reply_is_dict() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=_SECRET_BODY) if req.url.path == "/v1/no" else httpx.Response(204)

    with pytest.raises(SCMAPIError) as info:
        _client(handler).post_json("/no", {})
    assert "ghp_SECRET123" not in str(info.value) and "lacks access" in str(info.value)
    assert _client(handler).post_json("/yes", {}) == {}
