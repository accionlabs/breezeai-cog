"""`integrations/scm/gitlab.py` against a mock transport: PRIVATE-TOKEN auth, encoded
project path with nested groups, X-Next-Page pagination, compare mapping, raw files,
clone URL."""

from __future__ import annotations

import httpx
import pytest

from breezeai_cog.integrations.scm import CommitInfo, PrComment, PullRequestInfo, RepoRef, SCMAPIError
from breezeai_cog.integrations.scm.gitlab import GitLabSCMClient

REF = RepoRef("gitlab", "group/sub", "proj", host="gitlab.com")
PROJ = "group%2Fsub%2Fproj"
SHA = "a" * 40
BASE = "b" * 40


def _client(router, settings, token="glpat", **kw) -> GitLabSCMClient:
    return GitLabSCMClient(token, settings, transport=router.transport, sleep=lambda _s: None, **kw)


def test_private_token_header_and_encoded_project(router, settings) -> None:
    router.add(rf"/api/v4/projects/{PROJ}/repository/tree", httpx.Response(200, json=[]))
    _client(router, settings).tree(REF, SHA)
    assert router.requests[0].headers["PRIVATE-TOKEN"] == "glpat"
    assert router.urls[0].startswith(f"https://gitlab.com/api/v4/projects/{PROJ}/repository/tree?")


def test_anonymous_has_no_token_header(router, settings) -> None:
    router.add(r"/repository/tree", httpx.Response(200, json=[]))
    _client(router, settings, token=None).tree(REF, SHA)
    assert "PRIVATE-TOKEN" not in router.requests[0].headers


def test_tree_paginates_by_x_next_page(router, settings) -> None:
    def page1(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"type": "blob", "path": "a.py"}, {"type": "tree", "path": "src"},
        ], headers={"X-Next-Page": "2", "X-Total-Pages": "2"})

    router.add(r"page=2", httpx.Response(200, json=[{"type": "blob", "path": "src/b.py"}],
                                          headers={"X-Next-Page": ""}))
    router.add(r"/repository/tree\?", page1)
    assert _client(router, settings).tree(REF, SHA) == ["a.py", "src/b.py"]
    assert router.urls[0].endswith(f"/repository/tree?recursive=true&per_page=100&ref={SHA}")
    assert router.urls[1].endswith(f"/repository/tree?recursive=true&per_page=100&ref={SHA}&page=2")


def test_compare_maps_flags(router, settings) -> None:
    router.add(rf"/projects/{PROJ}/repository/compare\?from={BASE}&to={SHA}", httpx.Response(200, json={"diffs": [
        {"old_path": "a.py", "new_path": "a.py"},
        {"old_path": "b.py", "new_path": "b.py", "new_file": True},
        {"old_path": "gone.py", "new_path": "gone.py", "deleted_file": True},
        {"old_path": "old.py", "new_path": "new.py", "renamed_file": True},
    ]}))
    cs = _client(router, settings).compare(REF, BASE, SHA)
    assert cs.changed == ["a.py", "b.py", "new.py"]
    assert cs.deleted == ["gone.py", "old.py"]


def test_file_content_raw_endpoint_fully_encodes_path(router, settings) -> None:
    router.add(rf"/projects/{PROJ}/repository/files/src%2Fa%20b.py/raw\?ref={SHA}", httpx.Response(200, content=b"ok\n"))
    assert _client(router, settings).file_content(REF, "src/a b.py", SHA) == "ok\n"


def test_forbidden_carries_read_api_hint(router, settings) -> None:
    router.add(r"/repository/compare", httpx.Response(403, json={"message": "403 Forbidden"}))
    with pytest.raises(SCMAPIError, match="read_api"):
        _client(router, settings).compare(REF, BASE, SHA)


@pytest.mark.parametrize(("token", "expected"), [
    ("glpat", "https://oauth2:glpat@gitlab.com/group/sub/proj.git"),
    (None, "https://gitlab.com/group/sub/proj.git"),
])
def test_clone_url(router, settings, token, expected) -> None:
    assert _client(router, settings, token=token).clone_url(REF) == expected


def test_self_hosted_override_and_host(router, settings) -> None:
    ref = RepoRef("gitlab", "grp", "proj", host="git.acme.com")
    router.add(r"git\.acme\.com/api/v4/projects/grp%2Fproj/", httpx.Response(200, json=[]))
    c = _client(router, settings, api_base_url="https://git.acme.com/api/v4")
    c.tree(ref, SHA)
    assert router.urls[0].startswith("https://git.acme.com/api/v4/projects/grp%2Fproj/repository/tree")
    assert c.clone_url(ref) == "https://oauth2:glpat@git.acme.com/grp/proj.git"


def test_branch_head_encodes_branch_as_one_segment(router, settings) -> None:
    router.add(rf"/projects/{PROJ}/repository/branches/feature%2Fx$", httpx.Response(200, json={
        "name": "feature/x",
        "commit": {"id": SHA, "message": "wip", "author_name": "Carol",
                   "committed_date": "2026-09-03T00:00:00.000Z", "authored_date": "2026-09-02T00:00:00.000Z"},
    }))
    out = _client(router, settings).branch_head(REF, "feature/x")
    assert out == CommitInfo(SHA, "wip", "Carol", "2026-09-03T00:00:00.000Z")


def test_branch_head_missing_is_404(router, settings) -> None:
    router.add(r"/repository/branches/", httpx.Response(404, json={"message": "404 Branch Not Found"}))
    with pytest.raises(SCMAPIError) as info:
        _client(router, settings).branch_head(REF, "nope")
    assert info.value.http_status == 404


def test_pull_request_uses_diff_refs(router, settings) -> None:
    router.add(rf"/projects/{PROJ}/merge_requests/7$", httpx.Response(200, json={
        "iid": 7, "title": "Add x", "state": "opened", "web_url": "https://gitlab.com/group/sub/proj/-/merge_requests/7",
        "source_branch": "feat/x", "target_branch": "main", "sha": SHA,
        "diff_refs": {"base_sha": BASE, "head_sha": SHA, "start_sha": BASE},
    }))
    out = _client(router, settings).pull_request(REF, 7)
    assert out == PullRequestInfo(7, "Add x", "open", "main", "feat/x", BASE, SHA, "https://gitlab.com/group/sub/proj/-/merge_requests/7")
    assert len(router.requests) == 1


def test_pull_request_without_diff_refs_resolves_target_tip_to_a_sha(router, settings) -> None:
    """The backend fell back to the *branch name* here; a SHA is required."""
    router.add(r"/merge_requests/7$", httpx.Response(200, json={
        "iid": 7, "state": "merged", "source_branch": "feat/x", "target_branch": "main", "sha": SHA,
    }))
    router.add(r"/repository/branches/main$", httpx.Response(200, json={"commit": {"id": BASE}}))
    out = _client(router, settings).pull_request(REF, 7)
    assert out.state == "merged" and out.head_sha == SHA and out.base_sha == BASE
    assert router.urls[1].endswith("/repository/branches/main")


@pytest.mark.parametrize(("raw", "expected"), [("opened", "open"), ("merged", "merged"), ("closed", "closed"), ("locked", "closed")])
def test_pull_request_state_normalisation(router, settings, raw, expected) -> None:
    router.add(r"/merge_requests/", httpx.Response(200, json={"iid": 1, "state": raw, "sha": SHA, "diff_refs": {"base_sha": BASE, "head_sha": SHA}}))
    assert _client(router, settings).pull_request(REF, 1).state == expected


def test_post_pr_comment_posts_a_note(router, settings) -> None:
    import json as _json

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST" and _json.loads(req.content) == {"body": "hi"}
        return httpx.Response(201, json={"id": 31})

    router.add(rf"/projects/{PROJ}/merge_requests/7/notes$", handler)
    assert _client(router, settings).post_pr_comment(REF, 7, "hi") == PrComment("31", "")
