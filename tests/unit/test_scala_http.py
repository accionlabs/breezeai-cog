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
    calls = {s.method: s for s in record.statements if s.semanticType == "api_call"}
    assert calls["GET"]
    assert calls["POST"]
    assert calls["REQUEST"]


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


def test_an_http4s_client_is_an_http_client() -> None:
    assert _api_calls(HTTP4S, "client: Client[IO]", 'client.get(uri"/users")') == [
        ("GET", "/users")
    ]


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
        ("Http(u).postData(b).asString", [("POST", None)]),
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
