"""Git source acquisition for ``/api/analyze-diff`` (server-only). Port of the
``server.js`` provider/clone/diff helpers: first-time analysis does a full ``git clone``
(avoids per-file API rate limits); incremental analysis pulls only the changed files via
the provider REST API (GitHub or Bitbucket). Returns a populated temp dir + the changed-
file filter set + the deleted-file list.

The returned temp dir is the caller's to clean up after streaming."""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..config import Settings
from .errors import ApiError

logger = logging.getLogger(__name__)

_GITHUB = re.compile(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$")
_BITBUCKET = re.compile(r"bitbucket\.org/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$")
_GITLAB = re.compile(r"gitlab\.com/(.+)$")
_AZURE_DEVOPS = re.compile(
    r"(?:dev\.azure\.com/([^/]+)/([^/]+)|([a-zA-Z0-9-]+)\.visualstudio\.com/([^/]+)(?:/([^/]+))?)/_git/([^/]+)"
)

def parse_repo_url(repo_url: str) -> dict[str, str] | None:
    gh = _GITHUB.search(repo_url)
    if gh:
        return {"provider": "github", "owner": gh.group(1), "repo": gh.group(2)}
    bb = _BITBUCKET.search(repo_url)
    if bb:
        return {"provider": "bitbucket", "owner": bb.group(1), "repo": bb.group(2)}
    gl = _GITLAB.search(repo_url)
    if gl:
        # GitLab namespaces can nest (group/subgroup/repo). Keep the whole path;
        # the repo boundary is marked by "/-/" in web URLs. owner holds the
        # namespace and repo the project, so f"{owner}/{repo}" is the full path.
        raw = gl.group(1).split("?")[0].split("#")[0].split("/-/")[0].strip("/")
        if raw.endswith(".git"):
            raw = raw[:-4]
        segments = [s for s in raw.split("/") if s]
        if len(segments) < 2:
            return None
        return {"provider": "gitlab", "owner": "/".join(segments[:-1]), "repo": segments[-1]}
    az = _AZURE_DEVOPS.search(repo_url)
    if az:
        repo_name = az.group(6)
        if az.group(1):  # dev.azure.com format
            owner = az.group(1)
            project = az.group(2)
        else:  # visualstudio.com format
            owner = az.group(3)
            project = az.group(4)
        return {"provider": "azure_devops", "owner": owner, "project": project, "repo": repo_name}
    return None

def _scrub(s: str) -> str:
    return re.sub(r"//[^/@\s]+:[^/@\s]+@", "//***:***@", str(s or ""))


# --- GitHub ---

def _github_api(endpoint: str, token: str | None) -> Any:
    import httpx

    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(f"https://api.github.com{endpoint}", headers=headers, timeout=60.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"GitHub API {resp.status_code}: {resp.text}")
    return resp.json()


def _gh_tree(owner: str, repo: str, commit: str, token: str | None) -> list[str]:
    tree = _github_api(f"/repos/{owner}/{repo}/git/trees/{commit}?recursive=1", token)
    return [e["path"] for e in (tree.get("tree") or []) if e.get("type") == "blob"]


def _gh_compare(owner: str, repo: str, base: str, head: str, token: str | None) -> dict[str, list[str]]:
    cmp = _github_api(f"/repos/{owner}/{repo}/compare/{base}...{head}", token)
    files = cmp.get("files") or []
    return {
        "deleted": [f["filename"] for f in files if f.get("status") == "removed"],
        "changed": [f["filename"] for f in files if f.get("status") != "removed"],
    }


def _gh_content(owner: str, repo: str, path: str, commit: str, token: str | None) -> str:
    from urllib.parse import quote

    data = _github_api(f"/repos/{owner}/{repo}/contents/{quote(path)}?ref={commit}", token)
    return base64.b64decode(data["content"]).decode("utf-8")


# --- Bitbucket ---

def _bitbucket_auth(credential: str | None) -> str | None:
    if not credential:
        return None
    if ":" not in credential:
        raise ApiError('Bitbucket credential must be in "username:api_key" format (API key via Basic auth).', 400)
    return "Basic " + base64.b64encode(credential.encode()).decode()


def _bitbucket_api(endpoint_or_url: str, token: str | None) -> Any:
    import httpx

    headers = {"Accept": "application/json"}
    auth = _bitbucket_auth(token)
    if auth:
        headers["Authorization"] = auth
    url = endpoint_or_url if endpoint_or_url.startswith("http") else f"https://api.bitbucket.org{endpoint_or_url}"
    resp = httpx.get(url, headers=headers, timeout=60.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"Bitbucket API {resp.status_code}: {resp.text}")
    return resp.json()


def _bb_tree(owner: str, repo: str, commit: str, token: str | None) -> list[str]:
    paths: list[str] = []
    nxt: str | None = f"/2.0/repositories/{owner}/{repo}/src/{commit}/?pagelen=100&max_depth=100"
    while nxt:
        page = _bitbucket_api(nxt, token)
        for entry in page.get("values") or []:
            if entry.get("type") == "commit_file" and entry.get("path"):
                paths.append(entry["path"])
        nxt = page.get("next")
    return paths


def _bb_compare(owner: str, repo: str, base: str, head: str, token: str | None) -> dict[str, list[str]]:
    deleted, changed = [], []
    nxt: str | None = f"/2.0/repositories/{owner}/{repo}/diffstat/{head}..{base}?pagelen=100"
    while nxt:
        page = _bitbucket_api(nxt, token)
        for entry in page.get("values") or []:
            new_path = (entry.get("new") or {}).get("path")
            old_path = (entry.get("old") or {}).get("path")
            if entry.get("status") == "removed" and old_path:
                deleted.append(old_path)
            elif new_path:
                changed.append(new_path)
                if entry.get("status") == "renamed" and old_path and old_path != new_path:
                    deleted.append(old_path)
        nxt = page.get("next")
    return {"deleted": deleted, "changed": changed}


def _bb_content(owner: str, repo: str, path: str, commit: str, token: str | None) -> str:
    import httpx
    from urllib.parse import quote

    headers = {}
    auth = _bitbucket_auth(token)
    if auth:
        headers["Authorization"] = auth
    encoded = "/".join(quote(p) for p in path.split("/"))
    url = f"https://api.bitbucket.org/2.0/repositories/{owner}/{repo}/src/{commit}/{encoded}"
    resp = httpx.get(url, headers=headers, timeout=60.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"Bitbucket src {resp.status_code}: {resp.text}")
    return resp.text


# --- GitLab ---

def _gitlab_project(owner: str, repo: str) -> str:
    from urllib.parse import quote

    # GitLab addresses a project by its URL-encoded full path (namespace/project).
    return quote(f"{owner}/{repo}", safe="")


def _gitlab_get(endpoint: str, token: str | None):
    import httpx

    headers = {"Accept": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    url = endpoint if endpoint.startswith("http") else f"https://gitlab.com/api/v4{endpoint}"
    resp = httpx.get(url, headers=headers, timeout=60.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"GitLab API {resp.status_code}: {resp.text}")
    return resp


def _gl_tree(owner: str, repo: str, commit: str, token: str | None) -> list[str]:
    project = _gitlab_project(owner, repo)
    paths: list[str] = []
    page = 1
    while True:
        resp = _gitlab_get(
            f"/projects/{project}/repository/tree?recursive=true&per_page=100&ref={commit}&page={page}",
            token,
        )
        for entry in resp.json():
            if entry.get("type") == "blob" and entry.get("path"):
                paths.append(entry["path"])
        next_page = resp.headers.get("x-next-page")
        if not next_page:
            break
        page = int(next_page)
    return paths


def _gl_compare(owner: str, repo: str, base: str, head: str, token: str | None) -> dict[str, list[str]]:
    project = _gitlab_project(owner, repo)
    resp = _gitlab_get(f"/projects/{project}/repository/compare?from={base}&to={head}", token)
    deleted, changed = [], []
    for d in resp.json().get("diffs") or []:
        if d.get("deleted_file"):
            if d.get("old_path"):
                deleted.append(d["old_path"])
        elif d.get("new_path"):
            changed.append(d["new_path"])
            if d.get("renamed_file") and d.get("old_path") and d["old_path"] != d["new_path"]:
                deleted.append(d["old_path"])
    return {"deleted": deleted, "changed": changed}


def _gl_content(owner: str, repo: str, path: str, commit: str, token: str | None) -> str:
    from urllib.parse import quote

    project = _gitlab_project(owner, repo)
    encoded = quote(path, safe="")
    resp = _gitlab_get(f"/projects/{project}/repository/files/{encoded}/raw?ref={commit}", token)
    return resp.text


def _provider(provider: str) -> dict[str, Any]:
    if provider == "github":
        return {"tree": _gh_tree, "compare": _gh_compare, "content": _gh_content}
    if provider == "bitbucket":
        return {"tree": _bb_tree, "compare": _bb_compare, "content": _bb_content}
    if provider == "gitlab":
        return {"tree": _gl_tree, "compare": _gl_compare, "content": _gl_content}
    if provider == "azure_devops":
        return {"tree": lambda o, r, c, t: [], "compare": lambda o, r, b, h, t: {"deleted": [], "changed": []}, "content": lambda o, r, p, c, t: ""}
    raise ApiError(f"Unsupported git provider: {provider}", 400)


def _auth_clone_url(provider: str, owner: str, project: str, repo: str, token: str | None) -> str:
    if provider == "github":
        return (f"https://x-access-token:{token}@github.com/{owner}/{repo}.git" if token
                else f"https://github.com/{owner}/{repo}.git")
    if provider == "bitbucket":
        if not token:
            return f"https://bitbucket.org/{owner}/{repo}.git"
        if ":" not in token:
            raise ApiError('Bitbucket credential must be in "username:api_key" format (API key via Basic auth).', 400)
        _user, _, passwd = token.partition(":")
        return f"https://x-bitbucket-api-token-auth:{passwd}@bitbucket.org/{owner}/{repo}.git"
    if provider == "gitlab":
        return (f"https://oauth2:{token}@gitlab.com/{owner}/{repo}.git" if token
                else f"https://gitlab.com/{owner}/{repo}.git")
    if provider == "azure_devops":
        domain_url = f"{owner}.visualstudio.com/{project}/_git/{repo}"
        return (f"https://pat:{token}@{domain_url}" if token else f"https://{domain_url}")

    raise ApiError(f"Unsupported git provider: {provider}", 400)


def clone_repo_full(provider: str, owner: str, project: str, repo: str, incoming: str, branch: str, token: str | None, timeout: float = 1800.0) -> str:
    temp_dir = tempfile.mkdtemp(prefix="ontology-clone-")
    auth_url = _auth_clone_url(provider, owner, project, repo, token)
    try:
        # Shallow single-branch clone: big repos can take ~10 min otherwise.
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, "--single-branch", auth_url, temp_dir],
            check=True, capture_output=True, timeout=timeout,
        )
        if incoming:
            try:
                subprocess.run(["git", "-C", temp_dir, "checkout", "--quiet", incoming],
                               check=True, capture_output=True, timeout=timeout)
            except subprocess.CalledProcessError:
                # Shallow clone only has the branch tip; fetch the exact commit if needed
                subprocess.run(["git", "-C", temp_dir, "fetch", "--depth", "1", "origin", incoming],
                               check=True, capture_output=True, timeout=timeout)
                subprocess.run(["git", "-C", temp_dir, "checkout", "--quiet", incoming],
                               check=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git clone timed out: {_scrub(str(exc))}") from None
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if exc.stderr else str(exc)
        raise RuntimeError(f"git clone failed: {_scrub(stderr)}") from None
    import shutil

    shutil.rmtree(Path(temp_dir) / ".git", ignore_errors=True)
    return temp_dir


def resolve_git_diff(provider: str, owner: str, project: str, repo: str, current: str, incoming: str,
                     branch: str, token: str | None, timeout: float = 1800.0) -> tuple[str, set[str], list[str]]:
    api = _provider(provider)
    skeleton = api["tree"](owner, repo, incoming, token)
    diff = api["compare"](owner, repo, current, incoming, token)
    changed, deleted = diff["changed"], diff["deleted"]
    if provider in ("gitlab", "azure_devops"):
        # Fallback to full clone for providers where diff APIs are bypassed
        temp_dir = clone_repo_full(provider, owner, project, repo, incoming, branch, token, timeout)
        return temp_dir, None, []  # type: ignore

    if not changed and not deleted:
        # The commit range touches no files at all — the usual cause is a merge commit
        # whose branch brought in no net change, so GitHub's three-dot compare reports
        # commits but an empty file list. The tree is identical to `current`, so the
        # stored graph is already correct: return an empty diff (not a full clone) and
        # let the caller write empty meta and advance the commit pointer. Distinct from
        # the `changed and not filter_set` case below, which means "changes exist but we
        # could not read them" and must fail loudly.
        return tempfile.mkdtemp(prefix="ontology-"), set(), []

    temp_dir = tempfile.mkdtemp(prefix="ontology-")
    for sp in skeleton:
        full = Path(temp_dir) / sp
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("")
    filter_set: set[str] = set()
    for path in changed:
        try:
            content = api["content"](owner, repo, path, incoming, token)
        except Exception as exc:
            # Binary or genuinely unreadable files are expected to skip; but log
            # them so an empty ingest (e.g. a token that lacks read access) is
            # diagnosable rather than silently reported as "no changes".
            logger.warning("Skipping unreadable changed file %s: %s", path, exc)
            continue
        full = Path(temp_dir) / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        filter_set.add(path)

    # The compare found changed files but none could be read. Treat this as a
    # failure rather than an empty (no-op) diff: otherwise the caller streams an
    # empty result, the backend advances the stored commit, and the graph is left
    # stale while "already up to date" is reported. Failing keeps the sync retryable.
    if changed and not filter_set:
        raise ApiError(
            "Detected changed files but none could be read from the provider — "
            "check the token scope (GitLab needs read_api) and repository access.",
            502,
        )

    return temp_dir, filter_set, deleted


def acquire_diff(settings: Settings, body: dict[str, Any]) -> tuple[str, set[str] | None, list[str]]:
    parsed = parse_repo_url(body["repoUrl"])
    if parsed is None:
        raise ApiError("Invalid repo URL (supported hosts: github.com, bitbucket.org, gitlab.com, dev.azure.com)", 400)
    provider, owner, repo = parsed["provider"], parsed["owner"], parsed["repo"]
    project = parsed.get("project", "")
    current = body.get("currentCommitId")
    incoming = body["incomingCommitId"]
    token = body.get("gitToken")
    has_current = current not in (None, "", "null", "undefined")

    if has_current and provider in ("github", "bitbucket"):
        return resolve_git_diff(provider, owner, project, repo, current, incoming, body["gitBranch"], token,
                                settings.git_clone_timeout)
    temp_dir = clone_repo_full(provider, owner, project, repo, incoming, body["gitBranch"], token, settings.git_clone_timeout)
    return temp_dir, None, []  # full clone → process every file


# ── Provider diff for POST /api/git-diff ──────────────────────────────────────
#
# BREEZEAI-1228. This is the centralised replacement for the four private
# `get*Diff` helpers that used to live in BreezeAI_Backend's `GitService`
# (`src/services/git.service.ts`). It is a FAITHFUL port: each provider is
# queried with the same endpoints and arguments as before, and the per-file
# `status` strings are unchanged, so existing backend callers see identical
# behaviour.
#
# ONE DELIBERATE DEVIATION — `null` instead of `0`/absent. The backend reported
# `additions: 0` / `deletions: 0` for providers that do not supply line counts,
# which is indistinguishable from a file that genuinely changed zero lines. Here
# an unsupplied value is `None`. What each provider can actually tell us:
#
#   provider   | additions/deletions        | patch
#   -----------|----------------------------|---------------------------
#   github     | real                       | yes
#   gitlab     | None (API omits them)      | yes (`diff` field)
#   azure      | None                       | None (no patch API)
#   bitbucket  | real on compare, real on   | only on the single-commit
#              | single-commit              | path (raw diff text)
#
# Anything that treats these as line counts must handle `None`.

_DIFF_TIMEOUT = 60.0

#: Azure DevOps versions its payload shapes, so every request must name one.
#: 7.1 is the current GA version; the backend's Azure calls were pinned at 6.0
#: and sdlc-autonomous-agents was already on 7.1, so 6.0 was the outlier.
#: ONE constant: this was duplicated across nine call sites, which is how the
#: two services drifted apart unnoticed.
_ADO_API_VERSION = "7.1"
_ADO_PARAMS: dict[str, Any] = {"api-version": _ADO_API_VERSION}

# Azure's no-base fallback used to return the file's SIZE IN BYTES in
# `additions`. That was never a line count; it is now `None` like every other
# value Azure cannot supply.


def _diff_file(filename: str, status: str, additions: int | None = None,
               deletions: int | None = None, patch: str | None = None,
               previous_filename: str | None = None) -> dict[str, Any]:
    """One changed file.

    `previousFilename` is populated only on a rename and only where the provider
    reports it; consumers that track renames (sdlc-autonomous-agents' `FileChange`)
    need it to follow a file across a move.
    """
    return {
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "patch": patch,
        "previousFilename": previous_filename,
    }


def _diff_result(base_sha: str, head_sha: str, files: list[dict[str, Any]],
                 truncated: bool = False) -> dict[str, Any]:
    """`truncated` is the provider saying "this list is incomplete".

    GitHub caps `/commits/{sha}` at 300 files and cannot paginate it. A consumer
    that cannot distinguish a capped list from a complete one will silently act on
    partial data, so it is surfaced rather than logged and dropped.
    """
    return {"baseSha": base_sha, "headSha": head_sha, "totalFiles": len(files),
            "files": files, "truncated": truncated}


def _diff_headers(provider: str, token: str | None) -> dict[str, str]:
    """Auth headers per provider — mirrors `GitService.buildHeaders`.

    Bitbucket is the one place this is STRICTER than the backend: the backend
    silently sent a `Bearer` header when the credential lacked a ``:``, which
    Bitbucket rejects with an opaque 401. ``_bitbucket_auth`` raises a clean 400
    instead (the convention already used by ``/api/analyze-diff``).
    """
    headers = {"Accept": "application/json"}
    if provider == "bitbucket":
        auth = _bitbucket_auth(token)
        if auth:
            headers["Authorization"] = auth
        return headers
    if not token:
        # Matches the backend: with no token the whole auth block is skipped and
        # the request goes out anonymous (fine for public repos).
        return headers
    if provider == "gitlab":
        headers["PRIVATE-TOKEN"] = token
    elif provider == "azure_devops":
        # Azure PAT auth is Basic with a BLANK username.
        headers["Authorization"] = "Basic " + base64.b64encode(f":{token}".encode()).decode()
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _base_api_url(repo_url: str, parsed: dict[str, str]) -> str:
    """Provider REST root for a parsed repo — mirrors `GitService.parseRepoUrl`."""
    from urllib.parse import quote

    provider, owner, repo = parsed["provider"], parsed["owner"], parsed["repo"]
    if provider == "github":
        return f"https://api.github.com/repos/{owner}/{repo}"
    if provider == "gitlab":
        # GitLab addresses a project by its URL-encoded full path, which for a
        # nested group is more than two segments.
        return f"https://gitlab.com/api/v4/projects/{quote(f'{owner}/{repo}', safe='')}"
    if provider == "bitbucket":
        return f"https://api.bitbucket.org/2.0/repositories/{owner}/{repo}"
    if provider == "azure_devops":
        project = quote(parsed.get("project", ""))
        repo_q = quote(repo)
        # parse_repo_url collapses both Azure hosts into one shape, so recover
        # which one this was from the original URL — the API roots differ.
        if "dev.azure.com" in repo_url:
            return f"https://dev.azure.com/{quote(owner)}/{project}/_apis/git/repositories/{repo_q}"
        return f"https://{owner}.visualstudio.com/{project}/_apis/git/repositories/{repo_q}"
    raise ApiError(f"Unsupported git provider: {provider}", 400)


_UNSUPPORTED_HOST = ("Invalid repo URL (supported hosts: github.com, bitbucket.org, "
                    "gitlab.com, dev.azure.com, *.visualstudio.com)")


def _resolve(repo_url: str, token: str | None) -> tuple[dict[str, str], str, str, dict[str, str]]:
    """Parse a repo URL into (parsed, provider, base API url, auth headers).

    Every git endpoint starts here, so an unsupported host is rejected BEFORE any
    request goes out — the fix for the old behaviour where an unrecognised host
    silently fell through to GitHub.
    """
    parsed = parse_repo_url(repo_url)
    if parsed is None:
        raise ApiError(_UNSUPPORTED_HOST, 400)
    provider = parsed["provider"]
    return parsed, provider, _base_api_url(repo_url, parsed), _diff_headers(provider, token)


#: Statuses worth another attempt: a rate limit and the three transient 5xx a
#: provider returns while shedding load. Everything else (401/403/404/422) is a
#: decision, not a hiccup, and retrying it only delays the error.
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_MAX = 3
_RETRY_BACKOFF_SECONDS = 2.0


def _send_with_retry(send, what: str):
    """Call *send*, retrying the transient statuses with jittered backoff.

    Jitter is multiplicative on the doubling base: without it every caller that
    hit the same rate limit retries in lockstep and re-trips it together.

    Args:
        send: Zero-arg callable performing one HTTP attempt.
        what: Short description used in the logs.

    Returns:
        The first response that is not a retryable status.

    Raises:
        ApiError: 502 once the budget is exhausted or on a non-retryable status.
    """
    import random
    import time

    last = None
    for attempt in range(_RETRY_MAX + 1):
        resp = send()
        if resp.status_code not in _RETRY_STATUSES:
            return resp
        last = resp
        if attempt == _RETRY_MAX:
            break
        delay = _RETRY_BACKOFF_SECONDS * (2**attempt) * (0.5 + random.random())
        logger.warning(
            "Retrying %s after HTTP %s (attempt %d/%d, sleeping %.1fs)",
            what, resp.status_code, attempt + 1, _RETRY_MAX, delay,
        )
        time.sleep(delay)
    logger.error("Gave up on %s after %d retries; last status %s",
                 what, _RETRY_MAX, last.status_code if last else "?")
    return last


def _check(resp, what: str):
    if resp.status_code >= 400:
        raise ApiError(
            f"Git provider returned {resp.status_code} for {what}: {_scrub(resp.text)[:500]}", 502
        )
    return resp


def _diff_get(url: str, headers: dict[str, str], params: dict[str, Any] | None = None):
    import httpx

    return _check(
        _send_with_retry(
            lambda: httpx.get(url, headers=headers, params=params, timeout=_DIFF_TIMEOUT),
            f"GET {url}",
        ),
        f"GET {url}",
    )


def parse_raw_diff(raw_diff: str) -> list[dict[str, Any]]:
    """Split raw unified-diff text into per-file entries.

    Port of `GitService.parseRawDiff`. Bitbucket's single-commit endpoint returns
    raw text rather than JSON, so counts are derived by counting +/- lines. A
    leading ``+++``/``---`` header line is excluded by requiring a non-repeat
    character after the sign — the same trick the original used.
    """
    if not isinstance(raw_diff, str):
        return []
    blocks = [b for b in re.split(r"(?m)^diff --git ", raw_diff) if b]
    files: list[dict[str, Any]] = []
    for block in blocks:
        first_line = block.split("\n")[0] if block else ""
        match = re.search(r"b/(.+)$", first_line)
        files.append(_diff_file(
            filename=match.group(1) if match else "unknown",
            status="modified",  # raw diff text does not distinguish add/delete
            additions=len(re.findall(r"(?m)^\+[^+]", block)),
            deletions=len(re.findall(r"(?m)^-[^-]", block)),
            patch=block,
        ))
    return files


def _github_diff(base_api: str, headers: dict[str, str], head: str, base: str | None) -> dict[str, Any]:
    def _map(files: list[dict]) -> list[dict[str, Any]]:
        return [_diff_file(f.get("filename", ""), f.get("status", ""),
                           f.get("additions"), f.get("deletions"), f.get("patch"),
                           f.get("previous_filename")) for f in files]

    if not base:
        # Single commit — GitHub diffs it against its first parent for us.
        data = _diff_get(f"{base_api}/commits/{head}", headers).json()
        parents = data.get("parents") or []
        return _diff_result(parents[0].get("sha", "") if parents else "", head, _map(data.get("files") or []))
    data = _diff_get(f"{base_api}/compare/{base}...{head}", headers).json()
    return _diff_result(base, head, _map(data.get("files") or []))


def _gitlab_status(f: dict) -> str:
    if f.get("new_file"):
        return "added"
    if f.get("deleted_file"):
        return "removed"
    if f.get("renamed_file"):
        return "renamed"
    return "modified"


def _gitlab_diff(base_api: str, headers: dict[str, str], head: str, base: str | None) -> dict[str, Any]:
    def _map(diffs: list[dict]) -> list[dict[str, Any]]:
        # GitLab reports no line counts on either endpoint — only the diff text.
        return [_diff_file(f.get("new_path") or f.get("old_path") or "", _gitlab_status(f),
                           None, None, f.get("diff"),
                           f.get("old_path") if f.get("renamed_file") else None) for f in diffs]

    if not base:
        data = _diff_get(f"{base_api}/repository/commits/{head}/diff", headers).json()
        return _diff_result("", head, _map(data or []))
    data = _diff_get(f"{base_api}/repository/compare", headers, {"from": base, "to": head}).json()
    return _diff_result(base, head, _map(data.get("diffs") or []))


def _azure_diff(base_api: str, headers: dict[str, str], head: str, base: str | None) -> dict[str, Any]:
    if base:
        data = _diff_get(f"{base_api}/diffs/commits", headers, {
            "api-version": _ADO_API_VERSION,
            "baseVersion": base, "baseVersionType": "Commit",
            "targetVersion": head, "targetVersionType": "Commit",
        }).json()
        # NOTE: `item.path` keeps its leading "/" here — unchanged from the
        # backend, because callers already match on these values.
        files = [_diff_file(c.get("item", {}).get("path", "") or "", c.get("changeType") or "edit")
                 for c in (data.get("changes") or [])]
        return _diff_result(base, head, files)

    # No base: `diffs/commits` rejects a missing baseVersion ("first" is not a
    # valid versionType), so enumerate the tree at head and report it all as added.
    data = _diff_get(f"{base_api}/items", headers, {
        "api-version": _ADO_API_VERSION,
        "versionDescriptor": json.dumps({"version": head, "versionType": "Commit"}),
        "recursionLevel": "Full",
    }).json()
    files = [_diff_file((item.get("path") or "").lstrip("/"), "added")
             for item in _azure_page_items(data) if not item.get("isFolder")]
    return _diff_result("", head, files)


def _bitbucket_diff(base_api: str, headers: dict[str, str], head: str, base: str | None) -> dict[str, Any]:
    if not base:
        # Single commit returns RAW diff text, not JSON.
        raw = _diff_get(f"{base_api}/diff/{head}", headers, {"context": 3}).text
        return _diff_result("", head, parse_raw_diff(raw))
    # `diffstat/{spec}` reads as "rev1 relative to rev2", so head..base is
    # correct here even though GitHub/GitLab spell the comparison base-first.
    data = _diff_get(f"{base_api}/diffstat/{head}..{base}", headers).json()
    files = []
    for f in (data.get("values") or []):
        new_path = (f.get("new") or {}).get("path")
        old_path = (f.get("old") or {}).get("path")
        files.append(_diff_file(new_path or old_path or "", f.get("status", ""),
                                f.get("lines_added"), f.get("lines_removed"), None,
                                old_path if f.get("status") == "renamed" and old_path != new_path else None))
    return _diff_result(base, head, files)


_DIFF_DISPATCH = {
    "github": _github_diff,
    "gitlab": _gitlab_diff,
    "azure_devops": _azure_diff,
    "bitbucket": _bitbucket_diff,
}


def fetch_git_diff(repo_url: str, head_commit: str, base_commit: str | None,
                   token: str | None) -> dict[str, Any]:
    """Fetch a file-level diff from the repo's git provider.

    The provider is inferred from the URL host. Blocking (httpx) — call it off
    the event loop.

    Raises:
        ApiError 400: the host is not a supported provider, or the credential
            shape is wrong. Note this is the FIX for the backend's old
            behaviour, where an unrecognised host silently fell through to
            GitHub and produced 404s against the wrong API.
        ApiError 502: the provider call itself failed.
    """
    parsed, provider, base_api, headers = _resolve(repo_url, token)
    handler = _DIFF_DISPATCH.get(provider)
    if handler is None:
        raise ApiError(f"Unsupported git provider: {provider}", 400)
    try:
        return handler(base_api, headers, head_commit, base_commit)
    except ApiError:
        raise
    except Exception as exc:  # network error, malformed payload, …
        raise ApiError(f"Failed to fetch diff from {provider}: {_scrub(str(exc))}", 502) from None


# ── Pull requests for POST /api/pull-request ──────────────────────────────────
#
# BREEZEAI-1228 (follow-up). Centralises the PR lookups that used to live in
# BreezeAI_Backend's `GitService`: `getPullRequestInfo` (full details + commits)
# and `getPullRequestBaseBranch` (base branch NAME only, no commits). Both are
# served by ONE endpoint — the lightweight variant is `includeCommits=false`,
# because the only thing that made it cheap was skipping the commits fetch.
#
# "Pull request" is canonical here; each provider's spelling (GitLab merge
# request, Bitbucket `pullrequests`, Azure `pullRequests`) is translated
# internally so callers never branch on provider.

class _SkipApprovals(Exception):
    """Internal sentinel: the caller asked for the cheap metadata-only fetch.

    GitHub and GitLab expose approvals on a SEPARATE endpoint, so reading them
    costs a second round-trip. `includeCommits=false` exists precisely to avoid
    extra round-trips, so it suppresses approvals too — `approvals` comes back
    `[]`, which means "not fetched", not "nobody approved". Azure and Bitbucket
    carry approvals on the PR payload itself, so they are always populated.
    """


_REFS_HEADS = "refs/heads/"


def _strip_ref(ref: str | None) -> str:
    """Azure returns fully-qualified refs; callers want the bare branch name."""
    ref = ref or ""
    return ref[len(_REFS_HEADS):] if ref.startswith(_REFS_HEADS) else ref


def _commit(sha: str, message: str, author: str, date: str) -> dict[str, Any]:
    return {"sha": sha or "", "message": message or "", "author": author or "", "date": date or ""}


def _paged_commits(url: str, headers: dict[str, str], extract, page_size: int = 100) -> list[dict[str, Any]]:
    """Page-number pagination (GitHub, GitLab): stop on an empty or short page."""
    commits: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _diff_get(url, headers, {"per_page": page_size, "page": page}).json()
        if not data:
            break
        commits.extend(extract(c) for c in data)
        if len(data) < page_size:
            break
        page += 1
    return commits


#: Azure does NOT wrap every endpoint the same way. Most return {"value": [...]},
#: but the change endpoints do not: commit changes come back under "changes" and
#: PR iteration changes under "changeEntries". Reading only "value" yields an
#: empty list — no error, no files, and a caller that validates nothing and
#: passes vacuously.
_AZ_PAGE_KEYS = ("value", "changes", "changeEntries")


def _azure_page_items(body: Any) -> list[Any]:
    """Items from one Azure page, whichever envelope it used."""
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for key in _AZ_PAGE_KEYS:
        items = body.get(key)
        if isinstance(items, list):
            return items
    return []


def _azure_paged(url: str, headers: dict[str, str], params: dict[str, Any], extract,
                 page_size: int = 100, max_pages: int = 100) -> list[dict[str, Any]]:
    """Continuation-token pagination (Azure DevOps).

    Azure signals "there is more" with the ``x-ms-continuationtoken`` response
    HEADER rather than a field in the body, which is why it needs its own loop
    instead of reusing `_paged_commits` or `_cursor_commits`.

    `max_pages` is a safety stop: if a provider ever echoed the same token back,
    the loop would otherwise never end. Hitting it logs rather than raising,
    because a truncated list is still more useful than a failed request.
    """
    out: list[dict[str, Any]] = []
    token: str | None = None
    for page in range(max_pages):
        page_params = {**params, "$top": page_size}
        if token:
            page_params["continuationToken"] = token
        resp = _diff_get(url, headers, page_params)
        values = _azure_page_items(resp.json())
        out.extend(extract(v) for v in values)
        token = (getattr(resp, "headers", None) or {}).get("x-ms-continuationtoken")
        if not token or not values:
            return out
    logger.warning("Azure pagination hit the %d-page safety stop for %s; list may be truncated",
                   max_pages, url)
    return out


def _cursor_commits(url: str, headers: dict[str, str], extract) -> list[dict[str, Any]]:
    """Cursor pagination (Bitbucket): follow `next` until it is absent."""
    commits: list[dict[str, Any]] = []
    nxt: str | None = url
    while nxt:
        data = _diff_get(nxt, headers).json()
        commits.extend(extract(c) for c in (data.get("values") or []))
        nxt = data.get("next")
    return commits


def _github_pr(base_api: str, headers: dict[str, str], pr_id: Any, include_commits: bool) -> dict[str, Any]:
    pr = _diff_get(f"{base_api}/pulls/{pr_id}", headers).json()
    commits = _paged_commits(
        f"{base_api}/pulls/{pr_id}/commits", headers,
        lambda c: _commit(c.get("sha"), (c.get("commit") or {}).get("message"),
                          ((c.get("commit") or {}).get("author") or {}).get("name"),
                          ((c.get("commit") or {}).get("author") or {}).get("date")),
    ) if include_commits else []
    base, head = pr.get("base") or {}, pr.get("head") or {}
    # Approvals are a separate endpoint. Merge Validation gates on them, but a
    # reviews failure must not fail the whole PR fetch — degrade to [].
    approvals: list[str] = []
    try:
        if not include_commits:
            raise _SkipApprovals
        reviews = _diff_get(f"{base_api}/pulls/{pr_id}/reviews", headers, {"per_page": 100}).json()
        approvals = [(r.get("user") or {}).get("login") for r in (reviews or [])
                     if r.get("state") == "APPROVED"]
        approvals = [a for a in approvals if a]
    except _SkipApprovals:
        pass
    except Exception:
        logger.warning("Could not read reviews for GitHub PR %s; approvals reported empty", pr_id)
    # GitHub's PR state is open|closed; "merged" is closed + merged_at.
    state = pr.get("state") or ""
    if state == "closed" and pr.get("merged_at"):
        state = "merged"
    return {
        "id": pr.get("number"), "title": pr.get("title"), "state": state,
        "description": pr.get("body") or "", "author": (pr.get("user") or {}).get("login") or "",
        "url": pr.get("html_url") or "", "createdAt": pr.get("created_at") or "",
        "mergedAt": pr.get("merged_at"),
        "labels": [l.get("name") for l in (pr.get("labels") or []) if l.get("name")],
        "reviewers": [r.get("login") for r in (pr.get("requested_reviewers") or []) if r.get("login")],
        "approvals": approvals,
        "baseBranch": base.get("ref") or "", "sourceBranch": head.get("ref") or "",
        "baseCommitSha": base.get("sha") or "", "headCommitSha": head.get("sha") or "",
        "commits": commits,
    }


def _gitlab_pr(base_api: str, headers: dict[str, str], pr_id: Any, include_commits: bool) -> dict[str, Any]:
    mr = _diff_get(f"{base_api}/merge_requests/{pr_id}", headers).json()
    commits = _paged_commits(
        f"{base_api}/merge_requests/{pr_id}/commits", headers,
        lambda c: _commit(c.get("id"), c.get("message"), c.get("author_name"), c.get("committed_date")),
    ) if include_commits else []
    diff_refs = mr.get("diff_refs") or {}
    approvals: list[str] = []
    try:
        if not include_commits:
            raise _SkipApprovals
        appr = _diff_get(f"{base_api}/merge_requests/{pr_id}/approvals", headers).json()
        approvals = [((a.get("user") or {}).get("username")) for a in (appr.get("approved_by") or [])]
        approvals = [a for a in approvals if a]
    except _SkipApprovals:
        pass
    except Exception:
        logger.warning("Could not read approvals for GitLab MR %s; reported empty", pr_id)
    state = {"opened": "open", "locked": "closed"}.get(mr.get("state") or "", mr.get("state") or "")
    return {
        "id": mr.get("iid"), "title": mr.get("title"), "state": state,
        "description": mr.get("description") or "",
        "author": (mr.get("author") or {}).get("username") or "",
        "url": mr.get("web_url") or "", "createdAt": mr.get("created_at") or "",
        "mergedAt": mr.get("merged_at"),
        "labels": list(mr.get("labels") or []),
        "reviewers": [r.get("username") for r in (mr.get("reviewers") or []) if r.get("username")],
        "approvals": approvals,
        "baseBranch": mr.get("target_branch") or "", "sourceBranch": mr.get("source_branch") or "",
        # FIXED (BREEZEAI-1228): this used to fall back to `target_branch` when
        # GitLab omits `diff_refs` (it does on some MR states). `target_branch` is
        # a branch NAME, not a SHA — so the diff was taken against wherever that
        # branch points RIGHT NOW rather than where the MR forked, silently
        # folding in unrelated commits merged since. Same defect class as the
        # Azure swap. "" now propagates instead, so the caller's existing
        # "could not determine base or head commit" guard fires with a clear 400.
        # `headCommitSha` needs no such fix: both its sources are real SHAs.
        "baseCommitSha": diff_refs.get("base_sha") or "",
        "headCommitSha": mr.get("sha") or diff_refs.get("head_sha") or "",
        "commits": commits,
    }


def _azure_pr(base_api: str, headers: dict[str, str], pr_id: Any, include_commits: bool) -> dict[str, Any]:
    params = dict(_ADO_PARAMS)
    pr = _diff_get(f"{base_api}/pullrequests/{pr_id}", headers, params).json()
    commits: list[dict[str, Any]] = []
    if include_commits:
        # FIXED (BREEZEAI-1228): this used to fetch a SINGLE page, so any PR with
        # more commits than one page returned a silently truncated list — and
        # nothing downstream could tell. Now paginates like the other three.
        commits = _azure_paged(
            f"{base_api}/pullrequests/{pr_id}/commits", headers, params,
            lambda c: _commit(c.get("commitId"), c.get("comment"),
                              (c.get("author") or {}).get("name"),
                              (c.get("author") or {}).get("date")),
        )
    reviewers = pr.get("reviewers") or []
    state = {"active": "open", "completed": "merged", "abandoned": "closed"}.get(
        pr.get("status") or "", pr.get("status") or "")
    return {
        "id": pr.get("pullRequestId"), "title": pr.get("title"), "state": state,
        "description": pr.get("description") or "",
        "author": (pr.get("createdBy") or {}).get("displayName") or "",
        "url": pr.get("url") or "", "createdAt": pr.get("creationDate") or "",
        "mergedAt": pr.get("closedDate"),
        "labels": [l.get("name") for l in (pr.get("labels") or []) if l.get("name")],
        "reviewers": [r.get("displayName") for r in reviewers if r.get("displayName")],
        # Azure encodes approval as a numeric vote; 10 == "approved".
        "approvals": [r.get("displayName") for r in reviewers
                      if r.get("vote") == 10 and r.get("displayName")],
        "baseBranch": _strip_ref(pr.get("targetRefName")), "sourceBranch": _strip_ref(pr.get("sourceRefName")),
        # Azure's SOURCE commit is the PR head and its TARGET commit is the base.
        # The backend had these two swapped, so every Azure PR sync analysed a
        # REVERSED diff (additions read as deletions). Corrected here.
        # The old `|| targetRefName` / `|| sourceRefName` fallbacks are dropped
        # rather than swapped: ref names are not SHAs, so they could never be a
        # valid commit argument. "" lets the caller's existing guard fire.
        "baseCommitSha": (pr.get("lastMergeTargetCommit") or {}).get("commitId") or "",
        "headCommitSha": (pr.get("lastMergeSourceCommit") or {}).get("commitId") or "",
        "commits": commits,
    }


def _bitbucket_pr(base_api: str, headers: dict[str, str], pr_id: Any, include_commits: bool) -> dict[str, Any]:
    pr = _diff_get(f"{base_api}/pullrequests/{pr_id}", headers).json()
    commits = _cursor_commits(
        f"{base_api}/pullrequests/{pr_id}/commits?pagelen=100", headers,
        lambda c: _commit(c.get("hash"), c.get("message"),
                          (c.get("author") or {}).get("raw")
                          or ((c.get("author") or {}).get("user") or {}).get("display_name"),
                          c.get("date")),
    ) if include_commits else []
    dest, src = pr.get("destination") or {}, pr.get("source") or {}
    participants = pr.get("participants") or []
    def _name(u: dict) -> str:
        return (u or {}).get("display_name") or (u or {}).get("nickname") or ""
    state = {"OPEN": "open", "MERGED": "merged",
             "DECLINED": "closed", "SUPERSEDED": "closed"}.get(pr.get("state") or "",
                                                               (pr.get("state") or "").lower())
    return {
        "id": pr.get("id"), "title": pr.get("title"), "state": state,
        "description": (pr.get("summary") or {}).get("raw") or pr.get("description") or "",
        "author": _name(pr.get("author")), "createdAt": pr.get("created_on") or "",
        "url": ((pr.get("links") or {}).get("html") or {}).get("href") or "",
        "mergedAt": pr.get("updated_on") if (pr.get("state") == "MERGED") else None,
        # Bitbucket Cloud has no PR labels at all.
        "labels": [],
        "reviewers": [_name(r) for r in (pr.get("reviewers") or []) if _name(r)],
        "approvals": [_name(p.get("user")) for p in participants
                      if p.get("approved") and _name(p.get("user"))],
        "baseBranch": (dest.get("branch") or {}).get("name") or "",
        "sourceBranch": (src.get("branch") or {}).get("name") or "",
        "baseCommitSha": (dest.get("commit") or {}).get("hash") or "",
        "headCommitSha": (src.get("commit") or {}).get("hash") or "",
        "commits": commits,
    }


_PR_DISPATCH = {
    "github": _github_pr,
    "gitlab": _gitlab_pr,
    "azure_devops": _azure_pr,
    "bitbucket": _bitbucket_pr,
}


def fetch_pull_request(repo_url: str, pull_request_id: Any, token: str | None,
                       include_commits: bool = True) -> dict[str, Any]:
    """Fetch pull-request details from the repo's git provider.

    Set ``include_commits=False`` to skip the commit listing — that is the only
    thing that made the backend's `getPullRequestBaseBranch` cheap, and it saves
    a paginated round-trip when the caller only needs `baseBranch`.

    Blocking (httpx) — call it off the event loop.

    Raises:
        ApiError 400: unsupported host or bad credential shape.
        ApiError 502: the provider call failed.
    """
    parsed, provider, base_api, headers = _resolve(repo_url, token)
    handler = _PR_DISPATCH.get(provider)
    if handler is None:
        raise ApiError(f"Unsupported git provider: {provider}", 400)
    try:
        return handler(base_api, headers, pull_request_id, include_commits)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"Failed to fetch pull request from {provider}: {_scrub(str(exc))}", 502) from None


# ── Latest commit + PR comment for /api/latest-commit and /api/pr-comment ─────
#
# BREEZEAI-1228 (follow-up 2). The last two provider-touching methods from
# BreezeAI_Backend's `GitService`. After these, no service outside COG makes a
# direct git-provider REST call.


def _diff_post(url: str, headers: dict[str, str], payload: dict[str, Any],
               params: dict[str, Any] | None = None):
    import httpx

    # Writes are retried on the same transient statuses. Every POST here is
    # idempotent in effect — a duplicate comment or a repeated status update is
    # strictly better than losing the PR gate to one 503.
    return _check(
        _send_with_retry(
            lambda: httpx.post(url, headers=headers, json=payload, params=params,
                               timeout=_DIFF_TIMEOUT),
            f"POST {url}",
        ),
        f"POST {url}",
    )


def _github_latest(base_api: str, headers: dict[str, str], branch: str) -> dict[str, Any]:
    from urllib.parse import quote

    br = _diff_get(f"{base_api}/branches/{quote(branch, safe='')}", headers).json()
    sha = ((br.get("commit") or {}).get("sha")) or ""
    # Second hop: the branch payload carries only a commit stub, not message/author.
    detail = _diff_get(f"{base_api}/commits/{sha}", headers).json()
    c = (detail.get("commit") or {})
    author = c.get("author") or {}
    return _commit(sha, c.get("message"), author.get("name"), author.get("date"))


def _gitlab_latest(base_api: str, headers: dict[str, str], branch: str) -> dict[str, Any]:
    from urllib.parse import quote

    data = _diff_get(f"{base_api}/repository/branches/{quote(branch, safe='')}", headers).json()
    c = data.get("commit") or {}
    return _commit(c.get("id"), c.get("message"), c.get("author_name"), c.get("committed_date"))


def _bitbucket_latest(base_api: str, headers: dict[str, str], branch: str) -> dict[str, Any]:
    from urllib.parse import quote

    data = _diff_get(f"{base_api}/refs/branches/{quote(branch, safe='')}", headers).json()
    t = data.get("target") or {}
    author = t.get("author") or {}
    return _commit(t.get("hash"), t.get("message"),
                   author.get("raw") or (author.get("user") or {}).get("display_name"), t.get("date"))


def _azure_latest(base_api: str, headers: dict[str, str], branch: str) -> dict[str, Any]:
    from urllib.parse import quote

    params = dict(_ADO_PARAMS)
    refs = _diff_get(f"{base_api}/refs", headers,
                     {**params, "filter": f"heads/{quote(branch, safe='')}"}).json()
    items = _azure_page_items(refs)
    ref = items[0] if items else None
    if not ref:
        # 404 rather than the backend's generic Error (which surfaced as a 500):
        # a missing branch is a client-correctable condition, not a server fault.
        raise ApiError(f"Branch {branch} not found in Azure DevOps repository", 404)
    sha = ref.get("objectId") or ""
    detail = _diff_get(f"{base_api}/commits/{sha}", headers, params).json()
    author = detail.get("author") or {}
    return _commit(sha, detail.get("comment"), author.get("name"), author.get("date"))


_LATEST_DISPATCH = {
    "github": _github_latest,
    "gitlab": _gitlab_latest,
    "bitbucket": _bitbucket_latest,
    "azure_devops": _azure_latest,
}


def fetch_latest_commit(repo_url: str, branch: str, token: str | None) -> dict[str, Any]:
    """Tip commit of *branch*. Blocking (httpx) — call it off the event loop."""
    parsed, provider, base_api, headers = _resolve(repo_url, token)
    handler = _LATEST_DISPATCH.get(provider)
    if handler is None:
        raise ApiError(f"Unsupported git provider: {provider}", 400)
    try:
        return handler(base_api, headers, branch)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"Failed to fetch latest commit from {provider}: {_scrub(str(exc))}", 502) from None


def post_pull_request_comment(repo_url: str, pull_request_id: Any, body: str,
                              token: str | None) -> dict[str, Any]:
    """Post a comment on a pull request.

    ⚠️ CONTRACT CHANGE from the backend's `postPrComment`, which took a
    caller-supplied `commentUrl` and POSTed to it verbatim. COG builds the URL
    itself from `repoUrl` + `pullRequestId`, for two reasons:

    1. **Security.** Accepting an arbitrary URL would make this endpoint a
       generic request forger — it would POST caller-controlled content, with a
       real credential attached, to anywhere the caller named.
    2. **It fixes Azure.** The backend sent GitHub-shaped ``{body}`` to the PR
       *resource* URL, which Azure rejects; the error was then swallowed by the
       caller, so Azure skip-comments silently never appeared. Building the URL
       here means using Azure's actual threads API and payload.
    """
    parsed, provider, base_api, headers = _resolve(repo_url, token)
    headers["Content-Type"] = "application/json"

    try:
        if provider == "github":
            # PR comments live on the ISSUES endpoint, not /pulls.
            _diff_post(f"{base_api}/issues/{pull_request_id}/comments", headers, {"body": body})
        elif provider == "gitlab":
            _diff_post(f"{base_api}/merge_requests/{pull_request_id}/notes", headers, {"body": body})
        elif provider == "bitbucket":
            _diff_post(f"{base_api}/pullrequests/{pull_request_id}/comments", headers,
                       {"content": {"raw": body}})
        elif provider == "azure_devops":
            # Azure has no flat comment endpoint: a comment is the first entry of
            # a new THREAD. commentType 1 = text, status 1 = active.
            _diff_post(f"{base_api}/pullRequests/{pull_request_id}/threads", headers,
                       {"comments": [{"parentCommentId": 0, "content": body, "commentType": 1}],
                        "status": 1},
                       dict(_ADO_PARAMS))
        else:
            raise ApiError(f"Unsupported git provider: {provider}", 400)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"Failed to post comment to {provider}: {_scrub(str(exc))}", 502) from None
    return {"posted": True, "provider": provider}


# ── Directory tree for POST /api/directory-tree ───────────────────────────────
#
# BREEZEAI-1228 (follow-up 3). Port of `GitService.getDirectoryTree` and its four
# private `get*Tree` helpers, which had been commented out in the backend as dead
# code. Reimplemented here so the provider knowledge survives in a live, tested
# form rather than as commented-out TypeScript.


def _tree_entry(path: str, is_dir: bool, size: int | None = None) -> dict[str, Any]:
    return {"path": path or "", "type": "tree" if is_dir else "blob", "size": size}


def _github_tree(base_api: str, headers: dict[str, str], branch: str) -> dict[str, Any]:
    from urllib.parse import quote

    data = _diff_get(f"{base_api}/git/trees/{quote(branch, safe='')}", headers,
                     {"recursive": 1}).json()
    entries = [_tree_entry(e.get("path"), e.get("type") == "tree", e.get("size"))
               for e in (data.get("tree") or [])]
    # GitHub does not paginate this endpoint — it returns everything up to an
    # internal cap and sets `truncated`. The backend ignored that flag, so a very
    # large repo silently produced a partial tree. Surface it instead of guessing.
    return {"entries": entries, "truncated": bool(data.get("truncated"))}


def _gitlab_tree(base_api: str, headers: dict[str, str], branch: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _diff_get(f"{base_api}/repository/tree", headers,
                         {"ref": branch, "recursive": True, "per_page": 100, "page": page}).json()
        if not data:
            break
        entries.extend(_tree_entry(e.get("path"), e.get("type") == "tree") for e in data)
        if len(data) < 100:
            break
        page += 1
    return {"entries": entries, "truncated": False}


def _azure_tree(base_api: str, headers: dict[str, str], branch: str) -> dict[str, Any]:
    data = _diff_get(f"{base_api}/items", headers, {
        "api-version": _ADO_API_VERSION,
        "versionDescriptor": json.dumps({"version": branch, "versionType": "Branch"}),
        "recursionLevel": "Full",
    }).json()
    # `versionType` is PascalCase-strict on Azure; "branch" resolves wrong.
    entries = [_tree_entry((i.get("path") or "").lstrip("/"), bool(i.get("isFolder")), i.get("size"))
               for i in _azure_page_items(data)]
    return {"entries": entries, "truncated": False}


def _bitbucket_tree(base_api: str, headers: dict[str, str], branch: str) -> dict[str, Any]:
    from urllib.parse import quote

    entries: list[dict[str, Any]] = []
    nxt: str | None = f"{base_api}/src/{quote(branch, safe='')}/?pagelen=100"
    while nxt:
        data = _diff_get(nxt, headers).json()
        for e in (data.get("values") or []):
            entries.append(_tree_entry(e.get("path"), e.get("type") == "commit_directory", e.get("size")))
        nxt = data.get("next")
    return {"entries": entries, "truncated": False}


_TREE_DISPATCH = {
    "github": _github_tree,
    "gitlab": _gitlab_tree,
    "azure_devops": _azure_tree,
    "bitbucket": _bitbucket_tree,
}


def fetch_directory_tree(repo_url: str, branch: str, token: str | None) -> dict[str, Any]:
    """Recursive file listing for a branch.

    Returns ``{entries: [{path, type, size}], truncated: bool}``. `truncated` is
    only ever true for GitHub, whose tree endpoint caps its response instead of
    paginating; the other three paginate to completion.

    Blocking (httpx) — call it off the event loop.
    """
    parsed, provider, base_api, headers = _resolve(repo_url, token)
    handler = _TREE_DISPATCH.get(provider)
    if handler is None:
        raise ApiError(f"Unsupported git provider: {provider}", 400)
    try:
        return handler(base_api, headers, branch)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"Failed to fetch directory tree from {provider}: {_scrub(str(exc))}", 502) from None


# ── Commit detail, commit comment, commit status ──────────────────────────────
#
# BREEZEAI-1228 (follow-up 4). The three `AbstractSCMClient` methods COG did not
# cover, blocking sdlc-autonomous-agents from retiring its own `scm/` layer:
# `get_commit`, `post_commit_comment`, `update_commit_status`.


def _commit_detail(sha: str, message: str, author: str, authored_at: str, url: str,
                   files: list[dict[str, Any]], branch: str | None = None,
                   truncated: bool = False) -> dict[str, Any]:
    """Mirrors sdlc's `CommitDetails`. `jiraTicketKeys` is deliberately absent —
    it is derived from `message` by the caller's own configured pattern."""
    return {"sha": sha or "", "message": message or "", "author": author or "",
            "authoredAt": authored_at or "", "url": url or "", "branch": branch,
            "files": files, "truncated": truncated}


# GitHub returns at most this many files on the single-commit endpoint and the
# endpoint cannot be paginated. Reaching it means the list is incomplete.
_GH_COMMIT_FILES_CAP = 300


def _github_commit(base_api: str, headers: dict[str, str], sha: str) -> dict[str, Any]:
    data = _diff_get(f"{base_api}/commits/{sha}", headers).json()
    c = data.get("commit") or {}
    author = c.get("author") or {}
    files = [_diff_file(f.get("filename", ""), f.get("status", ""), f.get("additions"),
                        f.get("deletions"), f.get("patch"), f.get("previous_filename"))
             for f in (data.get("files") or [])]
    return _commit_detail(data.get("sha") or sha, c.get("message"), author.get("name"),
                          author.get("date"), data.get("html_url"), files,
                          truncated=len(files) >= _GH_COMMIT_FILES_CAP)


def _gitlab_commit(base_api: str, headers: dict[str, str], sha: str) -> dict[str, Any]:
    data = _diff_get(f"{base_api}/repository/commits/{sha}", headers).json()
    # GitLab's commit-diff endpoint pages at 20 by default, so it MUST be paged —
    # a large commit otherwise silently reports only its first 20 files.
    diffs = _paged_commits(
        f"{base_api}/repository/commits/{sha}/diff", headers,
        lambda f: _diff_file(f.get("new_path") or f.get("old_path") or "", _gitlab_status(f),
                             None, None, f.get("diff"),
                             f.get("old_path") if f.get("renamed_file") else None),
    )
    return _commit_detail(data.get("id") or sha, data.get("message"), data.get("author_name"),
                          data.get("committed_date"), data.get("web_url"), diffs)


def _bitbucket_commit(base_api: str, headers: dict[str, str], sha: str) -> dict[str, Any]:
    data = _diff_get(f"{base_api}/commit/{sha}", headers).json()
    # diffstat carries counts but no patch text; the raw diff carries patches but
    # no counts. Fetch both and join them by filename.
    stats = _cursor_commits(f"{base_api}/commit/{sha}/diffstat?pagelen=100", headers, lambda f: f)
    patches = {f["filename"]: f.get("patch") for f in parse_raw_diff(
        _diff_get(f"{base_api}/commit/{sha}/diff", headers).text)}
    files = []
    for f in stats:
        new_path = (f.get("new") or {}).get("path")
        old_path = (f.get("old") or {}).get("path")
        name = new_path or old_path or ""
        files.append(_diff_file(name, f.get("status", ""), f.get("lines_added"),
                                f.get("lines_removed"), patches.get(name),
                                old_path if f.get("status") == "renamed" and old_path != new_path else None))
    author = data.get("author") or {}
    return _commit_detail(data.get("hash") or sha, data.get("message"),
                          author.get("raw") or (author.get("user") or {}).get("display_name"),
                          data.get("date"), ((data.get("links") or {}).get("html") or {}).get("href"),
                          files)


def _azure_render_patch(base_api: str, headers: dict[str, str],
                        before_id: str | None, after_id: str | None, path: str) -> str | None:
    """Azure exposes NO diff endpoint, so a patch has to be rendered locally from
    the before/after blobs. Returns None for binary or unreadable content."""
    import difflib

    def blob(object_id: str | None) -> str | None:
        if not object_id:
            return ""
        try:
            text = _diff_get(f"{base_api}/blobs/{object_id}", headers,
                             {**_ADO_PARAMS, "$format": "text"}).text
        except Exception:
            return None
        return None if "\x00" in text[:8192] else text  # NUL byte ⇒ binary

    before, after = blob(before_id), blob(after_id)
    if before is None or after is None:
        return None
    return "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                        after.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}")) or None


_AZ_CHANGE_STATUS = {"add": "added", "edit": "modified", "delete": "deleted", "rename": "renamed"}


def _azure_commit(base_api: str, headers: dict[str, str], sha: str) -> dict[str, Any]:
    params = dict(_ADO_PARAMS)
    data = _diff_get(f"{base_api}/commits/{sha}", headers, params).json()
    changes = _azure_paged(f"{base_api}/commits/{sha}/changes", headers, params, lambda c: c)
    files = []
    for ch in changes:
        item = ch.get("item") or {}
        if item.get("isFolder"):
            continue
        path = (item.get("path") or "").lstrip("/")
        change_type = (ch.get("changeType") or "edit").split(",")[0].strip()
        patch = _azure_render_patch(base_api, headers, (ch.get("originalObjectId")
                                                        or item.get("originalObjectId")),
                                    item.get("objectId"), path)
        adds = dels = None
        if patch:
            adds = len(re.findall(r"(?m)^\+[^+]", patch))
            dels = len(re.findall(r"(?m)^-[^-]", patch))
        files.append(_diff_file(path, _AZ_CHANGE_STATUS.get(change_type, change_type),
                                adds, dels, patch,
                                (ch.get("sourceServerItem") or "").lstrip("/") or None
                                if change_type == "rename" else None))
    author = data.get("author") or {}
    return _commit_detail(data.get("commitId") or sha, data.get("comment"), author.get("name"),
                          author.get("date"), data.get("remoteUrl"), files)


_COMMIT_DISPATCH = {
    "github": _github_commit,
    "gitlab": _gitlab_commit,
    "bitbucket": _bitbucket_commit,
    "azure_devops": _azure_commit,
}


def fetch_commit(repo_url: str, sha: str, token: str | None) -> dict[str, Any]:
    """A single commit with its file changes. Blocking — call off the event loop."""
    parsed, provider, base_api, headers = _resolve(repo_url, token)
    handler = _COMMIT_DISPATCH.get(provider)
    if handler is None:
        raise ApiError(f"Unsupported git provider: {provider}", 400)
    try:
        return handler(base_api, headers, sha)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"Failed to fetch commit from {provider}: {_scrub(str(exc))}", 502) from None


def post_commit_comment(repo_url: str, sha: str, body: str, token: str | None) -> dict[str, Any]:
    """Comment on a commit.

    Azure DevOps has **no commit-comment API**. It returns `{"posted": false}` with
    a reason rather than raising — callers must treat "not posted" as a real
    outcome, not assume success (the same contract sdlc's `AbstractSCMClient`
    documents for its `{}` return).
    """
    parsed, provider, base_api, headers = _resolve(repo_url, token)
    headers["Content-Type"] = "application/json"
    try:
        if provider == "github":
            _diff_post(f"{base_api}/commits/{sha}/comments", headers, {"body": body})
        elif provider == "gitlab":
            # GitLab names this field `note`, not `body`.
            _diff_post(f"{base_api}/repository/commits/{sha}/comments", headers, {"note": body})
        elif provider == "bitbucket":
            _diff_post(f"{base_api}/commit/{sha}/comments", headers, {"content": {"raw": body}})
        elif provider == "azure_devops":
            return {"posted": False, "provider": provider,
                    "reason": "Azure DevOps has no commit-comment API"}
        else:
            raise ApiError(f"Unsupported git provider: {provider}", 400)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"Failed to post commit comment to {provider}: {_scrub(str(exc))}", 502) from None
    return {"posted": True, "provider": provider}


_VALID_STATES = ("pending", "success", "failure", "error")
# Each provider spells the same four states differently, and each truncates the
# description at a different length. Getting either wrong is a 400 from the API.
_STATE_MAP = {
    "github": {s: s for s in _VALID_STATES},
    "gitlab": {"pending": "pending", "success": "success", "failure": "failed", "error": "failed"},
    "bitbucket": {"pending": "INPROGRESS", "success": "SUCCESSFUL",
                  "failure": "FAILED", "error": "FAILED"},
    "azure_devops": {"pending": "pending", "success": "succeeded",
                     "failure": "failed", "error": "error"},
}
_DESC_CAP = {"github": 140, "gitlab": 250, "bitbucket": 255, "azure_devops": 4000}


def update_commit_status(repo_url: str, sha: str, state: str, description: str,
                         context: str, target_url: str, token: str | None,
                         build_key: str | None = None) -> dict[str, Any]:
    """Post or update a commit status check.

    `state` is one of pending | success | failure | error; each provider's own
    vocabulary is applied internally. An unrecognised state is coerced to
    `pending` rather than rejected, matching the behaviour this replaces.

    Bitbucket requires a build key and has no sane default — without one it
    returns `{"posted": false}` rather than silently doing nothing.
    """
    parsed, provider, base_api, headers = _resolve(repo_url, token)
    headers["Content-Type"] = "application/json"
    mapped = _STATE_MAP[provider].get((state or "").lower(), _STATE_MAP[provider]["pending"])
    desc = (description or "")[:_DESC_CAP[provider]]

    try:
        if provider == "github":
            _diff_post(f"{base_api}/statuses/{sha}", headers,
                       {"state": mapped, "description": desc, "context": context,
                        "target_url": target_url})
        elif provider == "gitlab":
            _diff_post(f"{base_api}/statuses/{sha}", headers,
                       {"state": mapped, "description": desc, "name": context,
                        "target_url": target_url})
        elif provider == "bitbucket":
            if not build_key:
                return {"posted": False, "provider": provider,
                        "reason": "buildKey is required for Bitbucket build statuses"}
            _diff_post(f"{base_api}/commit/{sha}/statuses/build", headers,
                       {"key": build_key, "state": mapped, "name": context,
                        "description": desc, "url": target_url})
        elif provider == "azure_devops":
            # Azure splits the context into genre/name on "/".
            genre, _, name = context.rpartition("/")
            _diff_post(f"{base_api}/commits/{sha}/statuses", headers,
                       {"state": mapped, "description": desc,
                        "context": {"genre": genre or "breezeai", "name": name or context},
                        "targetUrl": target_url},
                       dict(_ADO_PARAMS))
        else:
            raise ApiError(f"Unsupported git provider: {provider}", 400)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"Failed to post commit status to {provider}: {_scrub(str(exc))}", 502) from None
    return {"posted": True, "provider": provider, "state": mapped}
