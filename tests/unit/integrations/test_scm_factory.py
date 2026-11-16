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


# ── Bitbucket Data Center ──────────────────────────────────────────────────

_DC_REF = RepoRef(
    "bitbucket_server", "p3", "kit", host="stash.payroll.co.jp", context_path="/stash"
)


def test_bitbucket_server_without_a_token_is_a_400_before_any_call() -> None:
    """`requires_credential` is enforced in the factory, so a tokenless request gets one
    400 naming gitToken instead of a 401 per provider call."""
    settings = Settings(_env_file=None)
    with pytest.raises(SCMCredentialError) as info:
        SCMClientFactory.for_repo(_DC_REF, settings)
    assert info.value.status_code == 400 and "gitToken" in str(info.value)


def test_bitbucket_server_has_no_deployment_wide_token_fallback() -> None:
    """Every Data Center host is a different customer's instance, so there is no
    `scm_token_bitbucket_server` for a tokenless request to fall back to."""
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "scm_token_bitbucket_server")
    assert resolve_scm_token("bitbucket_server", None, settings) is None
    assert resolve_scm_token("bitbucket_server", "abc", settings) == "abc"


def test_bitbucket_server_api_base_is_derived_from_the_context_path() -> None:
    settings = Settings(_env_file=None)
    assert resolve_api_base_url(_DC_REF, settings) == (
        "https://stash.payroll.co.jp/stash/rest/api/1.0"
    )


def test_bitbucket_server_api_base_mapping_wins_over_the_derived_one() -> None:
    settings = Settings(
        _env_file=None,
        scm_instance_base_url_mapping={
            "stash.payroll.co.jp": "https://stash.payroll.co.jp/proxied/rest/api/1.0"
        },
    )
    assert resolve_api_base_url(_DC_REF, settings) == (
        "https://stash.payroll.co.jp/proxied/rest/api/1.0"
    )


# ── which hosts this service will connect to ───────────────────────────────

def test_a_loopback_host_is_refused() -> None:
    """The caller names the host, so without this the service probes its own pod."""
    from breezeai_cog.integrations.scm.netguard import SCMHostNotAllowedError

    settings = Settings(_env_file=None)
    ref = RepoRef("bitbucket_server", "P3", "kit", host="localhost", context_path="")
    with pytest.raises(SCMHostNotAllowedError) as info:
        SCMClientFactory.for_repo(ref, settings, request_token="dXNlcjpwYXNz")
    assert info.value.status_code == 400


def test_a_private_host_is_allowed_by_default() -> None:
    """Self-hosted Bitbucket and GitHub Enterprise normally live on a private address;
    blocking those by default would block the self-hosted providers entirely."""
    from breezeai_cog.integrations.scm.netguard import ensure_host_allowed

    ensure_host_allowed("10.0.0.5", Settings(_env_file=None))


def test_private_hosts_can_be_blocked_explicitly() -> None:
    from breezeai_cog.integrations.scm.netguard import SCMHostNotAllowedError, ensure_host_allowed

    with pytest.raises(SCMHostNotAllowedError):
        ensure_host_allowed("10.0.0.5", Settings(_env_file=None, scm_block_private_networks=True))


def test_an_allow_list_bounds_the_reachable_hosts() -> None:
    from breezeai_cog.integrations.scm.netguard import SCMHostNotAllowedError, ensure_host_allowed

    settings = Settings(_env_file=None, scm_allowed_api_hosts=["stash.payroll.co.jp"])
    ensure_host_allowed("stash.payroll.co.jp", settings)
    with pytest.raises(SCMHostNotAllowedError):
        ensure_host_allowed("evil.example.com", settings)
