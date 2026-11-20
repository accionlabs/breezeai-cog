"""A Java HTTP call whose verb is an ``HttpMethod.X`` argument (Vert.x ``request`` /
``requestAbs``, Spring ``exchange`` / ``execute``, ``WebClient.method``) takes that verb, and its
URL from the argument that holds it (java/statements.py)."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers import additive
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java.parser import JavaParser

SRC = """package com.acme;
import io.vertx.core.http.HttpClient;
import io.vertx.core.http.HttpMethod;
import io.vertx.ext.web.client.WebClient;
import org.springframework.web.client.RestTemplate;
public class Client {
  HttpClient httpClient; WebClient webClient; RestTemplate restTemplate; Object other;
  void go(String id, String uri) {
    httpClient.request(HttpMethod.PUT, 8080, "localhost", "/orders/" + id);
    httpClient.request(HttpMethod.GET, "/x");
    httpClient.request(HttpMethod.DELETE, "api.example.com", "/y");
    httpClient.request(HttpMethod.POST, uri);
    webClient.requestAbs(HttpMethod.GET, "https://api.example.com/z");
    restTemplate.exchange("/orders/" + id, HttpMethod.PATCH, null, String.class);
    httpClient.request(io.vertx.core.http.HttpMethod.HEAD, "/h");
    other.request(HttpMethod.PUT, "/not-a-client");
    httpClient.request(HttpMethod.valueOf(id), "/dynamic");
  }
}
"""


def _calls(tmp_path: Path) -> list[tuple[int, str | None, str | None]]:
    additive.discover_detectors()
    rel = "src/main/java/com/acme/Client.java"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SRC)
    ctx = ParseContext(path=rel, abs_path=path, source=SRC.encode(), repo_root=tmp_path,
                       capture_statements=True)
    return [(s.startLine, s.method, s.endpoint) for s in JavaParser().parse_file(ctx).statements
            if s.semanticType == "api_call"]


def test_the_verb_argument_is_the_method_and_the_url_is_found(tmp_path: Path) -> None:
    calls = _calls(tmp_path)
    assert calls[:7] == [
        (9, "PUT", "/orders/{id}"),  # the host string is not the path
        (10, "GET", "/x"),
        (11, "DELETE", "/y"),
        (12, "POST", None),  # the URI is a variable
        (13, "GET", "https://api.example.com/z"),
        (14, "PATCH", "/orders/{id}"),  # RestTemplate: the URL comes first
        (15, "HEAD", "/h"),
    ]


def test_no_client_or_no_written_verb_gives_no_verb(tmp_path: Path) -> None:
    calls = _calls(tmp_path)
    assert all(line != 16 for line, _, _ in calls)  # not an HTTP client
    # `HttpMethod.valueOf(id)`: the verb is read at runtime — never guessed
    assert [(m, e) for line, m, e in calls if line == 17] == [(None, None)]
