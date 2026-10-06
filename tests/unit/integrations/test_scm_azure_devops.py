"""`integrations/scm/azure_devops.py` against a mock transport, shaped by the 7.1 REST
reference: PAT Basic auth, repository base path, unpaginated recursive items list with
leading-slash paths, $skip-paged diffs with flag-style changeType, rename old paths,
octet-stream content, and host-aware clone URLs."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from breezeai_cog.integrations.scm import CommitInfo, PrComment, PullRequestInfo, RepoRef, SCMAPIError
from breezeai_cog.integrations.scm.azure_devops import AzureDevOpsSCMClient

REF = RepoRef("azure_devops", "my-org", "my-repo", project="my project", host="dev.azure.com")
BASE_PATH = "/my-org/my%20project/_apis/git/repositories/my-repo"
SHA = "a" * 40
BASE = "b" * 40


def _client(router, settings, token="pat123", **kw) -> AzureDevOpsSCMClient:
    return AzureDevOpsSCMClient(token, settings, transport=router.transport, sleep=lambda _s: None, **kw)


def _qs(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_pat_basic_auth_with_empty_username(router, settings) -> None:
    router.add(r"/items\?", httpx.Response(200, json={"count": 0, "value": []}))
    _client(router, settings).tree(REF, SHA)
    expected = "Basic " + base64.b64encode(b":pat123").decode()
    assert router.requests[0].headers["Authorization"] == expected


def test_anonymous_has_no_auth(router, settings) -> None:
    router.add(r"/items\?", httpx.Response(200, json={"count": 0, "value": []}))
    _client(router, settings, token=None).tree(REF, SHA)
    assert "Authorization" not in router.requests[0].headers


def test_tree_request_shape_and_blob_filtering(router, settings) -> None:
    router.add(r"/items\?", httpx.Response(200, json={"count": 4, "value": [
        {"gitObjectType": "tree", "path": "/", "isFolder": True},
        {"gitObjectType": "tree", "path": "/src", "isFolder": True},
        {"gitObjectType": "blob", "path": "/src/App.cs"},
        {"gitObjectType": "blob", "path": "/README.md"},
    ]}))
    assert _client(router, settings).tree(REF, SHA) == ["src/App.cs", "README.md"]
    url = router.urls[0]
    assert url.startswith(f"https://dev.azure.com{BASE_PATH}/items?")
    assert _qs(url) == {
        "recursionLevel": "full",
        "versionDescriptor.version": SHA,
        "versionDescriptor.versionType": "commit",
        "api-version": "7.1",
    }


def test_compare_request_shape_and_change_mapping(router, settings) -> None:
    router.add(r"/diffs/commits\?", httpx.Response(200, json={
        "allChangesIncluded": True,
        "changes": [
            {"item": {"gitObjectType": "tree", "path": "/src", "isFolder": True}, "changeType": "edit"},
            {"item": {"gitObjectType": "blob", "path": "/src/a.cs"}, "changeType": "edit"},
            {"item": {"gitObjectType": "blob", "path": "/src/b.cs"}, "changeType": "add"},
            {"item": {"gitObjectType": "blob", "path": "/gone.cs"}, "changeType": "delete"},
            {"item": {"gitObjectType": "blob", "path": "/new.cs"}, "changeType": "rename",
             "sourceServerItem": "/old.cs"},
            {"item": {"gitObjectType": "blob", "path": "/moved.cs"}, "changeType": "edit, rename",
             "sourceServerItem": "/was.cs"},
        ],
    }))
    cs = _client(router, settings).compare(REF, BASE, SHA)
    assert cs.changed == ["src/a.cs", "src/b.cs", "new.cs", "moved.cs"]
    assert cs.deleted == ["gone.cs", "old.cs", "was.cs"]
    q = _qs(router.urls[0])
    assert q["baseVersion"] == BASE and q["baseVersionType"] == "commit"
    assert q["targetVersion"] == SHA and q["targetVersionType"] == "commit"
    assert q["$top"] == "500" and q["api-version"] == "7.1" and "$skip" not in q


def test_compare_pages_by_skip_until_all_changes_included(router, settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        skip = int(req.url.params.get("$skip", "0"))
        if skip == 0:
            changes = [{"item": {"gitObjectType": "blob", "path": f"/f{i}.cs"}, "changeType": "edit"} for i in range(500)]
            return httpx.Response(200, json={"allChangesIncluded": False, "changes": changes})
        return httpx.Response(200, json={"allChangesIncluded": True, "changes": [
            {"item": {"gitObjectType": "blob", "path": "/last.cs"}, "changeType": "add"},
        ]})

    router.add(r"/diffs/commits\?", handler)
    cs = _client(router, settings).compare(REF, BASE, SHA)
    assert len(cs.changed) == 501 and cs.changed[-1] == "last.cs"
    assert [_qs(u).get("$skip") for u in router.urls] == [None, "500"]


def test_compare_empty(router, settings) -> None:
    router.add(r"/diffs/commits\?", httpx.Response(200, json={"allChangesIncluded": True, "changes": []}))
    assert _client(router, settings).compare(REF, BASE, SHA).is_empty


def test_file_content_request_shape(router, settings) -> None:
    router.add(r"/items\?", httpx.Response(200, content=b"class A {}\n"))
    assert _client(router, settings).file_content(REF, "src/A b.cs", SHA) == "class A {}\n"
    req = router.requests[0]
    assert req.headers["Accept"] == "application/octet-stream"
    assert _qs(str(req.url)) == {
        "path": "/src/A b.cs",
        "versionDescriptor.version": SHA,
        "versionDescriptor.versionType": "commit",
        "download": "true",
        "$format": "octetStream",
        "api-version": "7.1",
    }


def test_file_content_binary_rejected(router, settings) -> None:
    router.add(r"/items\?", httpx.Response(200, content=b"PK\x03\x04\xff\xfe"))
    with pytest.raises(SCMAPIError, match="non-UTF-8"):
        _client(router, settings).file_content(REF, "a.zip", SHA)


def test_api_error_has_no_body(router, settings) -> None:
    router.add(r"/items\?", httpx.Response(401, text="TF400813: pat123 not authorized"))
    with pytest.raises(SCMAPIError) as info:
        _client(router, settings).tree(REF, SHA)
    assert info.value.http_status == 401 and "pat123" not in str(info.value)


@pytest.mark.parametrize(("ref", "token", "expected"), [
    (REF, "pat123", "https://pat:pat123@dev.azure.com/my-org/my project/_git/my-repo"),
    (REF, None, "https://dev.azure.com/my-org/my project/_git/my-repo"),
    (
        RepoRef("azure_devops", "my-org", "my-repo", project="proj", host="my-org.visualstudio.com"),
        "pat123",
        "https://pat:pat123@my-org.visualstudio.com/proj/_git/my-repo",
    ),
    (
        RepoRef("azure_devops", "DefaultCollection", "my-repo", project="proj", host="ado.acme.com"),
        "pat123",
        "https://pat:pat123@ado.acme.com/DefaultCollection/proj/_git/my-repo",
    ),
])
def test_clone_url_follows_host(router, settings, ref, token, expected) -> None:
    assert _client(router, settings, token=token).clone_url(ref) == expected


def test_server_base_url_override_uses_collection_as_org(router, settings) -> None:
    ref = RepoRef("azure_devops", "DefaultCollection", "my-repo", project="proj", host="ado.acme.com")
    router.add(r"ado\.acme\.com/DefaultCollection/proj/_apis/git/repositories/my-repo/items", httpx.Response(200, json={"value": []}))
    _client(router, settings, api_base_url="https://ado.acme.com/").tree(ref, SHA)
    assert router.urls[0].startswith("https://ado.acme.com/DefaultCollection/proj/_apis/git/repositories/my-repo/items?")


def test_branch_head_selects_exact_ref_then_fetches_commit(router, settings) -> None:
    """`filter` is starts-with, so `heads/main` also returns `main-old`; pick the exact name."""
    router.add(r"/refs\?", httpx.Response(200, json={"value": [
        {"name": "refs/heads/main-old", "objectId": "b" * 40},
        {"name": "refs/heads/main", "objectId": SHA},
    ], "count": 2}))
    router.add(rf"/commits/{SHA}\?", httpx.Response(200, json={
        "commitId": SHA, "comment": "Merged PR 12", "author": {"name": "Dana", "date": "2026-09-04T00:00:00Z"},
    }))
    out = _client(router, settings).branch_head(REF, "main")
    assert out == CommitInfo(SHA, "Merged PR 12", "Dana", "2026-09-04T00:00:00Z")
    assert _qs(router.urls[0]) == {"filter": "heads/main", "api-version": "7.1"}
    assert router.urls[1].startswith(f"https://dev.azure.com{BASE_PATH}/commits/{SHA}?")


def test_branch_head_missing_branch_is_404_without_second_call(router, settings) -> None:
    router.add(r"/refs\?", httpx.Response(200, json={"value": [
        {"name": "refs/heads/main-old", "objectId": "b" * 40},
    ], "count": 1}))
    with pytest.raises(SCMAPIError) as info:
        _client(router, settings).branch_head(REF, "main")
    assert info.value.http_status == 404 and "'main' not found" in str(info.value)
    assert len(router.requests) == 1


def test_pull_request_maps_source_to_head_and_target_to_base(router, settings) -> None:
    """Per the 7.1 reference: lastMergeSourceCommit = head of the *source* branch (PR head),
    lastMergeTargetCommit = head of the *target* branch (base). The backend had them swapped."""
    router.add(r"/pullrequests/7\?", httpx.Response(200, json={
        "pullRequestId": 7, "title": "Add x", "status": "active",
        "sourceRefName": "refs/heads/feat/x", "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": SHA}, "lastMergeTargetCommit": {"commitId": BASE},
        "_links": {"web": {"href": "https://dev.azure.com/my-org/my%20project/_git/my-repo/pullrequest/7"}},
    }))
    out = _client(router, settings).pull_request(REF, 7)
    assert out == PullRequestInfo(7, "Add x", "open", "main", "feat/x", BASE, SHA, "https://dev.azure.com/my-org/my%20project/_git/my-repo/pullrequest/7")
    assert _qs(router.urls[0]) == {"api-version": "7.1"}
    assert router.urls[0].startswith(f"https://dev.azure.com{BASE_PATH}/pullrequests/7?")


@pytest.mark.parametrize(("raw", "expected"), [("active", "open"), ("completed", "merged"), ("abandoned", "closed")])
def test_pull_request_state_normalisation(router, settings, raw, expected) -> None:
    router.add(r"/pullrequests/", httpx.Response(200, json={"pullRequestId": 1, "status": raw}))
    assert _client(router, settings).pull_request(REF, 1).state == expected


def test_post_pr_comment_creates_a_thread(router, settings) -> None:
    """Azure has no flat comment endpoint: a comment is the first entry of a new thread.
    The backend used to POST {body} here, which Azure rejects."""
    import json as _json

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert _json.loads(req.content) == {"comments": [{"parentCommentId": 0, "content": "hi", "commentType": 1}], "status": 1}
        assert req.url.params["api-version"] == "7.1"
        return httpx.Response(200, json={"id": 4, "_links": {"self": {"href": "https://dev.azure.com/x/threads/4"}}, "comments": [{"id": 1}]})

    router.add(rf"{BASE_PATH}/pullrequests/7/threads\?", handler)
    out = _client(router, settings).post_pr_comment(REF, 7, "hi")
    assert out == PrComment("4", "https://dev.azure.com/x/threads/4")
