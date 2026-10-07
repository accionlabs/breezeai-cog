"""`integrations/scm/github.py` against a mock transport: auth header, endpoint shapes,
tree/compare mapping, raw content, binary rejection, and clone URLs."""

from __future__ import annotations

import httpx
import pytest

from breezeai_cog.integrations.scm import CommitInfo, PrComment, PullRequestInfo, RepoRef, SCMAPIError
from breezeai_cog.integrations.scm.github import GitHubSCMClient

REF = RepoRef("github", "acme", "widgets", host="github.com")
SHA = "a" * 40
BASE = "b" * 40


def _client(router, settings, token="tok", **kw) -> GitHubSCMClient:
    return GitHubSCMClient(token, settings, transport=router.transport, sleep=lambda _s: None, **kw)


def test_headers_and_base_url(router, settings) -> None:
    router.add(r"/git/trees/", httpx.Response(200, json={"tree": []}))
    _client(router, settings).tree(REF, SHA)
    req = router.requests[0]
    assert str(req.url).startswith("https://api.github.com/repos/acme/widgets/git/trees/")
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_anonymous_sends_no_authorization(router, settings) -> None:
    router.add(r"/git/trees/", httpx.Response(200, json={"tree": []}))
    _client(router, settings, token=None).tree(REF, SHA)
    assert "Authorization" not in router.requests[0].headers


def test_enterprise_base_url_override(router, settings) -> None:
    router.add(r"ghe\.acme\.com/api/v3/repos/", httpx.Response(200, json={"tree": []}))
    _client(router, settings, api_base_url="https://ghe.acme.com/api/v3/").tree(REF, SHA)
    assert router.urls[0].startswith("https://ghe.acme.com/api/v3/repos/acme/widgets/")


def test_tree_returns_blob_paths_only(router, settings) -> None:
    router.add(r"/git/trees/" + SHA + r"\?recursive=1", httpx.Response(200, json={
        "tree": [
            {"path": "src", "type": "tree"},
            {"path": "src/a.py", "type": "blob"},
            {"path": "README.md", "type": "blob"},
            {"path": "sub", "type": "commit"},
        ],
    }))
    assert _client(router, settings).tree(REF, SHA) == ["src/a.py", "README.md"]


def test_compare_maps_statuses_and_follows_link_pages(router, settings) -> None:
    page2 = "https://api.github.com/repos/acme/widgets/compare/x...y?per_page=100&page=2"

    def first(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": [
            {"filename": "a.py", "status": "modified"},
            {"filename": "b.py", "status": "added"},
            {"filename": "gone.py", "status": "removed"},
        ]}, headers={"Link": f'<{page2}>; rel="next", <x>; rel="last"'})

    router.add(r"page=2", httpx.Response(200, json={"files": [
        {"filename": "new/name.py", "status": "renamed", "previous_filename": "old/name.py"},
    ]}))
    router.add(r"/compare/" + BASE + r"\.\.\." + SHA, first)
    cs = _client(router, settings).compare(REF, BASE, SHA)
    assert cs.changed == ["a.py", "b.py", "new/name.py"]
    assert cs.deleted == ["gone.py", "old/name.py"]
    assert router.urls[0].endswith(f"/compare/{BASE}...{SHA}?per_page=100")
    assert router.urls[1] == page2


def test_compare_empty_is_empty_changeset(router, settings) -> None:
    router.add(r"/compare/", httpx.Response(200, json={"files": []}))
    assert _client(router, settings).compare(REF, BASE, SHA).is_empty


def test_file_content_uses_raw_media_type_and_encodes_path(router, settings) -> None:
    router.add(r"/contents/src/my%20file%20%2B.py\?ref=" + SHA, httpx.Response(200, content="print('hi')\n".encode()))
    out = _client(router, settings).file_content(REF, "src/my file +.py", SHA)
    assert out == "print('hi')\n"
    assert router.requests[0].headers["Accept"] == "application/vnd.github.raw+json"


def test_binary_content_is_rejected(router, settings) -> None:
    router.add(r"/contents/", httpx.Response(200, content=b"\x89PNG\r\n\x1a\n\x00\xff"))
    with pytest.raises(SCMAPIError, match="non-UTF-8"):
        _client(router, settings).file_content(REF, "logo.png", SHA)


def test_api_failure_is_scm_api_error_without_body(router, settings) -> None:
    router.add(r"/git/trees/", httpx.Response(404, json={"message": "Not Found tok"}))
    with pytest.raises(SCMAPIError) as info:
        _client(router, settings).tree(REF, SHA)
    assert info.value.http_status == 404 and "Not Found tok" not in str(info.value)


@pytest.mark.parametrize(("token", "expected"), [
    ("tok", "https://x-access-token:tok@github.com/acme/widgets.git"),
    (None, "https://github.com/acme/widgets.git"),
])
def test_clone_url(router, settings, token, expected) -> None:
    assert _client(router, settings, token=token).clone_url(REF) == expected


def test_clone_url_follows_self_hosted_host(router, settings) -> None:
    ref = RepoRef("github", "acme", "widgets", host="ghe.acme.com")
    assert _client(router, settings).clone_url(ref) == "https://x-access-token:tok@ghe.acme.com/acme/widgets.git"


def test_context_manager_closes(router, settings) -> None:
    with _client(router, settings) as c:
        assert c.supports_incremental and c.provider == "github"


def test_branch_head_single_call_with_slash_in_branch(router, settings) -> None:
    router.add(r"/repos/acme/widgets/branches/feature/x$", httpx.Response(200, json={
        "name": "feature/x",
        "commit": {
            "sha": SHA,
            "html_url": "https://github.com/acme/widgets/commit/" + SHA,
            "author": {"login": "alice"},
            "commit": {"message": "feat: x\n\nbody", "author": {"name": "Alice", "date": "2026-09-01T10:00:00Z"}},
        },
    }))
    out = _client(router, settings).branch_head(REF, "feature/x")
    assert out == CommitInfo(SHA, "feat: x\n\nbody", "Alice", "2026-09-01T10:00:00Z")
    assert len(router.requests) == 1  # no second /commits/{sha} round trip


def test_branch_head_missing_branch_is_404_error(router, settings) -> None:
    router.add(r"/branches/", httpx.Response(404, json={"message": "Branch not found"}))
    with pytest.raises(SCMAPIError) as info:
        _client(router, settings).branch_head(REF, "nope")
    assert info.value.http_status == 404


def _pr_body(state="open", merged_at=None):
    return {
        "number": 7, "title": "Add x", "state": state, "merged_at": merged_at,
        "html_url": "https://github.com/acme/widgets/pull/7",
        "base": {"ref": "main", "sha": BASE}, "head": {"ref": "feat/x", "sha": SHA},
    }


def test_pull_request_maps_fields(router, settings) -> None:
    router.add(r"/repos/acme/widgets/pulls/7$", httpx.Response(200, json=_pr_body()))
    out = _client(router, settings).pull_request(REF, 7)
    assert out == PullRequestInfo(7, "Add x", "open", "main", "feat/x", BASE, SHA, "https://github.com/acme/widgets/pull/7")


@pytest.mark.parametrize(("state", "merged_at", "expected"), [
    ("open", None, "open"), ("closed", None, "closed"), ("closed", "2026-09-01T00:00:00Z", "merged"),
])
def test_pull_request_state_normalisation(router, settings, state, merged_at, expected) -> None:
    router.add(r"/pulls/7$", httpx.Response(200, json=_pr_body(state, merged_at)))
    assert _client(router, settings).pull_request(REF, 7).state == expected


def test_pull_request_missing_is_404_error(router, settings) -> None:
    router.add(r"/pulls/", httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(SCMAPIError) as info:
        _client(router, settings).pull_request(REF, 999)
    assert info.value.http_status == 404


def test_post_pr_comment_uses_issue_comments(router, settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST" and req.content == b'{"body":"hi"}' or b'"body": "hi"' in req.content
        return httpx.Response(201, json={"id": 55, "html_url": "https://github.com/acme/widgets/pull/7#issuecomment-55"})

    router.add(r"/repos/acme/widgets/issues/7/comments$", handler)
    out = _client(router, settings).post_pr_comment(REF, 7, "hi")
    assert out == PrComment("55", "https://github.com/acme/widgets/pull/7#issuecomment-55")
