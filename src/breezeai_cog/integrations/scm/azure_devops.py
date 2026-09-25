"""Azure DevOps provider — Git REST API 7.1 over ``SCMHttpClient``.

Replaces the stubs that forced every Azure repository onto a full clone. Verified
against the 7.1 reference (Items – List, Diffs – Get, Items – Get):

* **Tree**: ``GET …/items?recursionLevel=full&versionDescriptor.version=<sha>
  &versionDescriptor.versionType=commit`` — one unpaginated ``{count, value}`` list;
  entries carry ``gitObjectType`` (``blob``/``tree``) and a **leading-slash** ``path``.
* **Compare**: ``GET …/diffs/commits?baseVersion=&baseVersionType=commit
  &targetVersion=&targetVersionType=commit&$top=&$skip=`` — paged by ``$skip``;
  ``allChangesIncluded`` says whether the page completed the set. ``changeType`` is a
  flags enum rendered as text (``add``, ``edit``, ``delete``, ``rename``, or a
  combination such as ``edit, rename``); a rename's old path is ``sourceServerItem``.
* **Content**: ``GET …/items?path=<path>&versionDescriptor…&download=true`` with an
  octet-stream ``Accept``, decoded strictly as UTF-8 by the shared wrapper.

Repository base: ``{api_base}/{org}/{project}/_apis/git/repositories/{repo}``. Auth is a
PAT via Basic with an empty username. The clone URL follows ``RepoRef.host``, so
``dev.azure.com`` input no longer produces a ``visualstudio.com`` clone URL.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import httpx

from .base import DEFAULT_HOSTS, PR_STATES, AbstractSCMClient, ChangeSet, CommitInfo, PrComment, PullRequestInfo, RepoRef
from .errors import SCMAPIError
from .http import SCMHttpClient, warn_if_anonymous

if TYPE_CHECKING:
    from ...config import Settings

_API_VERSION = "7.1"
#: Changes per ``diffs/commits`` page. The service default is 100; we ask for more to
#: keep round trips down on large merges.
_DIFF_PAGE = 500


def _commit_descriptor(commit: str) -> dict[str, str]:
    return {
        "versionDescriptor.version": commit,
        "versionDescriptor.versionType": "commit",
    }


def _strip_slash(path: str) -> str:
    return path[1:] if path.startswith("/") else path


def _strip_ref(name: str) -> str:
    return name[len("refs/heads/"):] if name.startswith("refs/heads/") else name


class AzureDevOpsSCMClient(AbstractSCMClient):
    """Azure DevOps Services / Server source-acquisition client.

    Args:
        token: Personal access token with ``Code (read)``; ``None`` for anonymous
            (public projects only).
        settings: Supplies the API base and the HTTP tuning knobs.
        api_base_url: Per-instance override of ``settings.azure_devops_api_base_url``
            (an Azure DevOps Server host, e.g. ``https://ado.acme.com``).
        transport: Injectable ``httpx`` transport for tests.
        sleep: Injectable retry wait for tests.
    """

    provider = "azure_devops"
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
        warn_if_anonymous("AzureDevOps", self._token)
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = (
                "Basic " + base64.b64encode(f":{self._token}".encode()).decode()
            )
        self._http = SCMHttpClient(
            provider="AzureDevOps",
            base_url=api_base_url or settings.azure_devops_api_base_url,
            headers=headers,
            settings=settings,
            transport=transport,
            sleep=sleep,
        )

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _repo_path(ref: RepoRef) -> str:
        """``/{org}/{project}/_apis/git/repositories/{repo}`` — ``org`` is the
        collection on Azure DevOps Server, which the URL parser stores in ``owner``."""
        return (
            f"/{quote(ref.owner, safe='')}/{quote(ref.project, safe='')}"
            f"/_apis/git/repositories/{quote(ref.repo, safe='')}"
        )

    # ── AbstractSCMClient ──────────────────────────────────────────────────

    def tree(self, ref: RepoRef, commit: str) -> list[str]:
        data = self._http.get_json(
            f"{self._repo_path(ref)}/items",
            {"recursionLevel": "full", **_commit_descriptor(commit), "api-version": _API_VERSION},
        )
        return [
            _strip_slash(item["path"])
            for item in (data.get("value") or [])
            if item.get("gitObjectType") == "blob" and not item.get("isFolder") and item.get("path")
        ]

    def branch_head(self, ref: RepoRef, branch: str) -> CommitInfo:
        """``refs?filter=heads/{branch}`` then ``commits/{objectId}``.

        ``filter`` is a **starts-with** match (Refs–List), so ``heads/main`` also returns
        ``refs/heads/main-old``; the exact ``refs/heads/{branch}`` entry is selected by name
        rather than taking ``value[0]`` as the backend did.
        """
        base = self._repo_path(ref)
        refs = self._http.get_json(
            f"{base}/refs", {"filter": f"heads/{branch}", "api-version": _API_VERSION}
        )
        wanted = f"refs/heads/{branch}"
        object_id = next(
            (r.get("objectId") for r in (refs.get("value") or []) if r.get("name") == wanted),
            None,
        )
        if not object_id:
            raise SCMAPIError(
                f"AzureDevOps branch {branch!r} not found in {ref.owner}/{ref.project}/{ref.repo}",
                http_status=404, provider="AzureDevOps", operation=f"GET {base}/refs",
            )
        commit = self._http.get_json(
            f"{base}/commits/{quote(object_id, safe='')}", {"api-version": _API_VERSION}
        )
        author = commit.get("author") or {}
        return CommitInfo(
            sha=commit.get("commitId") or object_id,
            message=commit.get("comment") or "",
            author=author.get("name") or "",
            date=author.get("date") or "",
        )

    def pull_request(self, ref: RepoRef, number: int) -> PullRequestInfo:
        """``GET {repo}/pullrequests/{id}`` (Pull Requests – Get Pull Request, 7.1).

        Direction, per the reference: ``lastMergeSourceCommit`` is *the commit at the head
        of the source branch* → **head**; ``lastMergeTargetCommit`` is *the head of the
        target branch* → **base**. (The backend's own client had these swapped.)
        ``sourceRefName`` / ``targetRefName`` arrive as ``refs/heads/<branch>``.
        """
        pr = self._http.get_json(
            f"{self._repo_path(ref)}/pullrequests/{int(number)}", {"api-version": _API_VERSION}
        )
        src = pr.get("lastMergeSourceCommit") or {}
        dst = pr.get("lastMergeTargetCommit") or {}
        web = ((pr.get("_links") or {}).get("web") or {}).get("href") or ""
        return PullRequestInfo(
            number=int(pr.get("pullRequestId") or number),
            title=pr.get("title") or "",
            state=PR_STATES.get(str(pr.get("status")), "open"),
            base_branch=_strip_ref(pr.get("targetRefName") or ""),
            head_branch=_strip_ref(pr.get("sourceRefName") or ""),
            base_sha=dst.get("commitId") or "",
            head_sha=src.get("commitId") or "",
            url=web,
        )

    def post_pr_comment(self, ref: RepoRef, number: int, body: str) -> PrComment:
        """``POST {repo}/pullrequests/{id}/threads`` — Azure has no flat comment endpoint; a
        comment is the first entry of a new thread (``commentType`` 1 = text, ``status`` 1 =
        active). The backend used to POST ``{body}`` here, which Azure rejects."""
        data = self._http.post_json(
            f"{self._repo_path(ref)}/pullrequests/{int(number)}/threads",
            {
                "comments": [{"parentCommentId": 0, "content": body, "commentType": 1}],
                "status": 1,
            },
            {"api-version": _API_VERSION},
        )
        comments = data.get("comments") or [{}]
        return PrComment(
            id=str(data.get("id") or ""),
            url=((data.get("_links") or {}).get("self") or {}).get("href")
            or (comments[0].get("_links") or {}).get("self", {}).get("href") or "",
        )

    def compare(self, ref: RepoRef, base: str, head: str) -> ChangeSet:
        path = f"{self._repo_path(ref)}/diffs/commits"
        fixed = {
            "baseVersion": base,
            "baseVersionType": "commit",
            "targetVersion": head,
            "targetVersionType": "commit",
            "$top": str(_DIFF_PAGE),
            "api-version": _API_VERSION,
        }
        skip = 0

        def next_of(resp: httpx.Response) -> str | None:
            nonlocal skip
            body = resp.json()
            changes = body.get("changes") or []
            if body.get("allChangesIncluded") or len(changes) < _DIFF_PAGE:
                return None
            skip += _DIFF_PAGE
            return f"{self._http.url_for(path)}?{urlencode({**fixed, '$skip': str(skip)})}"

        out = ChangeSet()
        for resp in self._http.paginate(path, next_of, fixed):
            for change in resp.json().get("changes") or []:
                self._apply_change(change, out)
        return out

    @staticmethod
    def _apply_change(change: dict[str, Any], out: ChangeSet) -> None:
        item = change.get("item") or {}
        if item.get("isFolder") or item.get("gitObjectType") == "tree":
            return
        new_path = _strip_slash(item.get("path") or "")
        if not new_path:
            return
        kinds = {k.strip() for k in str(change.get("changeType") or "").lower().split(",")}
        if "delete" in kinds:
            out.deleted.append(new_path)
            return
        out.changed.append(new_path)
        if "rename" in kinds:
            old_path = _strip_slash(change.get("sourceServerItem") or change.get("originalPath") or "")
            if old_path and old_path != new_path:
                out.deleted.append(old_path)

    def file_content(self, ref: RepoRef, path: str, commit: str) -> str:
        return self._http.get_text(
            f"{self._repo_path(ref)}/items",
            {
                "path": "/" + _strip_slash(path),
                **_commit_descriptor(commit),
                "download": "true",
                "$format": "octetStream",
                "api-version": _API_VERSION,
            },
            headers={"Accept": "application/octet-stream"},
        )

    def clone_url(self, ref: RepoRef) -> str:
        auth = f"pat:{self._token}@" if self._token else ""
        host = ref.host or DEFAULT_HOSTS["azure_devops"]
        if host.endswith(".visualstudio.com"):
            # Legacy per-organisation host: the org is the subdomain, not a path segment.
            return f"https://{auth}{host}/{ref.project}/_git/{ref.repo}"
        return f"https://{auth}{host}/{ref.owner}/{ref.project}/_git/{ref.repo}"


__all__ = ["AzureDevOpsSCMClient"]
