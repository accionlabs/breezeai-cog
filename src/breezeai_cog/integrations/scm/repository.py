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
from dataclasses import dataclass
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


def _two_segment(provider: str, origin: "_Origin", path: str) -> RepoRef | None:
    """``<owner>/<repo>[.git][/…]`` grammar shared by GitHub and Bitbucket."""
    segments = [s for s in path.split("?")[0].split("#")[0].strip("/").split("/") if s]
    if len(segments) < 2:
        return None
    return RepoRef(
        provider=provider, owner=segments[0], repo=_strip_git(segments[1]), host=origin.host, scheme=origin.scheme, port=origin.port,
    )


def _gitlab(origin: "_Origin", path: str) -> RepoRef | None:
    segments = _gitlab_segments(path)
    if len(segments) < 2:
        return None
    return RepoRef(
        provider="gitlab", owner="/".join(segments[:-1]), repo=segments[-1], host=origin.host, scheme=origin.scheme, port=origin.port,
    )


def _bitbucket_server(origin: "_Origin", path: str) -> RepoRef | None:
    """Bitbucket Data Center, anchored on keywords rather than segment positions.

    An instance may be served under a context path (``/stash`` on installs carried over
    from Stash), which shifts every index, so the project key and slug are located by the
    ``projects``/``repos`` or ``scm`` anchor and everything before it becomes
    ``context_path``. Three URL shapes reach us::

        /{ctx}/projects/{KEY}/repos/{slug}/…     the browse / pull-request URL
        /{ctx}/users/{name}/repos/{slug}/…       a personal repository
        /{ctx}/scm/{KEY}/{slug}.git              the clone URL

    ``projects``/``users`` is tried **before** ``scm`` because a browsed file path may
    itself contain a directory called ``scm``
    (``/projects/P3/repos/app/browse/scm/dir/f.py``); matching ``scm`` first would read
    that directory as the project key.

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
            context_path=("/" + "/".join(prefix)) if prefix else "",
            host=origin.host, scheme=origin.scheme, port=origin.port,
        )

    for i, segment in enumerate(segments):
        if segment not in ("projects", "users") or i + 3 >= len(segments):
            continue
        if segments[i + 2] != "repos":
            continue
        key = segments[i + 1]
        if segment == "users" and not key.startswith("~"):
            key = f"~{key}"
        return ref(segments[:i], key, segments[i + 3])

    # /scm/{KEY}/{slug}.git — requiring exactly two trailing segments is what keeps a
    # context path that is itself named "scm" from matching first.
    for i, segment in enumerate(segments):
        if segment == "scm" and len(segments) - i - 1 == 2:
            return ref(segments[:i], segments[i + 1], segments[i + 2])
    return None


def _azure_devops(origin: "_Origin", path: str) -> RepoRef | None:
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
        host=origin.host, scheme=origin.scheme, port=origin.port,
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
        return _gitlab(_Origin("gitlab.com"), gl.group(1))
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


@dataclass(frozen=True, slots=True)
class _Origin:
    """Scheme, host and explicit port of a repository URL.

    Carried into every ``RepoRef`` so the REST base, clone URL and web URL all address
    the instance the request actually named. Dropping the port sends a stock Bitbucket
    Data Center install (7990, or 8443 behind a proxy) to 443, where nothing answers.
    """

    host: str
    scheme: str = "https"
    port: int | None = None


def _origin_of(repo_url: str) -> _Origin | None:
    """Parse scheme / host / port; ``None`` when the URL names no host.

    A URL with no scheme is assumed HTTPS, which is what the old behaviour did. A port
    that equals the scheme default is normalised away so ``https://h:443`` and
    ``https://h`` produce the same reference.
    """
    candidate = repo_url if "://" in repo_url else f"https://{repo_url}"
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    scheme = (parts.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return None
    if port is not None and port == (443 if scheme == "https" else 80):
        port = None
    return _Origin(host=host, scheme=scheme, port=port)


def _host_of(repo_url: str) -> str:
    """Lower-cased host without userinfo or port; ``""`` when the URL has none.

    Hostname only: ``scm_instances`` and ``scm_instance_base_url_mapping`` are keyed by
    host, not by host:port.
    """
    origin = _origin_of(repo_url)
    return origin.host if origin else ""


def _instance(repo_url: str, instances: Mapping[str, str]) -> RepoRef | None:
    origin = _origin_of(repo_url)
    provider = instances.get(origin.host) if origin else None
    if not origin or not provider:
        return None
    provider = provider.lower().strip()
    if provider not in SUPPORTED_PROVIDERS:
        return None
    candidate = repo_url if "://" in repo_url else f"https://{repo_url}"
    path = urlsplit(candidate).path
    if provider in ("github", "bitbucket"):
        return _two_segment(provider, origin, path)
    if provider == "bitbucket_server":
        return _bitbucket_server(origin, path)
    if provider == "gitlab":
        return _gitlab(origin, path)
    return _azure_devops(origin, path)


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
    origin = _origin_of(repo_url)
    if origin is None:
        return None
    candidate = repo_url if "://" in repo_url else f"https://{repo_url}"
    return _bitbucket_server(origin, urlsplit(candidate).path)


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
