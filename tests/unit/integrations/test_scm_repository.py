"""`integrations/scm/repository.py`: repo URL → RepoRef for the four public clouds and
for self-hosted instances declared via `scm_instances`."""

from __future__ import annotations

import pytest

from breezeai_cog.integrations.scm import RepoRef, parse_repo_url
from breezeai_cog.server import git as git_mod


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/acme/widgets.git",
            RepoRef("github", "acme", "widgets", host="github.com"),
        ),
        (
            "https://github.com/acme/widgets/tree/main/src",
            RepoRef("github", "acme", "widgets", host="github.com"),
        ),
        (
            "https://bitbucket.org/acme/widgets",
            RepoRef("bitbucket", "acme", "widgets", host="bitbucket.org"),
        ),
        (
            "https://gitlab.com/group/subgroup/project.git",
            RepoRef("gitlab", "group/subgroup", "project", host="gitlab.com"),
        ),
        (
            "https://gitlab.com/group/project/-/tree/main?ref_type=heads#readme",
            RepoRef("gitlab", "group", "project", host="gitlab.com"),
        ),
        (
            "https://dev.azure.com/my-org/my-project/_git/my-repo",
            RepoRef("azure_devops", "my-org", "my-repo", project="my-project", host="dev.azure.com"),
        ),
        (
            "https://my-org.visualstudio.com/my-project/_git/my-repo",
            RepoRef(
                "azure_devops", "my-org", "my-repo",
                project="my-project", host="my-org.visualstudio.com",
            ),
        ),
    ],
)
def test_public_hosts(url: str, expected: RepoRef) -> None:
    assert parse_repo_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/acme/widgets",
        "https://gitlab.com/only-one-segment",
        "not a url",
    ],
)
def test_unknown_or_short_returns_none(url: str) -> None:
    assert parse_repo_url(url) is None


def test_public_hosts_are_not_self_hosted() -> None:
    ref = parse_repo_url("https://github.com/acme/widgets")
    assert ref is not None and not ref.is_self_hosted


def test_visualstudio_host_is_flagged_non_default() -> None:
    ref = parse_repo_url("https://my-org.visualstudio.com/my-project/_git/my-repo")
    assert ref is not None and ref.is_self_hosted


# --- self-hosted instances (settings.scm_instances: host -> provider) ---

_INSTANCES = {
    "git.acme.internal": "gitlab",
    "ghe.acme.com": "github",
    "bb.acme.com": "bitbucket",
    "ado.acme.com": "azure_devops",
}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://git.acme.internal/grp/sub/proj/-/tree/main",
            RepoRef("gitlab", "grp/sub", "proj", host="git.acme.internal"),
        ),
        (
            "https://ghe.acme.com/acme/widgets.git",
            RepoRef("github", "acme", "widgets", host="ghe.acme.com"),
        ),
        (
            "https://user@bb.acme.com:8443/acme/widgets",
            RepoRef("bitbucket", "acme", "widgets", host="bb.acme.com"),
        ),
        (
            "https://ado.acme.com/DefaultCollection/my-project/_git/my-repo",
            RepoRef("azure_devops", "DefaultCollection", "my-repo", project="my-project", host="ado.acme.com"),
        ),
    ],
)
def test_self_hosted_instances(url: str, expected: RepoRef) -> None:
    assert parse_repo_url(url, _INSTANCES) == expected


def test_self_hosted_host_not_listed_returns_none() -> None:
    assert parse_repo_url("https://git.other.com/grp/proj", _INSTANCES) is None


def test_public_pattern_wins_over_instances() -> None:
    ref = parse_repo_url("https://github.com/acme/widgets", {"github.com": "gitlab"})
    assert ref == RepoRef("github", "acme", "widgets", host="github.com")


# --- legacy dict shim in server/git.py (route + orchestration still consume dicts) ---


def test_legacy_shim_dict_shape() -> None:
    assert git_mod.parse_repo_url("https://github.com/acme/widgets") == {
        "provider": "github", "owner": "acme", "repo": "widgets",
    }
    assert git_mod.parse_repo_url("https://dev.azure.com/my-org/my-project/_git/my-repo") == {
        "provider": "azure_devops", "owner": "my-org", "repo": "my-repo", "project": "my-project",
    }
    assert git_mod.parse_repo_url("https://example.com/x/y") is None


def test_legacy_shim_ignores_self_hosted_until_wired() -> None:
    """Until the clone/REST helpers read RepoRef.host, a self-hosted URL must 400 rather
    than silently target the public cloud."""
    assert git_mod.parse_repo_url("https://git.acme.internal/grp/proj") is None
