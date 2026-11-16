"""`integrations/scm/bitbucket_server.py` against a mock transport: credential handling,
verbatim Basic auth, start/limit pagination, change-type mapping, epoch-millis dates and
the context-path clone URL.

The response bodies here encode REST 1.0 as documented; the three shapes flagged in the
module (`/files`, `/compare/changes`, `/pull-requests/{id}`) are confirmed against a live
instance on first run.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from breezeai_cog.integrations.scm import CommitInfo, RepoRef, SCMAPIError, SCMCredentialError
from breezeai_cog.integrations.scm.bitbucket_server import BitbucketServerSCMClient

REF = RepoRef(
    "bitbucket_server", "p3", "zaike-non-unity-kit-create",
    host="stash.payroll.co.jp", context_path="/stash",
)
API = "https://stash.payroll.co.jp/stash/rest/api/1.0"
REPO = f"{API}/projects/p3/repos/zaike-non-unity-kit-create"
SHA = "a" * 40
BASE = "b" * 40
TOKEN = base64.b64encode(b"jdoe:s3cr3t").decode()


def _client(router, settings, token=TOKEN, base=API, **kw) -> BitbucketServerSCMClient:
    return BitbucketServerSCMClient(
        token, settings, base, transport=router.transport, sleep=lambda _s: None, **kw
    )


# ── Credential ─────────────────────────────────────────────────────────────

def test_missing_credential_is_a_400(settings) -> None:
    with pytest.raises(SCMCredentialError) as info:
        BitbucketServerSCMClient(None, settings, API)
    assert info.value.status_code == 400 and "gitToken" in str(info.value)


def test_raw_unencoded_token_is_a_400(settings) -> None:
    with pytest.raises(SCMCredentialError) as info:
        BitbucketServerSCMClient("jdoe:s3cr3t", settings, API)
    assert "base64" in str(info.value)


def test_base64_without_a_colon_is_a_400(settings) -> None:
    with pytest.raises(SCMCredentialError):
        BitbucketServerSCMClient(base64.b64encode(b"nocolon").decode(), settings, API)


def test_missing_api_base_url_is_a_400(settings) -> None:
    with pytest.raises(SCMCredentialError) as info:
        BitbucketServerSCMClient(TOKEN, settings, None)
    assert "/rest/api/1.0" in str(info.value)


def test_basic_header_is_passed_through_not_re_encoded(router, settings) -> None:
    router.add(r"/files", httpx.Response(200, json={"values": [], "isLastPage": True}))
    _client(router, settings).tree(REF, SHA)
    assert router.requests[0].headers["Authorization"] == f"Basic {TOKEN}"


# ── tree ───────────────────────────────────────────────────────────────────

def test_tree_follows_start_limit_pagination(router, settings) -> None:
    router.add(r"start=2", httpx.Response(200, json={
        "values": ["src/c.py"], "isLastPage": True,
    }))
    router.add(r"/files", httpx.Response(200, json={
        "values": ["src/a.py", "src/b.py"], "isLastPage": False, "nextPageStart": 2,
    }))
    assert _client(router, settings).tree(REF, SHA) == ["src/a.py", "src/b.py", "src/c.py"]
    assert router.urls[0] == f"{REPO}/files?at={SHA}&limit=1000"
    assert f"at={SHA}" in router.urls[1] and "start=2" in router.urls[1]


def test_tree_stops_when_is_last_page_is_absent(router, settings) -> None:
    router.add(r"/files", httpx.Response(200, json={"values": ["a.py"]}))
    assert _client(router, settings).tree(REF, SHA) == ["a.py"]
    assert len(router.requests) == 1


def test_tree_raises_on_a_non_string_entry_rather_than_returning_a_short_tree(
    router, settings
) -> None:
    router.add(r"/files", httpx.Response(200, json={
        "values": [{"path": "a.py"}], "isLastPage": True,
    }))
    with pytest.raises(SCMAPIError, match="non-string tree entry"):
        _client(router, settings).tree(REF, SHA)


# ── branch_head ────────────────────────────────────────────────────────────

def test_branch_head_converts_epoch_millis_to_iso_8601(router, settings) -> None:
    router.add(r"/commits", httpx.Response(200, json={"values": [{
        "id": SHA, "message": "fix the thing",
        "author": {"name": "J Doe", "emailAddress": "j@acme.jp"},
        "authorTimestamp": 1700000000000,
    }]}))
    assert _client(router, settings).branch_head(REF, "develop") == CommitInfo(
        sha=SHA, message="fix the thing", author="J Doe",
        date="2023-11-14T22:13:20+00:00",
    )
    assert "until=refs%2Fheads%2Fdevelop" in router.urls[0]


def test_branch_head_without_a_timestamp_has_an_empty_date_not_a_guess(
    router, settings
) -> None:
    router.add(r"/commits", httpx.Response(200, json={"values": [{"id": SHA}]}))
    assert _client(router, settings).branch_head(REF, "main").date == ""


def test_branch_head_on_an_empty_values_list_is_a_404(router, settings) -> None:
    router.add(r"/commits", httpx.Response(200, json={"values": []}))
    with pytest.raises(SCMAPIError) as info:
        _client(router, settings).branch_head(REF, "nope")
    assert info.value.http_status == 404


# ── compare ────────────────────────────────────────────────────────────────

def test_compare_maps_change_types_and_splits_a_move(router, settings) -> None:
    router.add(r"/compare/changes", httpx.Response(200, json={"isLastPage": True, "values": [
        {"path": {"toString": "src/new.py"}, "type": "ADD"},
        {"path": {"toString": "src/edit.py"}, "type": "MODIFY"},
        {"path": {"toString": "src/gone.py"}, "type": "DELETE"},
        {"path": {"toString": "src/to.py"}, "srcPath": {"toString": "src/from.py"},
         "type": "MOVE"},
    ]}))
    result = _client(router, settings).compare(REF, BASE, SHA)
    assert result.changed == ["src/new.py", "src/edit.py", "src/to.py"]
    assert result.deleted == ["src/gone.py", "src/from.py"]
    assert [f.status for f in result.files] == ["added", "modified", "removed", "renamed"]
    # Reversed on purpose: Data Center answers from=base&to=head with an empty page,
    # so the wrong order reads as "nothing changed" rather than failing.
    assert router.urls[0] == f"{REPO}/compare/changes?from={SHA}&to={BASE}&limit=1000"


def test_compare_reports_no_line_counts_or_patch(router, settings) -> None:
    router.add(r"/compare/changes", httpx.Response(200, json={"isLastPage": True, "values": [
        {"path": {"toString": "a.py"}, "type": "MODIFY"},
    ]}))
    only = _client(router, settings).compare(REF, BASE, SHA).files[0]
    assert (only.additions, only.deletions, only.patch) == (0, 0, None)


def test_compare_treats_an_unknown_type_as_a_modification(router, settings) -> None:
    router.add(r"/compare/changes", httpx.Response(200, json={"isLastPage": True, "values": [
        {"path": {"toString": "a.py"}, "type": "SOMETHING_NEW"},
    ]}))
    result = _client(router, settings).compare(REF, BASE, SHA)
    assert result.changed == ["a.py"] and result.files[0].status == "modified"


def test_compare_raises_on_a_change_with_no_path(router, settings) -> None:
    router.add(r"/compare/changes", httpx.Response(200, json={"isLastPage": True, "values": [
        {"type": "MODIFY"},
    ]}))
    with pytest.raises(SCMAPIError, match="no path.toString"):
        _client(router, settings).compare(REF, BASE, SHA)


# ── pull requests ──────────────────────────────────────────────────────────

def test_pull_request_reads_full_shas_without_a_second_call(router, settings) -> None:
    router.add(r"/pull-requests/42$", httpx.Response(200, json={
        "id": 42, "title": "Add zaike kit", "state": "MERGED",
        "fromRef": {"displayId": "feature/kit", "latestCommit": SHA},
        "toRef": {"displayId": "develop", "latestCommit": BASE},
        "links": {"self": [{"href": f"{REPO}/pull-requests/42"}]},
    }))
    pr = _client(router, settings).pull_request(REF, 42)
    assert (pr.number, pr.state, pr.base_branch, pr.head_branch) == (
        42, "merged", "develop", "feature/kit",
    )
    assert (pr.base_sha, pr.head_sha) == (BASE, SHA)
    assert len(router.requests) == 1


def test_post_pr_comment_uses_the_data_center_text_shape(router, settings) -> None:
    router.add(r"/comments", httpx.Response(201, json={"id": 7}))
    assert _client(router, settings).post_pr_comment(REF, 42, "hello").id == "7"
    assert router.requests[0].read() == b'{"text":"hello"}'


# ── file content and clone URL ─────────────────────────────────────────────

def test_file_content_uses_raw_with_an_at_parameter(router, settings) -> None:
    router.add(r"/raw/", httpx.Response(200, text="print('hi')\n"))
    assert _client(router, settings).file_content(REF, "src/a b.py", SHA) == "print('hi')\n"
    assert router.urls[0] == f"{REPO}/raw/src/a%20b.py?at={SHA}"


def test_clone_url_carries_the_context_path_and_percent_encoded_credentials(
    router, settings
) -> None:
    token = base64.b64encode(b"jdoe:p@ss/w:rd").decode()
    client = _client(router, settings, token=token)
    assert client.clone_url(REF) == (
        "https://jdoe:p%40ss%2Fw%3Ard@stash.payroll.co.jp/stash"
        "/scm/p3/zaike-non-unity-kit-create.git"
    )


def test_personal_project_key_keeps_its_tilde(router, settings) -> None:
    ref = RepoRef("bitbucket_server", "~jdoe", "kit", host="stash.payroll.co.jp")
    router.add(r"/files", httpx.Response(200, json={"values": [], "isLastPage": True}))
    _client(router, settings).tree(ref, SHA)
    assert "/projects/~jdoe/repos/kit/files" in router.urls[0]
