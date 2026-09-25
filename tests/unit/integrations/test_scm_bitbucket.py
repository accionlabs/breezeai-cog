"""`integrations/scm/bitbucket.py` against a mock transport: credential validation,
Basic auth, body-cursor pagination, reversed diffstat order, renames, clone URL."""

from __future__ import annotations

import base64

import httpx
import pytest

from breezeai_cog.integrations.scm import CommitInfo, PrComment, PullRequestInfo, RepoRef, SCMAPIError, SCMCredentialError
from breezeai_cog.integrations.scm.bitbucket import BitbucketSCMClient

REF = RepoRef("bitbucket", "acme", "widgets", host="bitbucket.org")
SHA = "a" * 40
BASE = "b" * 40
API = "https://api.bitbucket.org/2.0"


def _client(router, settings, token="user:key", **kw) -> BitbucketSCMClient:
    return BitbucketSCMClient(token, settings, transport=router.transport, sleep=lambda _s: None, **kw)


def test_credential_without_colon_is_a_400(settings) -> None:
    with pytest.raises(SCMCredentialError) as info:
        BitbucketSCMClient("justakey", settings)
    assert info.value.status_code == 400 and "username:api_key" in str(info.value)


def test_basic_auth_header(router, settings) -> None:
    router.add(r"/src/", httpx.Response(200, json={"values": []}))
    _client(router, settings).tree(REF, SHA)
    expected = "Basic " + base64.b64encode(b"user:key").decode()
    assert router.requests[0].headers["Authorization"] == expected


def test_anonymous_has_no_auth_and_plain_clone_url(router, settings) -> None:
    router.add(r"/src/", httpx.Response(200, json={"values": []}))
    c = _client(router, settings, token=None)
    c.tree(REF, SHA)
    assert "Authorization" not in router.requests[0].headers
    assert c.clone_url(REF) == "https://bitbucket.org/acme/widgets.git"


def test_tree_follows_body_next_cursor(router, settings) -> None:
    nxt = f"{API}/repositories/acme/widgets/src/{SHA}/?pagelen=100&max_depth=100&page=2"
    router.add(r"page=2", httpx.Response(200, json={"values": [
        {"type": "commit_file", "path": "b.py"},
    ]}))
    router.add(rf"/repositories/acme/widgets/src/{SHA}/\?pagelen=100&max_depth=100$", httpx.Response(200, json={
        "values": [
            {"type": "commit_file", "path": "a.py"},
            {"type": "commit_directory", "path": "src"},
            {"type": "commit_file"},
        ],
        "next": nxt,
    }))
    assert _client(router, settings).tree(REF, SHA) == ["a.py", "b.py"]
    assert router.urls[1] == nxt


def test_compare_uses_reversed_spec_and_maps_renames(router, settings) -> None:
    router.add(rf"/diffstat/{SHA}\.\.{BASE}\?pagelen=100", httpx.Response(200, json={"values": [
        {"status": "modified", "new": {"path": "a.py"}, "old": {"path": "a.py"}},
        {"status": "added", "new": {"path": "b.py"}, "old": None},
        {"status": "removed", "new": None, "old": {"path": "gone.py"}},
        {"status": "renamed", "new": {"path": "new.py"}, "old": {"path": "old.py"}},
    ]}))
    cs = _client(router, settings).compare(REF, BASE, SHA)
    assert cs.changed == ["a.py", "b.py", "new.py"]
    assert cs.deleted == ["gone.py", "old.py"]


def test_file_content_encodes_each_segment(router, settings) -> None:
    router.add(rf"/src/{SHA}/src/a%20b/c%2Bd.py$", httpx.Response(200, content=b"x = 1\n"))
    assert _client(router, settings).file_content(REF, "src/a b/c+d.py", SHA) == "x = 1\n"


def test_file_content_binary_rejected(router, settings) -> None:
    router.add(r"/src/", httpx.Response(200, content=b"\xff\xfe\x00"))
    with pytest.raises(SCMAPIError, match="non-UTF-8"):
        _client(router, settings).file_content(REF, "bin", SHA)


def test_clone_url_uses_api_key_only(router, settings) -> None:
    c = _client(router, settings, token="alice:s3cret")
    assert c.clone_url(REF) == "https://x-bitbucket-api-token-auth:s3cret@bitbucket.org/acme/widgets.git"


def test_clone_url_follows_self_hosted_host(router, settings) -> None:
    ref = RepoRef("bitbucket", "acme", "widgets", host="bb.acme.com")
    assert _client(router, settings).clone_url(ref) == "https://x-bitbucket-api-token-auth:key@bb.acme.com/acme/widgets.git"


def test_server_base_url_override(router, settings) -> None:
    router.add(r"bb\.acme\.com/rest/api/2\.0/repositories/", httpx.Response(200, json={"values": []}))
    _client(router, settings, api_base_url="https://bb.acme.com/rest/api/2.0").tree(REF, SHA)
    assert router.urls[0].startswith("https://bb.acme.com/rest/api/2.0/repositories/acme/widgets/src/")


def test_branch_head_reads_target(router, settings) -> None:
    router.add(r"/repositories/acme/widgets/refs/branches/release/1.0$", httpx.Response(200, json={
        "name": "release/1.0",
        "target": {
            "hash": SHA, "message": "release", "date": "2026-09-02T00:00:00+00:00",
            "author": {"raw": "Bob <bob@acme.com>", "user": {"display_name": "Bob"}},
        },
    }))
    out = _client(router, settings).branch_head(REF, "release/1.0")
    assert out == CommitInfo(SHA, "release", "Bob <bob@acme.com>", "2026-09-02T00:00:00+00:00")


def test_branch_head_falls_back_to_display_name(router, settings) -> None:
    router.add(r"/refs/branches/", httpx.Response(200, json={
        "target": {"hash": SHA, "author": {"user": {"display_name": "Bob"}}},
    }))
    assert _client(router, settings).branch_head(REF, "main").author == "Bob"


def test_pull_request_resolves_abbreviated_hashes(router, settings) -> None:
    router.add(r"/pullrequests/7$", httpx.Response(200, json={
        "id": 7, "title": "Add x", "state": "OPEN",
        "source": {"branch": {"name": "feat/x"}, "commit": {"hash": SHA[:12]}},
        "destination": {"branch": {"name": "main"}, "commit": {"hash": BASE[:12]}},
        "links": {"html": {"href": "https://bitbucket.org/acme/widgets/pull-requests/7"}},
    }))
    router.add(rf"/commit/{SHA[:12]}$", httpx.Response(200, json={"hash": SHA}))
    router.add(rf"/commit/{BASE[:12]}$", httpx.Response(200, json={"hash": BASE}))
    out = _client(router, settings).pull_request(REF, 7)
    assert out == PullRequestInfo(7, "Add x", "open", "main", "feat/x", BASE, SHA, "https://bitbucket.org/acme/widgets/pull-requests/7")
    assert len(router.requests) == 3


def test_pull_request_full_hashes_need_no_extra_calls(router, settings) -> None:
    router.add(r"/pullrequests/7$", httpx.Response(200, json={
        "id": 7, "state": "MERGED",
        "source": {"branch": {"name": "f"}, "commit": {"hash": SHA}},
        "destination": {"branch": {"name": "main"}, "commit": {"hash": BASE}},
    }))
    out = _client(router, settings).pull_request(REF, 7)
    assert out.state == "merged" and out.head_sha == SHA and out.base_sha == BASE
    assert len(router.requests) == 1


@pytest.mark.parametrize(("raw", "expected"), [("OPEN", "open"), ("MERGED", "merged"), ("DECLINED", "closed"), ("SUPERSEDED", "closed")])
def test_pull_request_state_normalisation(router, settings, raw, expected) -> None:
    router.add(r"/pullrequests/", httpx.Response(200, json={"id": 1, "state": raw, "source": {"commit": {"hash": SHA}}, "destination": {"commit": {"hash": BASE}}}))
    assert _client(router, settings).pull_request(REF, 1).state == expected


def test_post_pr_comment_wraps_content_raw(router, settings) -> None:
    import json as _json

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST" and _json.loads(req.content) == {"content": {"raw": "hi"}}
        return httpx.Response(201, json={"id": 8, "links": {"html": {"href": "https://bitbucket.org/acme/widgets/pull-requests/7/_/diff#comment-8"}}})

    router.add(r"/pullrequests/7/comments$", handler)
    out = _client(router, settings).post_pr_comment(REF, 7, "hi")
    assert out == PrComment("8", "https://bitbucket.org/acme/widgets/pull-requests/7/_/diff#comment-8")
