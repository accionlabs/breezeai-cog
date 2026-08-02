"""Git source acquisition for ``/api/analyze-diff`` (server-only). Port of the
``server.js`` provider/clone/diff helpers: first-time analysis does a full ``git clone``
(avoids per-file API rate limits); incremental analysis pulls only the changed files via
the provider REST API (GitHub or Bitbucket). Returns a populated temp dir + the changed-
file filter set + the deleted-file list.

The returned temp dir is the caller's to clean up after streaming."""

from __future__ import annotations

import base64
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..config import Settings
from .errors import ApiError

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
        raise RuntimeError(f"git clone timed out: {_scrub(str(exc))}")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if exc.stderr else str(exc)
        raise RuntimeError(f"git clone failed: {_scrub(stderr)}")
    import shutil

    shutil.rmtree(Path(temp_dir) / ".git", ignore_errors=True)
    return temp_dir


def resolve_git_diff(provider: str, owner: str, project: str, repo: str, current: str, incoming: str,
                     token: str | None, timeout: float = 1800.0) -> tuple[str, set[str], list[str]]:
    api = _provider(provider)
    skeleton = api["tree"](owner, repo, incoming, token)
    diff = api["compare"](owner, repo, current, incoming, token)
    changed, deleted = diff["changed"], diff["deleted"]
    if provider in ("gitlab", "azure_devops") or (not changed and not deleted):
        # Fallback to full clone for providers where diff APIs are bypassed
        temp_dir = clone_repo_full(provider, owner, project, repo, incoming, incoming, token, timeout)
        return temp_dir, None, []  # type: ignore

    temp_dir = tempfile.mkdtemp(prefix="ontology-")
    for sp in skeleton:
        full = Path(temp_dir) / sp
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("")
    filter_set: set[str] = set()
    for path in changed:
        try:
            content = api["content"](owner, repo, path, incoming, token)
        except Exception:
            continue  # binary/unreadable
        full = Path(temp_dir) / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        filter_set.add(path)
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
        return resolve_git_diff(provider, owner, project, repo, current, incoming, token, settings.git_clone_timeout)
    temp_dir = clone_repo_full(provider, owner, project, repo, incoming, body["gitBranch"], token, settings.git_clone_timeout)
    return temp_dir, None, []  # full clone → process every file
