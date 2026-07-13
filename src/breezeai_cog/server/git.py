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


def parse_repo_url(repo_url: str) -> dict[str, str] | None:
    gh = _GITHUB.search(repo_url)
    if gh:
        return {"provider": "github", "owner": gh.group(1), "repo": gh.group(2)}
    bb = _BITBUCKET.search(repo_url)
    if bb:
        return {"provider": "bitbucket", "owner": bb.group(1), "repo": bb.group(2)}
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


def _provider(provider: str) -> dict[str, Any]:
    if provider == "github":
        return {"tree": _gh_tree, "compare": _gh_compare, "content": _gh_content}
    if provider == "bitbucket":
        return {"tree": _bb_tree, "compare": _bb_compare, "content": _bb_content}
    raise ApiError(f"Unsupported git provider: {provider}", 400)


def _auth_clone_url(provider: str, owner: str, repo: str, token: str | None) -> str:
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
    raise ApiError(f"Unsupported git provider: {provider}", 400)


def clone_repo_full(provider: str, owner: str, repo: str, incoming: str, branch: str, token: str | None) -> str:
    temp_dir = tempfile.mkdtemp(prefix="ontology-clone-")
    auth_url = _auth_clone_url(provider, owner, repo, token)
    try:
        subprocess.run(["git", "clone", "--branch", branch, "--single-branch", auth_url, temp_dir],
                       check=True, capture_output=True)
        if incoming:
            subprocess.run(["git", "-C", temp_dir, "checkout", "--quiet", incoming],
                           check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if exc.stderr else str(exc)
        raise RuntimeError(f"git clone failed: {_scrub(stderr)}")
    import shutil

    shutil.rmtree(Path(temp_dir) / ".git", ignore_errors=True)
    return temp_dir


def resolve_git_diff(provider: str, owner: str, repo: str, current: str, incoming: str,
                     token: str | None) -> tuple[str, set[str], list[str]]:
    api = _provider(provider)
    skeleton = api["tree"](owner, repo, incoming, token)
    diff = api["compare"](owner, repo, current, incoming, token)
    changed, deleted = diff["changed"], diff["deleted"]
    if not changed and not deleted:
        raise ApiError("No changed files found between the two commits", 422)

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
        raise ApiError("Invalid repo URL (supported hosts: github.com, bitbucket.org)", 400)
    provider, owner, repo = parsed["provider"], parsed["owner"], parsed["repo"]
    current = body.get("currentCommitId")
    incoming = body["incomingCommitId"]
    token = body.get("gitToken")
    has_current = current not in (None, "", "null", "undefined")

    if has_current:
        return resolve_git_diff(provider, owner, repo, current, incoming, token)
    temp_dir = clone_repo_full(provider, owner, repo, incoming, body["gitBranch"], token)
    return temp_dir, None, []  # full clone → process every file
