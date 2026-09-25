"""Abstract SCM client — the contract every git-hosting provider implements.

``/api/analyze-diff`` needs four things from a provider: the file tree at a commit, the
changed and deleted files between two commits, one file's content at a commit, and an
authenticated clone URL for the full-clone fallback. The backend's check-update flow adds a
fifth, the commit at the tip of a branch, and its sync-pr flow a sixth, read-only pull-request
metadata. The PR-validator adds the one write: posting a PR comment. Provider modules
(``github``, ``bitbucket``, ``gitlab``, ``azure_devops``) subclass
``AbstractSCMClient`` and return the unified shapes defined here, so the orchestration
in ``server/git.py`` never branches on the provider name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Final

#: Provider slugs. Also the keys of the factory registry, the suffix of the
#: ``scm_token_<slug>`` settings, and the allowed values of ``scm_instances``.
SUPPORTED_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"github", "bitbucket", "gitlab", "azure_devops"}
)

#: Public-cloud host per provider. ``RepoRef.host`` holds one of these unless the URL
#: named a self-hosted instance (or a legacy ``<owner>.visualstudio.com`` host).
DEFAULT_HOSTS: Final[dict[str, str]] = {
    "github": "github.com",
    "bitbucket": "bitbucket.org",
    "gitlab": "gitlab.com",
    "azure_devops": "dev.azure.com",
}


@dataclass(frozen=True, slots=True)
class RepoRef:
    """A parsed repository URL.

    Attributes:
        provider: One of ``SUPPORTED_PROVIDERS``.
        owner: Owner / organisation / workspace. For GitLab this is the full namespace
            (``group/subgroup``) so ``f"{owner}/{repo}"`` is the project path.
        repo: Bare repository name.
        project: Azure DevOps project; empty for every other provider.
        host: The host the URL named (``github.com``, ``acme.visualstudio.com``,
            ``git.acme.internal`` …). Lets the clone URL and API base URL follow the
            instance the request came from instead of assuming the public cloud.
    """

    provider: str
    owner: str
    repo: str
    project: str = ""
    host: str = ""

    @property
    def is_self_hosted(self) -> bool:
        """True when the host is not the provider's public cloud.

        A legacy ``<owner>.visualstudio.com`` host counts as self-hosted only in the
        sense that it is not ``dev.azure.com``; callers that build Azure URLs must
        handle it explicitly.
        """
        return bool(self.host) and self.host != DEFAULT_HOSTS.get(self.provider)

    def as_dict(self) -> dict[str, str]:
        """The legacy ``parse_repo_url`` dict shape (``project`` only when set)."""
        out = {"provider": self.provider, "owner": self.owner, "repo": self.repo}
        if self.project:
            out["project"] = self.project
        return out


@dataclass(slots=True)
class ChangeSet:
    """Files changed and deleted between two commits.

    ``changed`` holds every path whose content must be re-read at the head commit
    (added, modified, renamed-to). ``deleted`` holds paths that no longer exist at the
    head commit, including the old side of a rename.
    """

    changed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.changed and not self.deleted


@dataclass(frozen=True, slots=True)
class CommitInfo:
    """The tip of a branch: what ``/code-ontology/check-update`` needs to decide whether
    the stored ontology is behind and what to show the user.

    ``date`` is the provider's ISO-8601 string, passed through untouched.
    """

    sha: str
    message: str = ""
    author: str = ""
    date: str = ""


@dataclass(frozen=True, slots=True)
class PullRequestInfo:
    """A pull / merge request, read-only, in the shape ``/code-ontology/sync-pr`` needs.

    ``state`` is normalised to ``open`` / ``merged`` / ``closed`` across providers.
    ``base_sha`` / ``head_sha`` are **full** commit SHAs: the target-branch tip the PR
    was last compared against, and the source-branch tip. Feeding them to ``compare``
    yields the PR's changed files.
    """

    number: int
    title: str
    state: str
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    url: str = ""


@dataclass(frozen=True, slots=True)
class PrComment:
    """What a provider returns for a posted PR comment: its id and, when the provider
    gives one, a web URL. Either may be empty."""

    id: str = ""
    url: str = ""


#: Provider-native PR states → the three we expose.
PR_STATES: Final[dict[str, str]] = {
    # GitHub (state + merged_at handled in the client)
    "open": "open", "closed": "closed",
    # GitLab
    "opened": "open", "merged": "merged", "locked": "closed",
    # Bitbucket
    "OPEN": "open", "MERGED": "merged", "DECLINED": "closed", "SUPERSEDED": "closed",
    # Azure DevOps
    "active": "open", "completed": "merged", "abandoned": "closed",
}


class AbstractSCMClient(ABC):
    """Source-acquisition operations one git-hosting provider offers.

    Implementations are synchronous: the route runs acquisition in a threadpool, and
    the ``git`` subprocess fallback is synchronous anyway.

    Attributes:
        provider: The provider slug this client serves.
        supports_incremental: Whether ``tree`` / ``compare`` / ``file_content`` are
            implemented well enough for the incremental REST path. When ``False`` the
            orchestration always falls back to ``clone_url`` and a full clone.
    """

    provider: str = ""
    supports_incremental: bool = False

    @abstractmethod
    def tree(self, ref: RepoRef, commit: str) -> list[str]:
        """Every blob path in the repository at ``commit``.

        The orchestration materialises these as empty files so the scanner and import
        resolution see the whole repo shape, then overlays real content for the changed
        files only.
        """

    @abstractmethod
    def branch_head(self, ref: RepoRef, branch: str) -> CommitInfo:
        """The commit at the tip of ``branch``.

        Raises:
            SCMAPIError: when the branch does not exist (``http_status`` 404) or the
                provider cannot be reached.
        """

    @abstractmethod
    def pull_request(self, ref: RepoRef, number: int) -> PullRequestInfo:
        """Read-only metadata of one pull / merge request.

        Raises:
            SCMAPIError: when the PR does not exist (``http_status`` 404) or the
                provider cannot be reached.
        """

    @abstractmethod
    def post_pr_comment(self, ref: RepoRef, number: int, body: str) -> PrComment:
        """Post one top-level comment on a pull / merge request. **The only write** in
        this contract; the request is not retried (see ``SCMHttpClient.post_json``).

        Raises:
            SCMAPIError: when the PR does not exist or the token cannot write.
        """

    @abstractmethod
    def compare(self, ref: RepoRef, base: str, head: str) -> ChangeSet:
        """Files changed and deleted going from ``base`` to ``head``."""

    @abstractmethod
    def file_content(self, ref: RepoRef, path: str, commit: str) -> str:
        """Decoded text of one file at ``commit``.

        Raises:
            SCMAPIError: when the provider refuses or cannot serve the file. Callers
                treat a failure as "skip this file" and log it.
        """

    @abstractmethod
    def clone_url(self, ref: RepoRef) -> str:
        """HTTPS clone URL with the client's credential embedded, or without one when
        the client is anonymous. Never log the return value unscrubbed."""

    def close(self) -> None:
        """Release any pooled connections. Safe to call more than once."""

    def __enter__(self) -> AbstractSCMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
