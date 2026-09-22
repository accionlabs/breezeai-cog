"""POST /api/directory-tree (BREEZEAI-1228 follow-up 3).

Port of `GitService.getDirectoryTree`, which had been commented out in the
backend as dead code. Covers the four providers, both pagination styles, and
GitHub's `truncated` flag — which the original silently ignored.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from breezeai_cog.server.app import create_app

GH = "https://github.com/acme/widgets"
GL = "https://gitlab.com/grp/proj"
BB = "https://bitbucket.org/team/repo"
AZ = "https://dev.azure.com/org/proj/_git/repo"


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _Captured(list):
    def __init__(self) -> None:
        super().__init__()
        self.queue: list[_FakeResponse] = []


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    import httpx

    calls = _Captured()

    def fake_get(url: str, headers=None, params=None, timeout=None):
        calls.append({"url": url, "params": params or {}})
        return calls.queue.pop(0) if calls.queue else _FakeResponse({})

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _get(client: TestClient, **body) -> Any:
    payload = {"repoUrl": GH}
    payload.update(body)
    return client.post("/api/directory-tree", json=payload)


def test_github_maps_tree_and_blob(client, captured) -> None:
    captured.queue.append(_FakeResponse({"tree": [
        {"path": "src", "type": "tree"},
        {"path": "src/app.ts", "type": "blob", "size": 120},
    ]}))
    body = _get(client).json()
    assert body["entries"] == [
        {"path": "src", "type": "tree", "size": None},
        {"path": "src/app.ts", "type": "blob", "size": 120},
    ]
    assert body["truncated"] is False
    assert captured[0]["params"]["recursive"] == 1


def test_github_surfaces_truncation(client, captured) -> None:
    """GitHub caps this endpoint instead of paginating. The backend ignored the
    flag, so a very large repo silently produced a partial tree."""
    captured.queue.append(_FakeResponse({"tree": [{"path": "a", "type": "blob"}], "truncated": True}))
    assert _get(client).json()["truncated"] is True


def test_gitlab_pages_until_a_short_page(client, captured) -> None:
    captured.queue.append(_FakeResponse([{"path": f"f{i}", "type": "blob"} for i in range(100)]))
    captured.queue.append(_FakeResponse([{"path": "tail", "type": "tree"}]))
    body = _get(client, repoUrl=GL, gitToken="glpat").json()
    assert len(body["entries"]) == 101
    assert body["entries"][-1] == {"path": "tail", "type": "tree", "size": None}
    assert [c["params"]["page"] for c in captured] == [1, 2]


def test_gitlab_stops_on_an_empty_page(client, captured) -> None:
    captured.queue.append(_FakeResponse([]))
    assert _get(client, repoUrl=GL).json()["entries"] == []
    assert len(captured) == 1


def test_azure_strips_the_leading_slash_and_flags_folders(client, captured) -> None:
    captured.queue.append(_FakeResponse({"value": [
        {"path": "/src", "isFolder": True},
        {"path": "/src/a.cs", "isFolder": False, "size": 42},
    ]}))
    body = _get(client, repoUrl=AZ, gitToken="pat").json()
    assert body["entries"] == [
        {"path": "src", "type": "tree", "size": None},
        {"path": "src/a.cs", "type": "blob", "size": 42},
    ]
    # versionType is PascalCase-strict on Azure.
    assert json.loads(captured[0]["params"]["versionDescriptor"])["versionType"] == "Branch"


def test_bitbucket_follows_the_next_cursor(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "values": [{"path": "a.py", "type": "commit_file", "size": 10}],
        "next": "https://api.bitbucket.org/page2",
    }))
    captured.queue.append(_FakeResponse({
        "values": [{"path": "sub", "type": "commit_directory"}],
    }))
    body = _get(client, repoUrl=BB, gitToken="u:k").json()
    assert [e["path"] for e in body["entries"]] == ["a.py", "sub"]
    assert body["entries"][1]["type"] == "tree"
    assert captured[1]["url"] == "https://api.bitbucket.org/page2"


def test_branch_defaults_to_main(client, captured) -> None:
    captured.queue.append(_FakeResponse({"tree": []}))
    _get(client)
    assert captured[0]["url"].endswith("/git/trees/main")


def test_unknown_host_is_a_400(client, captured) -> None:
    assert _get(client, repoUrl="https://git.internal.corp/t/r").status_code == 400
    assert captured == []


def test_requires_repo_url(client) -> None:
    assert client.post("/api/directory-tree", json={}).status_code == 400


def test_provider_failure_surfaces_as_502(client, captured) -> None:
    captured.queue.append(_FakeResponse({"message": "Not Found"}, status_code=404))
    assert _get(client).status_code == 502
