"""Repository URL parsing.

Turns the ``repoUrl`` a caller sends to ``/api/analyze-diff`` into a ``RepoRef``.
Public-cloud hosts are recognised by pattern, first match wins in the order GitHub →
Bitbucket → GitLab → Azure DevOps (the order the old ``server/git.py`` used). A host
listed in ``instances`` (``settings.scm_instances``: host → provider slug) is parsed
with that provider's path grammar, which is how GitHub Enterprise, self-hosted GitLab
and Bitbucket Server become addressable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from .base import SUPPORTED_PROVIDERS, RepoRef

_GITHUB = re.compile(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$")
_BITBUCKET = re.compile(r"bitbucket\.org/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$")
_GITLAB = re.compile(r"gitlab\.com/(.+)$")
_AZURE_DEVOPS = re.compile(
    r"(?:dev\.azure\.com/([^/]+)/([^/]+)"
    r"|([a-zA-Z0-9-]+)\.visualstudio\.com/([^/]+)(?:/([^/]+))?)/_git/([^/]+)"
)


def _strip_git(name: str) -> str:
    return name[:-4] if name.endswith(".git") else name


def _gitlab_segments(path: str) -> list[str]:
    """GitLab namespaces nest (``group/subgroup/project``). Keep the whole path; the
    project boundary in a web URL is ``/-/``. Query and fragment are noise."""
    raw = path.split("?")[0].split("#")[0].split("/-/")[0].strip("/")
    return [s for s in _strip_git(raw).split("/") if s]


def _two_segment(provider: str, host: str, path: str) -> RepoRef | None:
    """``<owner>/<repo>[.git][/…]`` grammar shared by GitHub and Bitbucket."""
    segments = [s for s in path.split("?")[0].split("#")[0].strip("/").split("/") if s]
    if len(segments) < 2:
        return None
    return RepoRef(provider=provider, owner=segments[0], repo=_strip_git(segments[1]), host=host)


def _gitlab(host: str, path: str) -> RepoRef | None:
    segments = _gitlab_segments(path)
    if len(segments) < 2:
        return None
    return RepoRef(
        provider="gitlab", owner="/".join(segments[:-1]), repo=segments[-1], host=host
    )


def _bitbucket_server(host: str, path: str) -> RepoRef | None:
    """Bitbucket Data Center, anchored on keywords rather than segment positions.

    An instance may be served under a context path (``/stash`` on installs carried over
    from Stash), which shifts every index, so the project key and slug are located by the
    ``scm`` or ``projects``/``repos`` anchor and everything before it becomes
    ``context_path``. Three URL shapes reach us::

        /{ctx}/scm/{KEY}/{slug}.git              the clone URL
        /{ctx}/projects/{KEY}/repos/{slug}/…     the browse / pull-request URL
        /{ctx}/users/{name}/repos/{slug}/…       a personal repository

    Data Center serves a personal repository under both ``/users/{name}`` and
    ``/projects/~{name}`` but its REST API accepts only the ``~`` form, so the ``users``
    spelling is normalised to it.
    """
    segments = [s for s in path.split("?")[0].split("#")[0].strip("/").split("/") if s]

    def ref(prefix: list[str], key: str, slug: str) -> RepoRef | None:
        if not key or not slug:
            return None
        return RepoRef(
            provider="bitbucket_server",
            owner=key,
            repo=_strip_git(slug),
            host=host,
            context_path=("/" + "/".join(prefix)) if prefix else "",
        )

    # /scm/{KEY}/{slug}.git — requiring exactly two trailing segments is what keeps a
    # context path that is itself named "scm" from matching first.
    for i, segment in enumerate(segments):
        if segment == "scm" and len(segments) - i - 1 == 2:
            return ref(segments[:i], segments[i + 1], segments[i + 2])

    for i, segment in enumerate(segments):
        if segment not in ("projects", "users") or i + 3 >= len(segments):
            continue
        if segments[i + 2] != "repos":
            continue
        key = segments[i + 1]
        if segment == "users" and not key.startswith("~"):
            key = f"~{key}"
        return ref(segments[:i], key, segments[i + 3])
    return None


def _azure_devops(host: str, path: str) -> RepoRef | None:
    """``<org>/<project>/_git/<repo>``. Azure DevOps Server adds a collection segment
    first; the last three segments carry the meaning either way."""
    segments = [s for s in path.split("?")[0].split("#")[0].strip("/").split("/") if s]
    if len(segments) < 4 or segments[-2] != "_git":
        return None
    return RepoRef(
        provider="azure_devops",
        owner=segments[-4],
        project=segments[-3],
        repo=_strip_git(segments[-1]),
        host=host,
    )


def _public(repo_url: str) -> RepoRef | None:
    gh = _GITHUB.search(repo_url)
    if gh:
        return RepoRef("github", gh.group(1), gh.group(2), host="github.com")
    bb = _BITBUCKET.search(repo_url)
    if bb:
        return RepoRef("bitbucket", bb.group(1), bb.group(2), host="bitbucket.org")
    gl = _GITLAB.search(repo_url)
    if gl:
        return _gitlab("gitlab.com", gl.group(1))
    az = _AZURE_DEVOPS.search(repo_url)
    if az:
        if az.group(1):  # dev.azure.com/<org>/<project>/_git/<repo>
            return RepoRef(
                "azure_devops", az.group(1), _strip_git(az.group(6)),
                project=az.group(2), host="dev.azure.com",
            )
        # <owner>.visualstudio.com/<project>/_git/<repo>
        return RepoRef(
            "azure_devops", az.group(3), _strip_git(az.group(6)),
            project=az.group(4), host=f"{az.group(3)}.visualstudio.com",
        )
    return None


def _host_of(repo_url: str) -> str:
    """Lower-cased host without userinfo or port; ``""`` when the URL has none."""
    candidate = repo_url if "://" in repo_url else f"https://{repo_url}"
    try:
        return (urlsplit(candidate).hostname or "").lower()
    except ValueError:
        return ""


def _instance(repo_url: str, instances: Mapping[str, str]) -> RepoRef | None:
    host = _host_of(repo_url)
    provider = instances.get(host) if host else None
    if not provider:
        return None
    provider = provider.lower().strip()
    if provider not in SUPPORTED_PROVIDERS:
        return None
    candidate = repo_url if "://" in repo_url else f"https://{repo_url}"
    path = urlsplit(candidate).path
    if provider in ("github", "bitbucket"):
        return _two_segment(provider, host, path)
    if provider == "bitbucket_server":
        return _bitbucket_server(host, path)
    if provider == "gitlab":
        return _gitlab(host, path)
    return _azure_devops(host, path)


def parse_bitbucket_server_url(repo_url: str) -> RepoRef | None:
    """Parse a Bitbucket Data Center URL on **any** host, with no ``instances`` entry.

    Used when the caller names the provider explicitly (``gitProvider:
    "bitbucket_server"`` on an ``/api/git/*`` request). Data Center has no public-cloud
    pattern to recognise, so the caller's slug is the only thing that can identify the
    grammar for a host nobody pre-registered. Every other provider still goes through
    ``parse_repo_url``'s host detection.

    Returns:
        The reference, or ``None`` when the URL has no host or no addressable
        ``scm`` / ``projects``+``repos`` anchor.
    """
    host = _host_of(repo_url)
    if not host:
        return None
    candidate = repo_url if "://" in repo_url else f"https://{repo_url}"
    return _bitbucket_server(host, urlsplit(candidate).path)


def parse_repo_url(repo_url: str, instances: Mapping[str, str] | None = None) -> RepoRef | None:
    """Parse a repository URL into a ``RepoRef``.

    Args:
        repo_url: HTTPS web or clone URL as the caller sent it.
        instances: Extra ``host → provider`` pairs for self-hosted instances
            (``settings.scm_instances``). Consulted only when no public-cloud
            pattern matched.

    Returns:
        The reference, or ``None`` when no provider claims the URL or the path is
        too short for the provider's grammar (the route answers 400).
    """
    ref = _public(repo_url)
    if ref is not None:
        return ref
    if instances:
        return _instance(repo_url, instances)
    return None


__all__ = ["parse_bitbucket_server_url", "parse_repo_url"]
