"""A Vert.x route or consumer whose handler is an anonymous class (``new Handler<…>() { … }``)
names that class's ``handle``: it is a Function of its own (``Outer$1#handle``), the code that
runs. Without exactly one ``handle`` in the body, or for a lambda, the record keeps only the
line where the inline handler starts — docs/architecture/entry-points.md, anonymous handlers."""

from __future__ import annotations

SRC = "src/main/java/com/acme/"
V2 = """import org.vertx.java.core.Handler;
import org.vertx.java.core.eventbus.Message;
import org.vertx.java.core.json.JsonObject;
import org.vertx.java.platform.Verticle;
import java.util.*;
"""
V3 = """import io.vertx.core.AbstractVerticle;
import io.vertx.core.Handler;
import io.vertx.core.eventbus.Message;
import io.vertx.ext.web.Router;
import io.vertx.ext.web.RoutingContext;
"""


def _records(analyze_files, body: str, imports: str = V2, name: str = "Svc"):
    files = {f"{SRC}{name}.java": f"package com.acme;\n{imports}{body}\n"}
    out = analyze_files(files)
    return [s for s in out if s.semanticType in ("eventbus_consumer", "route")]


def _one(records):
    assert len(records) == 1, records
    s = records[0]
    return s.semanticType, s.endpoint, s.handler, s.handlerLine, s.handlerPath


_CONSUMER = """public class Svc extends Verticle {
  public void start() {
    vertx.eventBus().registerHandler("orders", new Handler<Message<JsonObject>>() {
      @Override
      public void handle(Message<JsonObject> m) { save(m); }
    });
  }
  void save(Message<JsonObject> m) { }
}"""


def test_a_consumer_names_the_anonymous_class_handle(analyze_files) -> None:
    # line 10: the `@Override` of `handle` — where the `Svc$1#handle` Function starts
    assert _one(_records(analyze_files, _CONSUMER)) == ("eventbus_consumer", "orders", "handle", 10, None)


def test_the_handler_line_is_the_anonymous_handle_function(tmp_path) -> None:
    from breezeai_cog.parsers.base import ParseContext
    from breezeai_cog.parsers.java.parser import JavaParser

    src = f"package com.acme;\n{V2}{_CONSUMER}\n"
    p = tmp_path / "Svc.java"
    p.write_text(src)
    record = JavaParser().parse_file(ParseContext(path="Svc.java", abs_path=p, source=src.encode(),
                                                  repo_root=tmp_path))
    fn = next(f for f in record.functions if f.parentId.endswith("#Svc$1"))
    assert (fn.name, fn.startLine) == ("handle", 10)


def test_a_vertx3_consumer_and_route(analyze_files) -> None:
    body = """public class Svc extends AbstractVerticle {
  public void start() {
    vertx.eventBus().consumer("orders", new Handler<Message<String>>() {
      public void handle(Message<String> m) { }
    });
    Router router = Router.router(vertx);
    router.get("/orders").handler(new Handler<RoutingContext>() {
      public void handle(RoutingContext ctx) { }
    });
  }
}"""
    found = sorted((s.semanticType, s.endpoint, s.handler, s.handlerLine)
                   for s in _records(analyze_files, body, V3))
    assert found == [("eventbus_consumer", "orders", "handle", 10),
                     ("route", "/orders", "handle", 14)]


def test_two_handle_overloads_name_nothing(analyze_files) -> None:
    body = """public class Svc extends Verticle {
  public void start() {
    vertx.eventBus().registerHandler("orders", new Handler<Message<JsonObject>>() {
      public void handle(Message<JsonObject> m) { handle(m, 1); }
      void handle(Message<JsonObject> m, int n) { }
    });
  }
}"""
    # which overload Vert.x calls is the interface's business: only the inline line is kept
    assert _one(_records(analyze_files, body)) == ("eventbus_consumer", "orders", None, 9, None)


def test_an_anonymous_class_without_handle_names_nothing(analyze_files) -> None:
    body = """public class Svc extends Verticle {
  public void start() {
    vertx.eventBus().registerHandler("orders", new OrderHandler() {
      protected void onOrder(Message<JsonObject> m) { }
    });
  }
}"""
    assert _one(_records(analyze_files, body)) == ("eventbus_consumer", "orders", None, 9, None)


def test_a_lambda_keeps_only_its_line(analyze_files) -> None:
    body = """public class Svc extends AbstractVerticle {
  public void start() {
    vertx.eventBus().consumer("orders", m -> { });
  }
}"""
    assert _one(_records(analyze_files, body, V3)) == ("eventbus_consumer", "orders", None, 9, None)


def test_a_registration_table_value_names_the_anonymous_handle(analyze_files) -> None:
    body = """public class Svc extends Verticle {
  static Map<String, Handler<Message<JsonObject>>> handlers = new HashMap<>();
  static {
    handlers.put("orders", new Handler<Message<JsonObject>>() {
      public void handle(Message<JsonObject> m) { }
    });
  }
  public void start() {
    for (String name : handlers.keySet()) {
      vertx.eventBus().registerHandler(name, handlers.get(name));
    }
  }
}"""
    assert _one(_records(analyze_files, body)) == ("eventbus_consumer", "orders", "handle", 11, None)
