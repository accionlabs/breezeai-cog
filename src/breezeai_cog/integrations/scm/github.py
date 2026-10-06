"""GitHub provider — REST API v3 over ``SCMHttpClient``.

Works against github.com and GitHub Enterprise Server: the API base comes from
``settings.github_api_base_url`` unless the factory passes a per-instance override,
and the clone URL follows ``RepoRef.host``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from .base import DEFAULT_HOSTS, PR_STATES, AbstractSCMClient, ChangeSet, CommitInfo, PrComment, PullRequestInfo, RepoRef
from .http import SCMHttpClient, warn_if_anonymous

if TYPE_CHECKING:
    from ...config import Settings

#: Media type that makes ``/contents`` return the raw bytes instead of a base64 JSON
#: envelope. The envelope path stopped at 1 MB (``encoding: "none"``); raw serves files
#: up to 100 MB, which is far past ``max_file_size`` anyway.
_RAW = "application/vnd.github.raw+json"


class GitHubSCMClient(AbstractSCMClient):
    """GitHub source-acquisition client.

    Args:
        token: Personal access token or installation token; ``None`` for anonymous.
        settings: Supplies the API base and the HTTP tuning knobs.
        api_base_url: Per-instance override of ``settings.github_api_base_url``.
        transport: Injectable ``httpx`` transport for tests.
        sleep: Injectable retry wait for tests.
    """

    provider = "github"
    supports_incremental = True

    def __init__(
        self,
        token: str | None,
        settings: Settings,
        api_base_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token or None
        warn_if_anonymous("GitHub", self._token)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._http = SCMHttpClient(
            provider="GitHub",
            base_url=api_base_url or settings.github_api_base_url,
            headers=headers,
            settings=settings,
            transport=transport,
            sleep=sleep,
        )

    def close(self) -> None:
        self._http.close()

    # ── AbstractSCMClient ──────────────────────────────────────────────────

    def tree(self, ref: RepoRef, commit: str) -> list[str]:
        """Recursive tree in one call. GitHub truncates past 100 000 entries / 7 MB
        and says so with ``truncated: true``; that is logged by the caller's size
        heuristics, not here, because the response shape gives no page to follow."""
        data = self._http.get_json(
            f"/repos/{ref.owner}/{ref.repo}/git/trees/{commit}", {"recursive": "1"}
        )
        return [
            e["path"] for e in (data.get("tree") or [])
            if e.get("type") == "blob" and e.get("path")
        ]

    def branch_head(self, ref: RepoRef, branch: str) -> CommitInfo:
        """``GET /repos/{o}/{r}/branches/{branch}`` — the branch object already embeds the
        tip commit's message and author, so one call suffices (the backend made two)."""
        data = self._http.get_json(
            f"/repos/{ref.owner}/{ref.repo}/branches/{quote(branch, safe='/')}"
        )
        commit = data.get("commit") or {}
        inner = commit.get("commit") or {}
        author = inner.get("author") or {}
        return CommitInfo(
            sha=commit.get("sha") or "",
            message=inner.get("message") or "",
            author=author.get("name") or (commit.get("author") or {}).get("login") or "",
            date=author.get("date") or "",
        )

    def pull_request(self, ref: RepoRef, number: int) -> PullRequestInfo:
        """``GET /repos/{o}/{r}/pulls/{n}``. ``closed`` + ``merged_at`` → ``merged``."""
        pr = self._http.get_json(f"/repos/{ref.owner}/{ref.repo}/pulls/{int(number)}")
        state = PR_STATES.get(str(pr.get("state")), "open")
        if state == "closed" and pr.get("merged_at"):
            state = "merged"
        base, head = pr.get("base") or {}, pr.get("head") or {}
        return PullRequestInfo(
            number=int(pr.get("number") or number),
            title=pr.get("title") or "",
            state=state,
            base_branch=base.get("ref") or "",
            head_branch=head.get("ref") or "",
            base_sha=base.get("sha") or "",
            head_sha=head.get("sha") or "",
            url=pr.get("html_url") or "",
        )

    def post_pr_comment(self, ref: RepoRef, number: int, body: str) -> PrComment:
        """``POST /repos/{o}/{r}/issues/{n}/comments`` — PR conversation comments are issue
        comments on GitHub."""
        data = self._http.post_json(
            f"/repos/{ref.owner}/{ref.repo}/issues/{int(number)}/comments", {"body": body}
        )
        return PrComment(id=str(data.get("id") or ""), url=data.get("html_url") or "")

    def compare(self, ref: RepoRef, base: str, head: str) -> ChangeSet:
        """Three-dot compare, paginated (``files`` is capped at 300 per page)."""
        first = f"/repos/{ref.owner}/{ref.repo}/compare/{base}...{head}"
        out = ChangeSet()
        for resp in self._http.paginate(first, _next_link, {"per_page": "100"}):
            for f in resp.json().get("files") or []:
                name = f.get("filename")
                if not name:
                    continue
                status = f.get("status")
                if status == "removed":
                    out.deleted.append(name)
                    continue
                out.changed.append(name)
                prev = f.get("previous_filename")
                if status == "renamed" and prev and prev != name:
                    out.deleted.append(prev)
        return out

    def file_content(self, ref: RepoRef, path: str, commit: str) -> str:
        return self._http.get_text(
            f"/repos/{ref.owner}/{ref.repo}/contents/{quote(path)}",
            {"ref": commit},
            headers={"Accept": _RAW},
        )

    def clone_url(self, ref: RepoRef) -> str:
        host = ref.host or DEFAULT_HOSTS["github"]
        auth = f"x-access-token:{self._token}@" if self._token else ""
        return f"https://{auth}{host}/{ref.owner}/{ref.repo}.git"


def _next_link(resp: httpx.Response) -> str | None:
    """``Link: <url>; rel="next"`` → url."""
    for part in resp.headers.get("Link", "").split(","):
        segments = [s.strip() for s in part.split(";")]
        if len(segments) == 2 and segments[1] == 'rel="next"':
            return str(segments[0]).strip("<>")
    return None


__all__ = ["GitHubSCMClient"]
