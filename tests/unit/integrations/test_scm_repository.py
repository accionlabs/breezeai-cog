"""`integrations/scm/repository.py`: repo URL → RepoRef for the four public clouds and
for self-hosted instances declared via `scm_instances`."""

from __future__ import annotations

import pytest

from breezeai_cog.integrations.scm import RepoRef, parse_bitbucket_server_url, parse_repo_url
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
            # Userinfo is dropped; the explicit port is kept, or every call would go
            # to 443 and a self-hosted instance on another port could never connect.
            "https://user@bb.acme.com:8443/acme/widgets",
            RepoRef("bitbucket", "acme", "widgets", host="bb.acme.com", port=8443),
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


# ── Bitbucket Data Center ──────────────────────────────────────────────────

_STASH = {"stash.example.com": "bitbucket_server"}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # The clone URL, under a /stash context path.
        (
            "https://stash.example.com/stash/scm/proj/order-batch-create.git",
            RepoRef("bitbucket_server", "proj", "order-batch-create",
                    host="stash.example.com", context_path="/stash"),
        ),
        # Browse URL, same instance.
        (
            "https://stash.example.com/stash/projects/PROJ/repos/cog/browse",
            RepoRef("bitbucket_server", "PROJ", "cog",
                    host="stash.example.com", context_path="/stash"),
        ),
        # Pull-request URL keeps the slug, not the trailing segments.
        (
            "https://stash.example.com/stash/projects/PROJ/repos/cog/pull-requests/42/overview",
            RepoRef("bitbucket_server", "PROJ", "cog",
                    host="stash.example.com", context_path="/stash"),
        ),
        # Served at the root: no context path.
        (
            "https://stash.example.com/scm/PROJ/cog.git",
            RepoRef("bitbucket_server", "PROJ", "cog", host="stash.example.com"),
        ),
        # A personal repository: /users/{name} normalises to the API's ~{name}.
        (
            "https://stash.example.com/users/jdoe/repos/cog/browse",
            RepoRef("bitbucket_server", "~jdoe", "cog", host="stash.example.com"),
        ),
        (
            "https://stash.example.com/projects/~jdoe/repos/cog/browse",
            RepoRef("bitbucket_server", "~jdoe", "cog", host="stash.example.com"),
        ),
        # A context path literally named "scm" still resolves: only one index leaves
        # exactly two trailing segments.
        (
            "https://stash.example.com/scm/scm/PROJ/cog.git",
            RepoRef("bitbucket_server", "PROJ", "cog",
                    host="stash.example.com", context_path="/scm"),
        ),
    ],
)
def test_bitbucket_server_urls(url, expected) -> None:
    assert parse_repo_url(url, _STASH) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://stash.example.com/stash/projects/PROJ/browse",   # no /repos segment
        "https://stash.example.com/stash/scm/PROJ",               # slug missing
        "https://stash.example.com/stash/dashboard",            # not a repository URL
    ],
)
def test_bitbucket_server_rejects_urls_it_cannot_address(url) -> None:
    assert parse_repo_url(url, _STASH) is None


def test_bitbucket_server_is_unreachable_without_an_instance_entry() -> None:
    """Data Center has no public-cloud pattern, so an unregistered host is a 400."""
    assert parse_repo_url(
        "https://stash.example.com/stash/scm/proj/cog.git", {}
    ) is None


def test_parse_bitbucket_server_url_needs_no_instances_entry() -> None:
    """What `gitProvider: "bitbucket_server"` on a request buys: an unregistered host."""
    assert parse_bitbucket_server_url(
        "https://stash.example.com/stash/scm/proj/order-batch-create.git"
    ) == RepoRef(
        "bitbucket_server", "proj", "order-batch-create",
        host="stash.example.com", context_path="/stash",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://stash.example.com/stash/dashboard",  # no repository anchor
        "/stash/scm/proj/kit.git",                        # no host
    ],
)
def test_parse_bitbucket_server_url_rejects_what_it_cannot_address(url) -> None:
    assert parse_bitbucket_server_url(url) is None


# ── scheme and port survive parsing (self-hosted instances) ────────────────

@pytest.mark.parametrize(
    ("url", "origin", "context"),
    [
        # Data Center's stock HTTP port, served at the root.
        ("http://bb.corp:7990/scm/PROJ/kit.git", "http://bb.corp:7990", ""),
        # Behind a TLS proxy on 8443, under a /stash context path.
        ("https://bb.corp:8443/stash/scm/PROJ/kit.git", "https://bb.corp:8443", "/stash"),
        # A port equal to the scheme default normalises away.
        ("https://bb.corp:443/stash/scm/PROJ/kit.git", "https://bb.corp", "/stash"),
        ("http://bb.corp:80/scm/PROJ/kit.git", "http://bb.corp", ""),
    ],
)
def test_bitbucket_server_keeps_scheme_and_port(url, origin, context) -> None:
    ref = parse_bitbucket_server_url(url)
    assert ref is not None
    assert (ref.origin, ref.context_path) == (origin, context)


def test_a_browsed_file_path_containing_scm_is_not_read_as_the_project() -> None:
    """`projects`/`repos` is matched before `scm`, so a directory called `scm` inside a
    browsed path cannot be mistaken for the project key."""
    ref = parse_bitbucket_server_url(
        "https://bb.corp/stash/projects/PROJ/repos/app/browse/scm/dir/f.py"
    )
    assert ref is not None
    assert (ref.owner, ref.repo, ref.context_path) == ("PROJ", "app", "/stash")
