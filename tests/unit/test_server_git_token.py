"""The git token a request carries: encrypted by the Breeze backend, or raw as before.

``VECTOR`` was produced by the backend's own encryption code with a throwaway key, so a
change to the format on either side fails here.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from breezeai_cog.config import Settings
from breezeai_cog.integrations.scm import AbstractSCMClient, CommitInfo, RepoRef
from breezeai_cog.server.app import create_app
from breezeai_cog.server.deps import ServerDeps
from breezeai_cog.server.errors import ApiError
from breezeai_cog.server.git_token import (
    DECRYPT_FAILED_MESSAGE,
    KEY_MISSING_MESSAGE,
    open_git_token,
    request_git_token,
)

KEY = "BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc="
AAD = "6f1c2b7e-3d4a-4b5c-8d9e-0f1a2b3c4d5e"
ENCRYPTED = (
    "Ga31D41ZeoGZNIYT12OiU+KY5B0y9bNDQB4IHWSM6ANI817yNvZedMztGoG9kKAtKIskVElqBz6Z++ZVRvMPzomB"
)
TOKEN = "ghp_exampleToken0123456789"
REPO = "https://github.com/acme/widgets"


def _settings(key: str | None = KEY) -> Settings:
    return Settings(_env_file=None, credentials_encryption_key=SecretStr(key) if key else None)


def test_the_key_is_read_from_the_shared_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIALS_ENCRYPTION_KEY", KEY)
    settings = Settings(_env_file=None)
    assert settings.credentials_encryption_key is not None
    assert open_git_token(ENCRYPTED, AAD, settings) == TOKEN


def test_the_key_is_also_read_with_the_service_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREDENTIALS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("BREEZEAI_COG_CREDENTIALS_ENCRYPTION_KEY", KEY)
    assert open_git_token(ENCRYPTED, AAD, Settings(_env_file=None)) == TOKEN


def test_opens_a_token_the_backend_encrypted() -> None:
    assert open_git_token(ENCRYPTED, AAD, _settings()) == TOKEN


def test_a_request_with_the_associated_data_is_decrypted() -> None:
    body = {"gitToken": ENCRYPTED, "gitTokenAad": AAD}
    assert request_git_token(body, _settings()) == TOKEN


def test_a_request_without_the_associated_data_carries_a_raw_token() -> None:
    assert request_git_token({"gitToken": "ghp_raw"}, _settings(None)) == "ghp_raw"


def test_a_request_without_a_token_has_none() -> None:
    assert request_git_token({}, _settings()) is None
    assert request_git_token({"gitToken": ""}, _settings()) is None


def test_the_wrong_associated_data_does_not_open_it() -> None:
    with pytest.raises(ApiError) as err:
        open_git_token(ENCRYPTED, "another-credential", _settings())
    assert err.value.status_code == 400
    assert str(err.value) == DECRYPT_FAILED_MESSAGE


def test_the_wrong_key_does_not_open_it() -> None:
    other = base64.b64encode(bytes([9]) * 32).decode()
    with pytest.raises(ApiError) as err:
        open_git_token(ENCRYPTED, AAD, _settings(other))
    assert err.value.status_code == 400


@pytest.mark.parametrize("token", ["not base64!!", "AAAA", base64.b64encode(b"x" * 40).decode()])
def test_a_damaged_token_answers_the_same_400(token: str) -> None:
    with pytest.raises(ApiError) as err:
        open_git_token(token, AAD, _settings())
    assert err.value.status_code == 400
    assert str(err.value) == DECRYPT_FAILED_MESSAGE


@pytest.mark.parametrize("key", [None, "not a key", base64.b64encode(b"short").decode()])
def test_an_encrypted_token_without_a_usable_key_answers_503(key: str | None) -> None:
    with pytest.raises(ApiError) as err:
        open_git_token(ENCRYPTED, AAD, _settings(key))
    assert err.value.status_code == 503
    assert str(err.value) == KEY_MISSING_MESSAGE
    assert TOKEN not in str(err.value) and ENCRYPTED not in str(err.value)


class _Client(AbstractSCMClient):
    provider = "github"
    supports_incremental = True

    def branch_head(self, ref, branch):
        return CommitInfo("a" * 40, "m", "a", "2026-09-01T00:00:00Z")

    def clone_url(self, ref):
        raise NotImplementedError

    def compare(self, ref, base, head):
        raise NotImplementedError

    def file_content(self, ref, path, commit):
        raise NotImplementedError

    def post_pr_comment(self, ref, pull_request_id, body):
        raise NotImplementedError

    def pull_request(self, ref, pull_request_id):
        raise NotImplementedError

    def tree(self, ref, commit):
        raise NotImplementedError

    def close(self) -> None:
        pass


def _app(seen: list, settings: Settings) -> TestClient:
    def open_scm(ref: RepoRef, s: Settings, token: str | None) -> AbstractSCMClient:
        seen.append(token)
        return _Client()

    deps = ServerDeps(
        settings=settings,
        open_storage=lambda key: (_ for _ in ()).throw(AssertionError("no storage here")),
        notify=lambda path, payload: None,
        open_scm=open_scm,
    )
    return TestClient(create_app(settings, deps))


def test_a_git_route_hands_the_provider_the_decrypted_token() -> None:
    seen: list = []
    r = _app(seen, _settings()).post(
        "/api/git/latest-commit",
        json={"repoUrl": REPO, "gitBranch": "main", "gitToken": ENCRYPTED, "gitTokenAad": AAD},
    )
    assert r.status_code == 200
    assert seen == [TOKEN]


def test_a_git_route_still_accepts_a_raw_token() -> None:
    seen: list = []
    r = _app(seen, _settings(None)).post(
        "/api/git/latest-commit", json={"repoUrl": REPO, "gitBranch": "main", "gitToken": "ghp_raw"}
    )
    assert r.status_code == 200
    assert seen == ["ghp_raw"]


def test_a_git_route_refuses_an_encrypted_token_it_cannot_open_without_leaking_it() -> None:
    seen: list = []
    r = _app(seen, _settings(None)).post(
        "/api/git/latest-commit",
        json={"repoUrl": REPO, "gitBranch": "main", "gitToken": ENCRYPTED, "gitTokenAad": AAD},
    )
    assert r.status_code == 503
    assert seen == []
    assert ENCRYPTED not in r.text and TOKEN not in r.text
