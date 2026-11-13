"""Apache HttpClient requests (``new HttpGet(…)``) are ``api_call`` records."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers import additive
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java.parser import JavaParser


def _calls(tmp_path: Path, src: str, rel: str = "src/main/java/com/acme/Client.java"):
    additive.discover_detectors()
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src)
    ctx = ParseContext(path=rel, abs_path=path, source=src.encode(), repo_root=tmp_path,
                       capture_statements=True)
    record = JavaParser().parse_file(ctx)
    return [(s.method, s.endpoint, s.startLine) for s in record.statements
            if s.semanticType == "api_call"]


_CLIENT = """package com.acme;
import org.apache.http.client.methods.HttpGet;
import org.apache.http.client.methods.HttpPost;
import org.apache.http.impl.client.CloseableHttpClient;
class Client {
    CloseableHttpClient client;
    void send(String uri, boolean read) throws Exception {
        HttpRequestBase req;
        if (read) {
            req = new HttpGet(uri);
        } else {
            req = new HttpPost("/api/v1/orders");
        }
        client.execute(req);
        client.execute(new HttpGet("/api/v1/orders/" + uri));
    }
}
"""


def test_request_class_gives_the_verb(tmp_path: Path) -> None:
    assert _calls(tmp_path, _CLIENT) == [
        ("GET", None, 10), ("POST", "/api/v1/orders", 12), ("GET", "/api/v1/orders/{uri}", 15)]


def test_httpclient5_and_wildcard_and_qualified(tmp_path: Path) -> None:
    src = """package com.acme;
import org.apache.hc.client5.http.classic.methods.*;
class Client {
    void send(String u) {
        Object a = new HttpPut(u);
        Object b = new org.apache.http.client.methods.HttpDelete(u);
    }
}
"""
    assert _calls(tmp_path, src) == [("PUT", None, 5), ("DELETE", None, 6)]


def test_own_class_named_like_a_request_is_not_one(tmp_path: Path) -> None:
    src = """package com.acme;
import org.apache.http.client.HttpClient;
class Client {
    void send(String u) { Object a = new HttpGet(u); }
}
class HttpGet { HttpGet(String u) {} }
"""
    assert _calls(tmp_path, src) == []


def test_mock_files_are_skipped(tmp_path: Path) -> None:
    assert _calls(tmp_path, _CLIENT, "src/test/java/com/acme/mocks/Client.java") == []
