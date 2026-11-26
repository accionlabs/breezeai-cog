"""A Groovy request made through a local assigned a new HTTP client
(``OrderApiClient client = new OrderApiClient(uri); client.put(body)``) is an ``api_call``, as the
same request written inline (``new OrderApiClient(uri).put(body)``) is; its arguments are the body,
so neither carries an endpoint."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.groovy.parser import GroovyParser


def _api(tmp_path: Path, src: str) -> list[tuple[int, str | None, str | None]]:
    p = tmp_path / "R.groovy"
    p.write_text(src)
    ctx = ParseContext(path="R.groovy", abs_path=p, source=src.encode(), repo_root=tmp_path,
                       capture_statements=True)
    record = GroovyParser().parse_file(ctx)
    return [(s.startLine, s.method, s.endpoint) for s in record.statements
            if s.semanticType == "api_call"]


def test_a_local_assigned_a_new_client_is_that_client(tmp_path: Path) -> None:
    assert _api(tmp_path, """import com.acme.client.OrderApiClient
class R {
  static Map put(String path, Map values) {
    String uri = "/data" + path
    OrderApiClient client = new OrderApiClient(uri)
    Map response = client.put(values)
    def c = new OrderApiClient(uri)
    c.delete()
    Map inline = new OrderApiClient(uri).put(values)
    return response
  }
}
""") == [(6, "PUT", None), (8, "DELETE", None), (9, "PUT", None)]


def test_names_that_may_hold_something_else_are_not_followed(tmp_path: Path) -> None:
    assert _api(tmp_path, """import com.acme.client.OrderApiClient
class R {
  OrderApiClient shared = new OrderApiClient("/a")
  void field() { shared.get() }
  void param(OrderApiClient given) { given.get() }
  void map(Map client) { client.get("k") }
  void reassigned(String u) {
    def c = new OrderApiClient(u)
    c = [:]
    c.get("k")
  }
  void twoWays(boolean b, String u) {
    if (b) { def d = new OrderApiClient(u) } else { def d = [:] }
    d.get("k")
  }
  void notAClient() {
    def m = new HashMap()
    m.get("k")
    def s3 = new S3Client()
    s3.get("k")
  }
}
""") == []
