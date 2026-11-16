"""Bitbucket Server / Data Center provider — REST API 1.0 over ``SCMHttpClient``.

A different product from Bitbucket Cloud, not a different host: every path, the
pagination protocol, the credential format and the clone prefix differ, which is why
this is its own provider slug rather than a flavour of ``bitbucket``.

Addressing
    ``{base}/projects/{projectKey}/repos/{repositorySlug}/…`` where ``base`` ends in
    ``/rest/api/1.0``. ``RepoRef.owner`` holds the project key (``~jdoe`` for a personal
    project), ``RepoRef.repo`` the repository slug.

Version
    The API path is pinned to ``1.0`` rather than Data Center's ``latest`` alias, which
    resolves to whatever the installed instance considers current and can therefore shift
    meaning under a customer upgrade.

Credential
    The ``Authorization: Basic <value>`` header value **already base64-encoded**, passed
    through verbatim. Re-encoding it (what Bitbucket Cloud's client does to its
    ``username:api_key``) would double-encode and 401 on every call. Data Center serves no
    public repositories in the deployments this targets, so the credential is required.

Pagination
    Uniform ``?start=&limit=`` with ``isLastPage`` / ``nextPageStart`` in the body — unlike
    Cloud's full-URL ``next`` cursor.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote, urlencode

import httpx

from .base import PR_STATES, AbstractSCMClient, ChangeSet, CommitInfo, FileDiff, PrComment, PullRequestInfo, RepoRef
from .errors import SCMAPIError, SCMCredentialError
from .http import SCMHttpClient

if TYPE_CHECKING:
    from ...config import Settings

logger = logging.getLogger(__name__)

_EMPTY_USERNAME = (
    "Bitbucket Data Center credential has an empty username; supply either a bare HTTP "
    'access token, or "username:password" (raw or base64-encoded).'
)

#: Entries per page. Data Center caps this per endpoint and simply returns fewer with
#: ``isLastPage: false``, so asking high is safe.
_PAGE_LIMIT: Final[str] = "1000"

#: Data Center change types → the normalised vocabulary in ``FileDiff.status``.
#: ``UNKNOWN`` is treated as a modification: re-reading a file that did not change is
#: harmless, while skipping one that did would leave the stored graph stale.
_CHANGE_STATUS: Final[dict[str, str]] = {
    "ADD": "added",
    "MODIFY": "modified",
    "DELETE": "removed",
    "COPY": "copied",
    "MOVE": "renamed",
    "UNKNOWN": "modified",
}


def _iso_8601(epoch_millis: Any) -> str:
    """Data Center's ``authorTimestamp`` (epoch **milliseconds**) as an ISO-8601 string.

    Every other provider hands us an ISO-8601 string that ``CommitInfo.date`` passes
    through untouched; Data Center is the one that needs converting. A value that is not
    a number yields ``""`` rather than a fabricated date.
    """
    if isinstance(epoch_millis, bool) or not isinstance(epoch_millis, (int, float)):
        return ""
    return datetime.fromtimestamp(epoch_millis / 1000, tz=UTC).isoformat()


def _encode_path(path: str) -> str:
    """Percent-encode each segment of a repository path, keeping the separators."""
    return "/".join(quote(segment) for segment in path.split("/"))


class BitbucketServerSCMClient(AbstractSCMClient):
    """Bitbucket Server / Data Center source-acquisition client.

    Args:
        token: The base64 ``username:password`` value for HTTP Basic. Required.
        settings: Supplies the HTTP tuning knobs.
        api_base_url: REST base ending in ``/rest/api/1.0``, including any context path.
            Required — Data Center has no public cloud to fall back to.
        transport: Injectable ``httpx`` transport for tests.
        sleep: Injectable retry wait for tests.

    Raises:
        SCMCredentialError: ``token`` is missing, or is not base64 of ``user:password``
            (400 for the caller).
    """

    provider = "bitbucket_server"
    supports_incremental = True
    requires_credential = True

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
        if not self._token:
            raise SCMCredentialError(
                "Bitbucket Data Center requires a credential; supply it as gitToken on "
                "the request.",
                provider=self.provider,
            )
        self._auth_header, self._username, self._password = self._credential(self._token)
        if not api_base_url:
            raise SCMCredentialError(
                "Bitbucket Data Center requires an API base URL ending in /rest/api/1.0; "
                "none was supplied and the provider has no public-cloud default.",
                provider=self.provider,
            )
        self._http = SCMHttpClient(
            provider="Bitbucket Data Center",
            base_url=api_base_url,
            headers={"Accept": "application/json", "Authorization": self._auth_header},
            settings=settings,
            transport=transport,
            sleep=sleep,
        )

    @staticmethod
    def _credential(token: str) -> tuple[str, str, str]:
        """``(header_value, username, password)`` for a Data Center credential.

        Three forms are accepted, because three are in circulation and rejecting any of
        them strands a real user:

        * a **bare HTTP access token** (``BBDC-…``) — what Data Center's own UI issues
          and what the Breeze web UI asks for. Sent as ``Bearer``; there is no username,
          so cloning uses a header rather than a URL credential.
        * a **raw ``username:password``** — base64-encoded here and sent as ``Basic``.
        * a **pre-encoded base64 ``username:password``** — sent as ``Basic`` verbatim,
          never re-encoded (double-encoding 401s on every call with no obvious cause).

        The forms cannot be confused: a raw ``user:token`` contains ``:``, which is not
        in the base64 alphabet, and a ``BBDC-…`` token contains ``-``, which is not
        either — so only one branch can ever match.

        Raises:
            SCMCredentialError: the decoded credential has an empty username. It would
                authenticate as nobody, and the clone-URL scrubber cannot mask a
                ``//:secret@`` pattern, so the password would reach the logs.
        """
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            decoded = ""
        if decoded and ":" in decoded:
            username, _, password = decoded.partition(":")
            if not username:
                raise SCMCredentialError(_EMPTY_USERNAME, provider="bitbucket_server")
            return f"Basic {token}", username, password
        if ":" in token:
            username, _, password = token.partition(":")
            if not username:
                raise SCMCredentialError(_EMPTY_USERNAME, provider="bitbucket_server")
            encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
            return f"Basic {encoded}", username, password
        return f"Bearer {token}", "", token

    def git_auth_config(self) -> list[str]:
        """``git -c`` arguments carrying this client's credential, or ``[]``.

        A ``Basic`` credential is embedded in ``clone_url`` as userinfo, so nothing is
        needed here. A bare token has no username to put in a URL, so it travels as an
        ``Authorization`` header instead. The value lands in the git process's argv, so
        ``server/git.py`` scrubs it out of any error text.
        """
        if self._auth_header.startswith("Bearer "):
            return ["-c", f"http.extraHeader=Authorization: {self._auth_header}"]
        return []

    def close(self) -> None:
        self._http.close()

    # ── Addressing ─────────────────────────────────────────────────────────

    @staticmethod
    def _repo_path(ref: RepoRef) -> str:
        """``/projects/{key}/repos/{slug}``. ``~`` in a personal project key is a safe
        path character and is deliberately left unescaped."""
        return f"/projects/{quote(ref.owner, safe='~')}/repos/{quote(ref.repo, safe='')}"

    def _pages(self, path: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        """Yield each page body of a Data Center paged endpoint.

        ``isLastPage`` absent is treated as "last": a body we cannot read must stop the
        walk rather than loop. The ceiling in ``SCMHttpClient.paginate`` is the backstop.
        """
        url = self._http.url_for(path)

        def body_of(resp: httpx.Response) -> dict[str, Any] | None:
            """The page body, or ``None`` when it is not a readable paged object.

            A Data Center behind SSO can answer a login page with HTTP 200; decoding
            that as JSON raises, which would surface as a 500 (our fault) instead of a
            502 naming the provider.
            """
            try:
                body = resp.json()
            except ValueError:
                raise SCMAPIError(
                    f"Bitbucket Data Center GET {path} returned a non-JSON body "
                    "(an SSO login page is the usual cause)",
                    http_status=resp.status_code, provider=self.provider,
                    operation=f"GET {path}",
                ) from None
            return body if isinstance(body, dict) else None

        def next_of(resp: httpx.Response) -> str | None:
            body = body_of(resp)
            if body is None or body.get("isLastPage", True):
                return None
            start = body.get("nextPageStart")
            if start is None:
                return None
            return f"{url}?{urlencode({**params, 'start': str(start)})}"

        for resp in self._http.paginate(path, next_of, params):
            body = body_of(resp)
            if body is None:
                raise SCMAPIError(
                    f"Bitbucket Data Center GET {path} did not return a paged object",
                    provider=self.provider, operation=f"GET {path}",
                )
            yield body

    # ── AbstractSCMClient ──────────────────────────────────────────────────

    def tree(self, ref: RepoRef, commit: str) -> list[str]:
        """``GET {repo}/files?at={commit}`` — every blob path, recursively.

        Data Center returns ``values`` as bare path strings. A non-string entry is an
        error rather than a skip: silently returning a short tree would hand the scanner
        an incomplete repository shape and the result would look successful.
        """
        path = f"{self._repo_path(ref)}/files"
        paths: list[str] = []
        for body in self._pages(path, {"at": commit, "limit": _PAGE_LIMIT}):
            for entry in body.get("values") or []:
                if not isinstance(entry, str):
                    raise SCMAPIError(
                        f"Bitbucket Data Center GET {path} returned a non-string tree "
                        f"entry ({type(entry).__name__})",
                        provider=self.provider, operation=f"GET {path}",
                    )
                if entry:
                    paths.append(entry)
        return paths

    def branch_head(self, ref: RepoRef, branch: str) -> CommitInfo:
        """``GET {repo}/commits?until=refs/heads/{branch}&limit=1`` — the tip commit.

        A branch that does not exist is a 404 from Data Center, which the HTTP layer
        turns into ``SCMAPIError``; an empty ``values`` list means the branch resolved
        but carries no commits, which is also an error for the caller's purposes.
        """
        path = f"{self._repo_path(ref)}/commits"
        data = self._http.get_json(
            path, {"until": f"refs/heads/{branch}", "limit": "1"}
        )
        values = (data or {}).get("values") or []
        if not values:
            raise SCMAPIError(
                f"Bitbucket Data Center GET {path} returned no commits for branch "
                f"{branch!r}",
                http_status=404, provider=self.provider, operation=f"GET {path}",
            )
        commit = values[0]
        author = commit.get("author") or {}
        return CommitInfo(
            sha=commit.get("id") or "",
            message=commit.get("message") or "",
            author=author.get("name") or author.get("emailAddress") or "",
            date=_iso_8601(commit.get("authorTimestamp")),
        )

    def pull_request(self, ref: RepoRef, number: int) -> PullRequestInfo:
        """``GET {repo}/pull-requests/{id}``.

        ``fromRef`` / ``toRef`` carry ``latestCommit`` as a **full** SHA, so unlike
        Bitbucket Cloud no second call is needed to expand an abbreviated hash.
        """
        pr = self._http.get_json(f"{self._repo_path(ref)}/pull-requests/{int(number)}")
        src, dst = pr.get("fromRef") or {}, pr.get("toRef") or {}
        links = (pr.get("links") or {}).get("self") or []
        return PullRequestInfo(
            number=int(pr.get("id") or number),
            title=pr.get("title") or "",
            state=PR_STATES.get(str(pr.get("state")), "open"),
            base_branch=dst.get("displayId") or "",
            head_branch=src.get("displayId") or "",
            base_sha=dst.get("latestCommit") or "",
            head_sha=src.get("latestCommit") or "",
            url=(links[0].get("href") if links and isinstance(links[0], dict) else "") or "",
        )

    def post_pr_comment(self, ref: RepoRef, number: int, body: str) -> PrComment:
        """``POST {repo}/pull-requests/{id}/comments`` with ``{"text": …}`` — Data
        Center's shape, not Cloud's ``{"content": {"raw": …}}``."""
        data = self._http.post_json(
            f"{self._repo_path(ref)}/pull-requests/{int(number)}/comments",
            {"text": body},
        )
        links = (data.get("links") or {}).get("self") or []
        return PrComment(
            id=str(data.get("id") or ""),
            url=(links[0].get("href") if links and isinstance(links[0], dict) else "") or "",
        )

    def compare(self, ref: RepoRef, base: str, head: str) -> ChangeSet:
        """``GET {repo}/compare/changes?from={head}&to={base}``.

        **The spec order is reversed** relative to GitHub's ``base...head``, the same way
        Bitbucket Cloud's ``diffstat/{head}..{base}`` is. Sending ``from=base&to=head``
        returns an empty page rather than an error, so the mistake does not surface as a
        failure — it surfaces as a repository that never appears to change. Verified
        against a live instance: for one merged pull request, ``from=base&to=head`` gave
        0 entries while ``from=head&to=base`` gave the same 9 the provider's own
        ``/pull-requests/{id}/changes`` reports.

        Data Center reports neither line counts nor diff text here, so ``additions`` /
        ``deletions`` stay ``0`` and ``patch`` stays ``None`` rather than carrying a
        guessed number — the same honest-null position Bitbucket Cloud's ``diffstat``
        already takes for ``patch``.

        A ``MOVE`` names the destination in ``path`` and the source in ``srcPath``, so a
        rename contributes its new path to ``changed`` and its old path to ``deleted``.
        """
        path = f"{self._repo_path(ref)}/compare/changes"
        out = ChangeSet()
        for body in self._pages(path, {"from": head, "to": base, "limit": _PAGE_LIMIT}):
            for entry in body.get("values") or []:
                new_path = ((entry.get("path") or {}).get("toString") or "").strip()
                old_path = ((entry.get("srcPath") or {}).get("toString") or "").strip()
                raw_type = str(entry.get("type") or "UNKNOWN").upper()
                if not new_path:
                    raise SCMAPIError(
                        f"Bitbucket Data Center GET {path} returned a change with no "
                        "path.toString",
                        provider=self.provider, operation=f"GET {path}",
                    )
                status = _CHANGE_STATUS.get(raw_type)
                if status is None:
                    logger.debug(
                        "Unknown Bitbucket Data Center change type %r on %s; treating "
                        "it as a modification.", raw_type, new_path,
                    )
                    status = "modified"
                out.files.append(FileDiff(filename=new_path, status=status))
                if status == "removed":
                    out.deleted.append(new_path)
                    continue
                out.changed.append(new_path)
                if status == "renamed" and old_path and old_path != new_path:
                    out.deleted.append(old_path)
        return out

    def file_content(self, ref: RepoRef, path: str, commit: str) -> str:
        """``GET {repo}/raw/{path}?at={commit}`` — the file's bytes, decoded strictly."""
        return self._http.get_text(
            f"{self._repo_path(ref)}/raw/{_encode_path(path)}", {"at": commit}
        )

    def clone_url(self, ref: RepoRef) -> str:
        """``https://user:password@host[/context]/scm/{key}/{slug}.git``.

        The clone prefix is ``/scm``, a different path from the REST base, and both sit
        under the instance's context path. Both credential halves are percent-encoded:
        Data Center passwords and HTTP access tokens routinely contain ``@``, ``/`` and
        ``:``, which would otherwise split the URL in the wrong place.
        """
        # A bare token authenticates through git_auth_config's header instead: there is
        # no username, and "//:token@" is a shape the clone-URL scrubber cannot mask.
        auth = (
            f"{quote(self._username, safe='')}:{quote(self._password, safe='')}@"
            if self._username else ""
        )
        return (
            f"{ref.origin.replace('//', f'//{auth}', 1)}{ref.context_path}"
            f"/scm/{quote(ref.owner, safe='~')}/{quote(ref.repo, safe='')}.git"
        )


__all__ = ["BitbucketServerSCMClient"]
