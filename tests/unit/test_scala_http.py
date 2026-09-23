"""Scala outbound HTTP detection tests."""

from __future__ import annotations

from pathlib import Path

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
