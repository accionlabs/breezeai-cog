"""`integrations/scm/factory.py`: registry, credential resolution order, per-instance API
base resolution, and error types for the caller to map."""

from __future__ import annotations

import logging

import pytest

from breezeai_cog.config import Settings
from breezeai_cog.integrations.scm import (
    RepoRef,
    SCMClientFactory,
    SCMCredentialError,
    UnsupportedSCMProviderError,
)
from breezeai_cog.integrations.scm.azure_devops import AzureDevOpsSCMClient
from breezeai_cog.integrations.scm.bitbucket import BitbucketSCMClient
from breezeai_cog.integrations.scm.factory import resolve_api_base_url, resolve_scm_token
from breezeai_cog.integrations.scm.github import GitHubSCMClient
from breezeai_cog.integrations.scm.gitlab import GitLabSCMClient


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize(("provider", "cls"), [
    ("github", GitHubSCMClient), ("bitbucket", BitbucketSCMClient),
    ("gitlab", GitLabSCMClient), ("azure_devops", AzureDevOpsSCMClient),
    ("GitHub ", GitHubSCMClient),  # case/whitespace tolerant
])
def test_create_dispatches_to_registered_class(provider, cls) -> None:
    token = "u:k" if "bitbucket" in provider.lower() else "t"
    with SCMClientFactory.create(provider, token, _settings()) as client:
        assert isinstance(client, cls)


def test_unknown_provider_is_a_400_error() -> None:
    with pytest.raises(UnsupportedSCMProviderError) as info:
        SCMClientFactory.create("svn", "t", _settings())
    assert info.value.status_code == 400 and "svn" in str(info.value)


def test_bad_credential_shape_surfaces_from_the_client() -> None:
    with pytest.raises(SCMCredentialError) as info:
        SCMClientFactory.create("bitbucket", "no-colon", _settings())
    assert info.value.status_code == 400


# --- token resolution: request > global fallback > anonymous ---


def test_request_token_wins() -> None:
    s = _settings(scm_token_github="global")
    assert resolve_scm_token("github", "fromrequest", s) == "fromrequest"


def test_global_fallback_when_request_has_none() -> None:
    s = _settings(scm_token_gitlab="glpat-global")
    assert resolve_scm_token("gitlab", None, s) == "glpat-global"
    assert resolve_scm_token("gitlab", "", s) == "glpat-global"


def test_anonymous_when_nothing_configured(caplog: pytest.LogCaptureFixture) -> None:
    assert resolve_scm_token("github", None, _settings()) is None
    with caplog.at_level(logging.WARNING, logger="breezeai_cog.integrations.scm.http"):
        SCMClientFactory.create("github", None, _settings()).close()
    assert any("without a credential" in r.getMessage() for r in caplog.records)


# --- API base resolution ---


def test_public_host_uses_provider_default() -> None:
    ref = RepoRef("github", "a", "r", host="github.com")
    assert resolve_api_base_url(ref, _settings()) is None


def test_mapped_self_hosted_instance() -> None:
    ref = RepoRef("gitlab", "grp", "proj", host="git.acme.com")
    s = _settings(scm_instance_base_url_mapping={"git.acme.com": "https://git.acme.com/api/v4"})
    assert resolve_api_base_url(ref, s) == "https://git.acme.com/api/v4"


def test_unmapped_self_hosted_instance_warns_and_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    ref = RepoRef("gitlab", "grp", "proj", host="git.acme.com")
    with caplog.at_level(logging.WARNING, logger="breezeai_cog.integrations.scm.factory"):
        assert resolve_api_base_url(ref, _settings()) is None
    assert any("git.acme.com" in r.getMessage() for r in caplog.records)


def test_legacy_visualstudio_host_is_served_by_default_base(caplog: pytest.LogCaptureFixture) -> None:
    ref = RepoRef("azure_devops", "org", "r", project="p", host="org.visualstudio.com")
    with caplog.at_level(logging.WARNING, logger="breezeai_cog.integrations.scm.factory"):
        assert resolve_api_base_url(ref, _settings()) is None
    assert not caplog.records


def test_for_repo_threads_everything_through() -> None:
    ref = RepoRef("gitlab", "grp", "proj", host="git.acme.com")
    s = _settings(
        scm_token_gitlab="glpat-global",
        scm_instance_base_url_mapping={"git.acme.com": "https://git.acme.com/api/v4"},
    )
    with SCMClientFactory.for_repo(ref, s) as client:
        assert isinstance(client, GitLabSCMClient)
        assert client._http.base_url == "https://git.acme.com/api/v4"
        assert client.clone_url(ref) == "https://oauth2:glpat-global@git.acme.com/grp/proj.git"
