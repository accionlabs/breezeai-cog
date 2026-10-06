"""Bitbucket Cloud provider — REST API 2.0 over ``SCMHttpClient``.

Credential format is ``username:api_key`` (Basic auth for the API). The clone URL
uses Bitbucket's ``x-bitbucket-api-token-auth`` pseudo-user with the key alone, which
is why the username half is validated but then discarded for cloning.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from .base import DEFAULT_HOSTS, PR_STATES, AbstractSCMClient, ChangeSet, CommitInfo, PrComment, PullRequestInfo, RepoRef
from .errors import SCMCredentialError
from .http import SCMHttpClient, warn_if_anonymous

if TYPE_CHECKING:
    from ...config import Settings

_CREDENTIAL_FORMAT = (
    'Bitbucket credential must be in "username:api_key" format (API key via Basic auth).'
)


def _next_of(resp: httpx.Response) -> str | None:
    """Bitbucket pages carry a full ``next`` URL in the body."""
    nxt = resp.json().get("next")
    return str(nxt) if nxt else None


class BitbucketSCMClient(AbstractSCMClient):
    """Bitbucket Cloud source-acquisition client.

    Args:
        token: ``username:api_key``; ``None`` for anonymous.
        settings: Supplies the API base and the HTTP tuning knobs.
        api_base_url: Per-instance override of ``settings.bitbucket_api_base_url``.
        transport: Injectable ``httpx`` transport for tests.
        sleep: Injectable retry wait for tests.

    Raises:
        SCMCredentialError: when ``token`` is set but has no ``:`` (400 for the caller).
    """

    provider = "bitbucket"
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
        self._api_key: str | None = None
        warn_if_anonymous("Bitbucket", self._token)
        headers = {"Accept": "application/json"}
        if self._token:
            if ":" not in self._token:
                raise SCMCredentialError(_CREDENTIAL_FORMAT, provider="bitbucket")
            _user, _, self._api_key = self._token.partition(":")
            headers["Authorization"] = (
                "Basic " + base64.b64encode(self._token.encode()).decode()
            )
        self._http = SCMHttpClient(
            provider="Bitbucket",
            base_url=api_base_url or settings.bitbucket_api_base_url,
            headers=headers,
            settings=settings,
            transport=transport,
            sleep=sleep,
        )

    def close(self) -> None:
        self._http.close()

    # ── AbstractSCMClient ──────────────────────────────────────────────────

    def tree(self, ref: RepoRef, commit: str) -> list[str]:
        first = f"/repositories/{ref.owner}/{ref.repo}/src/{commit}/"
        paths: list[str] = []
        for resp in self._http.paginate(first, _next_of, {"pagelen": "100", "max_depth": "100"}):
            for entry in resp.json().get("values") or []:
                if entry.get("type") == "commit_file" and entry.get("path"):
                    paths.append(entry["path"])
        return paths

    def branch_head(self, ref: RepoRef, branch: str) -> CommitInfo:
        """``GET /repositories/{o}/{r}/refs/branches/{branch}`` → ``target`` is the tip."""
        data = self._http.get_json(
            f"/repositories/{ref.owner}/{ref.repo}/refs/branches/{quote(branch, safe='/')}"
        )
        target = data.get("target") or {}
        author = target.get("author") or {}
        return CommitInfo(
            sha=target.get("hash") or "",
            message=target.get("message") or "",
            author=author.get("raw") or (author.get("user") or {}).get("display_name") or "",
            date=target.get("date") or "",
        )

    def pull_request(self, ref: RepoRef, number: int) -> PullRequestInfo:
        """``GET /repositories/{o}/{r}/pullrequests/{id}``.

        The PR object carries **abbreviated** commit hashes under ``source.commit`` and
        ``destination.commit``; each is resolved to the full SHA through ``/commit/{hash}``
        so callers can store and compare them like every other provider's.
        """
        base_path = f"/repositories/{ref.owner}/{ref.repo}"
        pr = self._http.get_json(f"{base_path}/pullrequests/{int(number)}")
        src, dst = pr.get("source") or {}, pr.get("destination") or {}
        return PullRequestInfo(
            number=int(pr.get("id") or number),
            title=pr.get("title") or "",
            state=PR_STATES.get(str(pr.get("state")), "open"),
            base_branch=(dst.get("branch") or {}).get("name") or "",
            head_branch=(src.get("branch") or {}).get("name") or "",
            base_sha=self._full_sha(base_path, (dst.get("commit") or {}).get("hash") or ""),
            head_sha=self._full_sha(base_path, (src.get("commit") or {}).get("hash") or ""),
            url=((pr.get("links") or {}).get("html") or {}).get("href") or "",
        )

    def _full_sha(self, base_path: str, sha: str) -> str:
        if not sha or len(sha) >= 40:
            return sha
        commit = self._http.get_json(f"{base_path}/commit/{sha}")
        return str(commit.get("hash") or sha)

    def post_pr_comment(self, ref: RepoRef, number: int, body: str) -> PrComment:
        """``POST /repositories/{o}/{r}/pullrequests/{id}/comments`` with ``{content: {raw}}``."""
        data = self._http.post_json(
            f"/repositories/{ref.owner}/{ref.repo}/pullrequests/{int(number)}/comments",
            {"content": {"raw": body}},
        )
        return PrComment(
            id=str(data.get("id") or ""),
            url=((data.get("links") or {}).get("html") or {}).get("href") or "",
        )

    def compare(self, ref: RepoRef, base: str, head: str) -> ChangeSet:
        """``diffstat/{head}..{base}`` — Bitbucket's spec order is reversed relative
        to GitHub's ``base...head``; the old helper used this order and it is kept."""
        first = f"/repositories/{ref.owner}/{ref.repo}/diffstat/{head}..{base}"
        out = ChangeSet()
        for resp in self._http.paginate(first, _next_of, {"pagelen": "100"}):
            for entry in resp.json().get("values") or []:
                new_path = (entry.get("new") or {}).get("path")
                old_path = (entry.get("old") or {}).get("path")
                status = entry.get("status")
                if status == "removed" and old_path:
                    out.deleted.append(old_path)
                elif new_path:
                    out.changed.append(new_path)
                    if status == "renamed" and old_path and old_path != new_path:
                        out.deleted.append(old_path)
        return out

    def file_content(self, ref: RepoRef, path: str, commit: str) -> str:
        encoded = "/".join(quote(p) for p in path.split("/"))
        return self._http.get_text(
            f"/repositories/{ref.owner}/{ref.repo}/src/{commit}/{encoded}"
        )

    def clone_url(self, ref: RepoRef) -> str:
        host = ref.host or DEFAULT_HOSTS["bitbucket"]
        auth = f"x-bitbucket-api-token-auth:{self._api_key}@" if self._api_key else ""
        return f"https://{auth}{host}/{ref.owner}/{ref.repo}.git"


__all__ = ["BitbucketSCMClient"]
