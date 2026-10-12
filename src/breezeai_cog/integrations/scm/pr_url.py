"""Turn a pasted pull-request URL into what the backend's manual PR trigger needs.

Ported from the backend's ``prValidatorWorkflow.parsePrUrl`` so the URL grammar lives in
one place: the provider, the repository's canonical web URL (what ``codeOntology.repoUrl``
stores), the PR number, and the ``prLinks`` block the webhook paths also produce — the
repo web URL plus the provider REST URLs for the PR's diff, comment and decline actions.

No provider call, no token. Self-hosted hosts come from ``settings.scm_instances`` and the
REST base from ``settings.scm_instance_base_url_mapping``, which the old in-process
parser never had.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

from .base import DEFAULT_HOSTS, RepoRef
from .factory import resolve_api_base_url
from .repository import parse_repo_url

if TYPE_CHECKING:
    from ...config import Settings

#: ``…/pull/123``, ``…/pull-requests/123``, ``…/pullrequests/123``, ``…/merge_requests/123``,
#: ``…/pullrequest/123`` — then a bare trailing ``/123`` as the last resort.
_PR_ID: Final[re.Pattern[str]] = re.compile(
    r"(?:pull|pull-requests|pullrequests|merge_requests|pullrequest)/(\d+)"
)
_TRAILING_ID: Final[re.Pattern[str]] = re.compile(r"/(\d+)/?(?:[?#].*)?$")


@dataclass(frozen=True, slots=True)
class PrLinks:
    repo: str
    diff: str
    comment: str
    decline: str


@dataclass(frozen=True, slots=True)
class PrUrlInfo:
    source: str
    repo_url: str
    pr_id: str
    links: PrLinks


def repo_api_url(ref: RepoRef, settings: Settings) -> str:
    """The provider REST URL that addresses ``ref``'s repository — built from the same base
    the clients use (``resolve_api_base_url`` for self-hosted, else the provider default)
    and each client's repository path shape."""
    base = resolve_api_base_url(ref, settings)
    if ref.provider == "github":
        return f"{(base or settings.github_api_base_url).rstrip('/')}/repos/{ref.owner}/{ref.repo}"
    if ref.provider == "gitlab":
        path = quote(f"{ref.owner}/{ref.repo}", safe="")
        return f"{(base or settings.gitlab_api_base_url).rstrip('/')}/projects/{path}"
    if ref.provider == "bitbucket":
        return f"{(base or settings.bitbucket_api_base_url).rstrip('/')}/repositories/{ref.owner}/{ref.repo}"
    return (
        f"{(base or settings.azure_devops_api_base_url).rstrip('/')}/{quote(ref.owner, safe='')}"
        f"/{quote(ref.project, safe='')}/_apis/git/repositories/{quote(ref.repo, safe='')}"
    )


def repo_web_url(ref: RepoRef) -> str:
    """The repository's canonical browser URL — the form ``codeOntology.repoUrl`` stores."""
    host = ref.host or DEFAULT_HOSTS[ref.provider]
    if ref.provider != "azure_devops":
        return f"https://{host}/{ref.owner}/{ref.repo}"
    if host.endswith(".visualstudio.com"):
        return f"https://{host}/{ref.project}/_git/{ref.repo}"
    return f"https://{host}/{ref.owner}/{ref.project}/_git/{ref.repo}"


def parse_pr_url(pr_url: str, settings: Settings) -> PrUrlInfo | None:
    """``None`` when the host is not a known provider or no PR number can be found."""
    ref = parse_repo_url(pr_url, settings.scm_instances)
    if ref is None:
        return None
    m = _PR_ID.search(pr_url) or _TRAILING_ID.search(pr_url)
    if not m:
        return None
    pr_id = m.group(1)
    api = repo_api_url(ref, settings)
    web = repo_web_url(ref)
    if ref.provider == "github":
        links = PrLinks(web, f"{api}/pulls/{pr_id}", f"{api}/issues/{pr_id}/comments", f"{api}/pulls/{pr_id}")
    elif ref.provider == "gitlab":
        links = PrLinks(
            web, f"{api}/merge_requests/{pr_id}/changes", f"{api}/merge_requests/{pr_id}/notes",
            f"{api}/merge_requests/{pr_id}",
        )
    elif ref.provider == "bitbucket":
        links = PrLinks(
            web, f"{api}/pullrequests/{pr_id}/diff", f"{api}/pullrequests/{pr_id}/comments",
            f"{api}/pullrequests/{pr_id}/decline",
        )
    else:
        links = PrLinks(web, f"{api}/pullrequests/{pr_id}", f"{api}/pullrequests/{pr_id}", f"{api}/pullrequests/{pr_id}")
    return PrUrlInfo(source=ref.provider, repo_url=web, pr_id=pr_id, links=links)


__all__ = ["PrLinks", "PrUrlInfo", "parse_pr_url", "repo_api_url", "repo_web_url"]
