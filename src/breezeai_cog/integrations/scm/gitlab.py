"""GitLab provider — REST API v4 over ``SCMHttpClient``.

Works against gitlab.com and self-hosted GitLab: the API base comes from
``settings.gitlab_api_base_url`` unless the factory passes a per-instance override.
A project is addressed by its URL-encoded full path (``group%2Fsubgroup%2Fproject``),
which is why ``RepoRef.owner`` keeps the whole namespace. Token needs ``read_api``.
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


class GitLabSCMClient(AbstractSCMClient):
    """GitLab source-acquisition client.

    Args:
        token: Personal / project / group access token; ``None`` for anonymous.
        settings: Supplies the API base and the HTTP tuning knobs.
        api_base_url: Per-instance override of ``settings.gitlab_api_base_url``.
        transport: Injectable ``httpx`` transport for tests.
        sleep: Injectable retry wait for tests.
    """

    provider = "gitlab"
    # The tree / compare / raw-file calls are complete. Whether the orchestration
    # actually takes the incremental path for GitLab is decided where the gate lives
    # (server/git.py), not here.
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
        warn_if_anonymous("GitLab", self._token)
        headers = {"Accept": "application/json"}
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token
        self._http = SCMHttpClient(
            provider="GitLab",
            base_url=api_base_url or settings.gitlab_api_base_url,
            headers=headers,
            settings=settings,
            transport=transport,
            sleep=sleep,
        )

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _project(ref: RepoRef) -> str:
        return quote(f"{ref.owner}/{ref.repo}", safe="")

    # ── AbstractSCMClient ──────────────────────────────────────────────────

    def tree(self, ref: RepoRef, commit: str) -> list[str]:
        """Recursive tree, paginated by page number via the ``X-Next-Page`` header."""
        path = f"/projects/{self._project(ref)}/repository/tree"
        base_params = {"recursive": "true", "per_page": "100", "ref": commit}

        def next_of(resp: httpx.Response) -> str | None:
            nxt = resp.headers.get("x-next-page")
            if not nxt:
                return None
            url = self._http.url_for(path)
            query = "&".join(f"{k}={quote(v, safe='')}" for k, v in base_params.items())
            return f"{url}?{query}&page={nxt}"

        paths: list[str] = []
        for resp in self._http.paginate(path, next_of, base_params):
            for entry in resp.json():
                if entry.get("type") == "blob" and entry.get("path"):
                    paths.append(entry["path"])
        return paths

    def branch_head(self, ref: RepoRef, branch: str) -> CommitInfo:
        """``GET /projects/{p}/repository/branches/{branch}`` (branch fully URL-encoded,
        so ``feature/x`` is one path segment)."""
        data = self._http.get_json(
            f"/projects/{self._project(ref)}/repository/branches/{quote(branch, safe='')}"
        )
        commit = data.get("commit") or {}
        return CommitInfo(
            sha=commit.get("id") or "",
            message=commit.get("message") or "",
            author=commit.get("author_name") or "",
            date=commit.get("committed_date") or commit.get("authored_date") or "",
        )

    def pull_request(self, ref: RepoRef, number: int) -> PullRequestInfo:
        """``GET /projects/{p}/merge_requests/{iid}``.

        ``diff_refs.base_sha`` / ``head_sha`` are the SHAs the MR was last diffed against;
        GitLab omits ``diff_refs`` on some MRs (e.g. before the first diff is computed), in
        which case the head is ``sha`` and the base is resolved from the **target branch's
        tip** via ``branch_head`` — a SHA, never the branch name.
        """
        mr = self._http.get_json(f"/projects/{self._project(ref)}/merge_requests/{int(number)}")
        refs = mr.get("diff_refs") or {}
        target = mr.get("target_branch") or ""
        head_sha = refs.get("head_sha") or mr.get("sha") or ""
        base_sha = refs.get("base_sha") or ""
        if not base_sha and target:
            base_sha = self.branch_head(ref, target).sha
        return PullRequestInfo(
            number=int(mr.get("iid") or number),
            title=mr.get("title") or "",
            state=PR_STATES.get(str(mr.get("state")), "open"),
            base_branch=target,
            head_branch=mr.get("source_branch") or "",
            base_sha=base_sha,
            head_sha=head_sha,
            url=mr.get("web_url") or "",
        )

    def post_pr_comment(self, ref: RepoRef, number: int, body: str) -> PrComment:
        """``POST /projects/{p}/merge_requests/{iid}/notes`` with ``{body}``. GitLab returns
        no web URL for a note; ``url`` is left empty."""
        data = self._http.post_json(
            f"/projects/{self._project(ref)}/merge_requests/{int(number)}/notes", {"body": body}
        )
        return PrComment(id=str(data.get("id") or ""))

    def compare(self, ref: RepoRef, base: str, head: str) -> ChangeSet:
        data = self._http.get_json(
            f"/projects/{self._project(ref)}/repository/compare",
            {"from": base, "to": head},
        )
        out = ChangeSet()
        for d in data.get("diffs") or []:
            old_path, new_path = d.get("old_path"), d.get("new_path")
            if d.get("deleted_file"):
                if old_path:
                    out.deleted.append(old_path)
            elif new_path:
                out.changed.append(new_path)
                if d.get("renamed_file") and old_path and old_path != new_path:
                    out.deleted.append(old_path)
        return out

    def file_content(self, ref: RepoRef, path: str, commit: str) -> str:
        return self._http.get_text(
            f"/projects/{self._project(ref)}/repository/files/{quote(path, safe='')}/raw",
            {"ref": commit},
        )

    def clone_url(self, ref: RepoRef) -> str:
        host = ref.host or DEFAULT_HOSTS["gitlab"]
        auth = f"oauth2:{self._token}@" if self._token else ""
        return f"https://{auth}{host}/{ref.owner}/{ref.repo}.git"


__all__ = ["GitLabSCMClient"]
