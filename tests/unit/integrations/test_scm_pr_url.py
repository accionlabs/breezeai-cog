"""`integrations/scm/pr_url.py`: a pasted PR URL → source, canonical repo URL, PR id and the
prLinks block — the backend's former `prValidatorWorkflow.parsePrUrl`, provider by provider."""

from __future__ import annotations

import pytest

from breezeai_cog.config import Settings
from breezeai_cog.integrations.scm import RepoRef
from breezeai_cog.integrations.scm.pr_url import PrLinks, PrUrlInfo, parse_pr_url, repo_api_url, repo_web_url


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_github() -> None:
    out = parse_pr_url("https://github.com/acme/widgets/pull/123", _s())
    api = "https://api.github.com/repos/acme/widgets"
    assert out == PrUrlInfo("github", "https://github.com/acme/widgets", "123",
                            PrLinks("https://github.com/acme/widgets", f"{api}/pulls/123", f"{api}/issues/123/comments", f"{api}/pulls/123"))


def test_gitlab_nested_group() -> None:
    out = parse_pr_url("https://gitlab.com/group/sub/proj/-/merge_requests/7", _s())
    api = "https://gitlab.com/api/v4/projects/group%2Fsub%2Fproj"
    assert out is not None and out.source == "gitlab" and out.pr_id == "7"
    assert out.repo_url == "https://gitlab.com/group/sub/proj"
    assert out.links == PrLinks(out.repo_url, f"{api}/merge_requests/7/changes", f"{api}/merge_requests/7/notes", f"{api}/merge_requests/7")


def test_bitbucket() -> None:
    out = parse_pr_url("https://bitbucket.org/acme/widgets/pull-requests/42/diff", _s())
    api = "https://api.bitbucket.org/2.0/repositories/acme/widgets"
    assert out is not None and out.pr_id == "42"
    assert out.links == PrLinks("https://bitbucket.org/acme/widgets", f"{api}/pullrequests/42/diff", f"{api}/pullrequests/42/comments", f"{api}/pullrequests/42/decline")


def test_azure_devops_canonical_repo_url_keeps_the_project() -> None:
    """The backend built `https://dev.azure.com/{org}/{repo}` here — it dropped the project,
    so the repoUrl never matched a stored ontology. The canonical `_git` form is used."""
    out = parse_pr_url("https://dev.azure.com/org/proj/_git/repo/pullrequest/9?_a=files", _s())
    api = "https://dev.azure.com/org/proj/_apis/git/repositories/repo"
    assert out == PrUrlInfo("azure_devops", "https://dev.azure.com/org/proj/_git/repo", "9",
                            PrLinks("https://dev.azure.com/org/proj/_git/repo", f"{api}/pullrequests/9", f"{api}/pullrequests/9", f"{api}/pullrequests/9"))


def test_legacy_visualstudio_host() -> None:
    out = parse_pr_url("https://org.visualstudio.com/proj/_git/repo/pullrequest/3", _s())
    assert out is not None and out.repo_url == "https://org.visualstudio.com/proj/_git/repo"
    assert out.links.diff == "https://dev.azure.com/org/proj/_apis/git/repositories/repo/pullrequests/3"


def test_self_hosted_gitlab_uses_mapped_base_and_host() -> None:
    s = _s(scm_instances={"git.acme.com": "gitlab"}, scm_instance_base_url_mapping={"git.acme.com": "https://git.acme.com/api/v4"})
    out = parse_pr_url("https://git.acme.com/grp/proj/-/merge_requests/5", s)
    assert out is not None and out.repo_url == "https://git.acme.com/grp/proj"
    assert out.links.comment == "https://git.acme.com/api/v4/projects/grp%2Fproj/merge_requests/5/notes"


def test_trailing_number_fallback_and_query_strings() -> None:
    assert parse_pr_url("https://github.com/acme/widgets/pull/12?diff=split#x", _s()).pr_id == "12"  # type: ignore[union-attr]
    assert parse_pr_url("https://github.com/acme/widgets/44/", _s()).pr_id == "44"  # type: ignore[union-attr]


@pytest.mark.parametrize("url", [
    "https://example.com/acme/widgets/pull/1",   # unknown host (the backend defaulted to GitHub)
    "https://github.com/acme/widgets",           # no PR number
    "https://github.com/acme/widgets/pull/abc",  # non-numeric
])
def test_none_when_unparseable(url: str) -> None:
    assert parse_pr_url(url, _s()) is None


def test_repo_web_and_api_url_helpers() -> None:
    ref = RepoRef("azure_devops", "DefaultCollection", "repo", project="proj", host="ado.acme.com")
    s = _s(scm_instances={"ado.acme.com": "azure_devops"}, scm_instance_base_url_mapping={"ado.acme.com": "https://ado.acme.com"})
    assert repo_web_url(ref) == "https://ado.acme.com/DefaultCollection/proj/_git/repo"
    assert repo_api_url(ref, s) == "https://ado.acme.com/DefaultCollection/proj/_apis/git/repositories/repo"
