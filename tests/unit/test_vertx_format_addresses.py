"""A Vert.x address built with ``String.format`` / ``formatted`` reads as the same concatenation
would: the format folded to its text, each ``%s`` / ``%d`` filled with its argument
(vertx_common.render_address). Interface fields are constants (java/constants.py)."""

from __future__ import annotations

MAIN = "src/main/java/a/Main.java"

PATHS = """package a;
public interface Paths {
  String PRICE = "%s/price/%s";
}
"""


def _records(stmts):
    return {s.startLine: (s.semanticType, s.method, s.endpoint)
            for s in stmts if s.path == MAIN and s.semanticType}


def test_format_addresses(analyze_files) -> None:
    got = _records(analyze_files({MAIN: """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
public class Main extends AbstractVerticle {
  static final String NOTICE = "%s/notice";
  public void start() {
    Router router = Router.router(vertx);
    String prefix = config().getString("prefix");
    router.post(String.format(NOTICE, prefix)).handler(this::h);
    router.get(String.format(Paths.PRICE, prefix, "jp")).handler(this::h);
    router.get(String.format("%s/cache/%d", prefix, 3)).handler(this::h);
    router.get(String.format("/api/%s", "v1")).handler(this::h);
    router.get("%s/y".formatted(prefix)).handler(this::h);
    vertx.eventBus().send(String.format("%s.ready", prefix), null);
    router.get(String.format("%05d/x", prefix)).handler(this::h);
    router.get(String.format("%s/%s/z", prefix)).handler(this::h);
    router.get(String.format("%1$s/w", prefix)).handler(this::h);
    router.get(String.format(unknownFormat, prefix)).handler(this::h);
  }
  void h(Object c) {}
}
""", "src/main/java/a/Paths.java": PATHS}))
    assert got[9] == ("route", "POST", "${prefix}/notice")  # a constant format
    assert got[10] == ("route", "GET", "${prefix}/price/jp")  # an interface constant, another file
    assert got[11] == ("route", "GET", "${prefix}/cache/3")
    assert got[12] == ("route", "GET", "/api/v1")  # every part known: the path itself
    assert got[13] == ("route", "GET", "${prefix}/y")  # `formatted`
    assert got[14] == ("eventbus_send", None, "${prefix}.ready")
    for line in (15, 16, 17, 18):  # padding, too few arguments, a positional index, no format
        assert line not in got


def test_interface_field_is_a_constant(analyze_files) -> None:
    got = _records(analyze_files({MAIN: """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
public class Main extends AbstractVerticle {
  public void start() {
    Router router = Router.router(vertx);
    router.get(Paths.HOME).handler(this::h);
  }
  void h(Object c) {}
}
""", "src/main/java/a/Paths.java": 'package a;\npublic interface Paths { String HOME = "/home"; }\n'}))
    assert got[7] == ("route", "GET", "/home")
