"""Scala outbound HTTP detection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.scala.parser import ScalaParser
from breezeai_cog.schemas import FileRecord


HTTP_SRC = b'''package example

import play.api.libs.ws.WSClient
import org.http4s.client.Client
import sttp.client3.SttpBackend

class Calls(
  ws: WSClient,
  client: Client[IO],
  backend: SttpBackend[IO, Any],
  http: HttpExt,
) {
  def run(uri: String): Unit = {
    ws.url("/api/users").get()
    ws.url("ftp://not-http.example/users").get()
    ws.url(uri).post(body)
    client.get(uri"/users").flatMap(_.as[String])
    client.expect[String](uri"/orders/$id")
    basicRequest.get(uri"/sttp").send(backend)
    http.singleRequest(HttpRequest(uri = "https://api.example/akka"))
    Http("https://api.example/items").asString
    cache.get("https://not-http.example/value")
  }
}
'''


def _parse(source: bytes = HTTP_SRC) -> FileRecord:
    path = Path("Calls.scala")
    ctx = ParseContext(
        path="Calls.scala",
        abs_path=path,
        source=source,
        repo_root=Path.cwd(),
        capture_statements=True,
    )
    return ScalaParser().parse_file(ctx)


def test_typed_scala_http_clients_and_builder_chains() -> None:
    record = _parse()
    calls = [s for s in record.statements if s.semanticType == "api_call"]

    got = {(s.method, s.endpoint) for s in calls}
    assert ("GET", "/api/users") in got
    assert ("POST", None) in got
    assert ("GET", "/users") in got
    assert ("GET", "/orders/{id}") in got
    assert ("GET", "/sttp") in got
    assert ("GET", "https://api.example/akka") in got
    assert ("GET", "https://api.example/items") in got
    assert not any("not-http.example" in (s.endpoint or "") for s in calls)


REST_TEMPLATE_SRC = b'''package example

import org.springframework.web.client.RestTemplate
import java.net.http.HttpClient

class Calls(
  restTemplate: RestTemplate,
  httpClient: HttpClient,
) {
  def run(): Unit = {
    restTemplate.getForObject(uri, classOf[String])
    restTemplate.postForObject("/orders", body, classOf[String])
    httpClient.send(req, handler)
  }
}
'''


def test_resttemplate_and_java_net_http_resolve_without_guessing() -> None:
    record = _parse(REST_TEMPLATE_SRC)
    calls = [(s.method, s.endpoint) for s in record.statements if s.semanticType == "api_call"]
    assert calls == [("GET", None), ("POST", "/orders"), ("REQUEST", None)]


RT = "import org.springframework.web.client.RestTemplate"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('rt.getForEntity("/a", classOf[String])', [("GET", "/a")]),
        ('rt.put("/a", body)', [("PUT", "/a")]),
        # the verb argument is read when written as HttpMethod.X, else left unresolved
        ('rt.exchange("/a", HttpMethod.POST, e, classOf[String])', [("POST", "/a")]),
        ('rt.execute("/a", org.springframework.http.HttpMethod.DELETE, cb, ex)', [("DELETE", "/a")]),
        ('rt.exchange("/a", verb, e, classOf[String])', [("REQUEST", "/a")]),
    ],
)
def test_resttemplate_verbs(body: str, expected: list[tuple[str, str | None]]) -> None:
    assert _api_calls(RT, "rt: RestTemplate", body) == expected


@pytest.mark.parametrize(
    ("params", "body"),
    [
        # Play WS's execute takes a verb string — it must not land in endpoint
        ("ws: WSClient", 'ws.url("/x").execute("POST")'),
        ("ws: WSClient", 'ws.url("/x").getForObject("/y")'),
        ("svc: OrderService", 'svc.exchange("/a", HttpMethod.POST)'),
    ],
)
def test_resttemplate_rules_need_a_resttemplate(params: str, body: str) -> None:
    assert _api_calls(RT, params, body) == []


def _api_calls(imports: str, params: str, body: str) -> list[tuple[str | None, str | None]]:
    """(method, endpoint) of every api_call in ``class Calls(<params>)`` running ``body``."""
    source = f'''package example
{imports}
class Calls({params}) {{
  def run(): Unit = {{
    {body}
  }}
}}
'''
    record = _parse(source.encode())
    return [(s.method, s.endpoint) for s in record.statements if s.semanticType == "api_call"]


HTTP4S = "import org.http4s.client.Client"


@pytest.mark.parametrize(
    ("imports", "params", "body"),
    [
        ("", "client: Client", "client.get(k)"),
        ("", "client: Client", "client.delete(id)"),
        ("", "client: com.mycorp.kafka.Client", "client.get(k)"),
        ("trait Client { def get(k: String): String }", "client: Client", "client.get(k)"),
        ("", "client: S3Client", "client.get(k)"),
        ("", "client: DbClient", "client.delete(id)"),
        ("", "client: RedisClient", "client.get(k)"),
    ],
)
def test_a_client_type_outside_http4s_is_not_an_http_client(
    imports: str, params: str, body: str
) -> None:
    assert _api_calls(imports, params, body) == []


@pytest.mark.parametrize(
    ("imports", "params"),
    [
        # the file uses http4s, but this Client is not http4s's
        (HTTP4S, "client: com.mycorp.kafka.Client"),
        ("import org.http4s.Uri", "client: Client"),
        ("import org.http4s.client.{Client => HC}", "client: Client"),
        ("import com.mycorp.kafka.Client", "client: Client"),
        ("", "client: HttpClient"),
        ("import java.net.http.HttpRequest", "client: HttpClient"),
    ],
)
def test_a_client_type_is_resolved_through_its_import(imports: str, params: str) -> None:
    assert _api_calls(imports, params, 'client.get("/a")') == []


@pytest.mark.parametrize(
    ("imports", "params"),
    [
        (HTTP4S, "client: Client[IO]"),
        ("import org.http4s.client.{Client, Middleware}", "client: Client[IO]"),
        ("import org.http4s.client.{Client => HC}", "client: HC[IO]"),
        ("import org.http4s.client.{Client as HC}", "client: HC[IO]"),
        ("import org.http4s.client._", "client: Client[IO]"),
        ("import org.http4s.client.*", "client: Client[IO]"),
        ("", "client: org.http4s.client.Client[IO]"),
    ],
)
def test_an_http4s_client_is_an_http_client(imports: str, params: str) -> None:
    assert _api_calls(imports, params, 'client.get(uri"/users")') == [("GET", "/users")]


def test_an_import_inside_the_class_body_counts() -> None:
    assert _api_calls(
        "", "client: Client[IO]", 'import org.http4s.client.Client\n    client.get(uri"/users")'
    ) == [("GET", "/users")]


@pytest.mark.parametrize(
    ("packages", "expected"),
    [
        # a file sees its own package's members without an import
        ("package org.http4s.client", [("GET", "/users")]),
        ("package org.http4s.client\npackage middleware", [("GET", "/users")]),
        # a single clause doesn't make the parent package visible
        ("package org.http4s.client.middleware", []),
    ],
)
def test_a_client_in_its_own_package_needs_no_import(
    packages: str, expected: list[tuple[str, str | None]]
) -> None:
    source = f'''{packages}
class Calls(client: Client[IO]) {{
  def run(): Unit = {{
    client.get(uri"/users")
  }}
}}
'''
    record = _parse(source.encode())
    calls = [(s.method, s.endpoint) for s in record.statements if s.semanticType == "api_call"]
    assert calls == expected


def test_a_java_net_http_client_is_resolved_through_its_import() -> None:
    assert _api_calls(
        "import java.net.http.{HttpClient, HttpRequest}", "c: HttpClient", "c.send(req, h)"
    ) == [("REQUEST", None)]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # a Request argument carries its own verb — never assume GET
        ("client.expect[String](req)", [("REQUEST", None)]),
        ("client.run(postReq)", [("REQUEST", None)]),
        # a Uri argument is a GET
        ('client.expect[String](uri"https://a.b/c")', [("GET", "https://a.b/c")]),
    ],
)
def test_http4s_verb_is_resolved_never_guessed(
    body: str, expected: list[tuple[str, str | None]]
) -> None:
    assert _api_calls(HTTP4S, "client: Client[IO]", body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Http(u).asString", [("GET", None)]),
        ("Http(u).asString()", [("GET", None)]),
        ("Http(u).postData(b).asString", [("POST", None)]),
        ("Http(u).postForm(Seq()).asString", [("POST", None)]),
        ("Http(u).postMulti(part).asString", [("POST", None)]),
        ('Http(u).put("data").asString', [("PUT", None)]),
        ('Http(u).method("PUT").asString', [("PUT", None)]),
        ("Http(u).method(verb).asString", [("REQUEST", None)]),
        # the last verb builder applied wins
        ('Http(u).postData(b).method("PATCH").asString', [("PATCH", None)]),
        # the URL is Http's argument, never another builder's
        ('Http("https://a.b/c").header("Accept", "json").asString', [("GET", "https://a.b/c")]),
        ('Http("https://a.b/c").param("q", "x").asString()', [("GET", "https://a.b/c")]),
        ('Http(u).header("Accept", "json").asString', [("GET", None)]),
    ],
)
def test_scalaj_verb_comes_from_the_builder_chain(
    body: str, expected: list[tuple[str, str | None]]
) -> None:
    assert _api_calls("", "u: String", body) == expected


def test_as_string_on_a_value_named_http_is_not_a_call() -> None:
    assert _api_calls("", "http: ConfigSection", "http.asString") == []


@pytest.mark.parametrize(
    ("imports", "params", "body", "expected"),
    [
        # building a request is not sending one
        ("", "", 'HttpRequest(method = HttpMethods.POST, uri = "/x")', []),
        (
            "",
            "http: HttpExt",
            'http.singleRequest(HttpRequest(HttpMethods.POST, "https://a/b"))',
            [("POST", "https://a/b")],
        ),
        (
            HTTP4S,
            "c: Client[IO]",
            "c.run(org.http4s.Request[F](org.http4s.Method.GET, uri))",
            [("REQUEST", None)],
        ),
        # a paren-less verb call is still a call
        ("", "ws: WSClient", 'ws.url("/x").get', [("GET", "/x")]),
    ],
)
def test_an_http_method_constant_is_not_a_call(
    imports: str, params: str, body: str, expected: list[tuple[str, str | None]]
) -> None:
    assert _api_calls(imports, params, body) == expected


def test_scala_http_client_ids_do_not_leak_between_files() -> None:
    _parse()
    source = b'''class Plain {
  def run(): Unit = {
    client.get("/not-a-client")
  }
}
'''
    record = _parse(source)
    assert not any(s.semanticType == "api_call" for s in record.statements)
