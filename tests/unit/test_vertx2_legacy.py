"""Legacy Vert.x 2 idioms (parsers/java_vertx/legacy.py): registration tables, address
forwarders and HttpClient calls — docs/entry-point-roles/vertx.md."""

from __future__ import annotations

SRC = "src/main/java/com/acme/"
V2 = """import org.vertx.java.core.Handler;
import org.vertx.java.core.Vertx;
import org.vertx.java.core.eventbus.Message;
import org.vertx.java.core.json.JsonObject;
import org.vertx.java.platform.Verticle;
import org.vertx.java.platform.Container;
"""


def _java(name: str, body: str, imports: str = V2) -> dict[str, str]:
    return {f"{SRC}{name}.java": f"package com.acme;\nimport java.util.*;\n{imports}{body}\n"}


def _handler(name: str, address: str) -> dict[str, str]:
    return _java(name, f"""public class {name} extends BusModBase implements Handler<Message<JsonObject>> {{
  public static final String ADDRESS = {address};
  public void handle(Message<JsonObject> m) {{ }}
}}""")


BASE = {
    **_java("Constants", 'public class Constants { public static final String MODULE = "orders"; }',
            ""),
    **_java("BusModBase", "public class BusModBase {}", ""),
}


def _bus(stmts, *types: str):
    types = types or ("eventbus_consumer", "eventbus_send")
    return sorted(
        ((s.path.rsplit("/", 1)[-1], s.semanticType, s.endpoint, s.handler,
          (s.handlerPath or "").rsplit("/", 1)[-1] or None)
         for s in stmts if s.semanticType in types),
        key=lambda r: tuple(x or "" for x in r))


# ── registration tables ─────────────────────────────────────────────────────────────────────

def _server(loop: str, puts: str = """    handlers.put(OrderFilter.ADDRESS, new OrderFilter());
    handlers.put(OrderWriter.ADDRESS, new OrderWriter());""") -> dict[str, str]:
    return _java("ServiceServer", f"""public class ServiceServer extends Verticle {{
  static Map<String, BusModBase> handlers = new HashMap<>();
  static {{
{puts}
  }}
  public void start() {{
{loop}
  }}
}}""")


ENTRY_LOOP = """    for (Map.Entry<String, BusModBase> e : handlers.entrySet()) {
      String name = e.getKey();
      vertx.eventBus().registerHandler(name, new Handler<Message<JsonObject>>() {
        public void handle(Message<JsonObject> m) { ((Handler) e.getValue()).handle(m); }
      });
    }"""


def _tables(analyze_files, loop: str, puts: str | None = None):
    files = {**BASE, **_handler("OrderFilter", 'Constants.MODULE + "/OrderFilter"'),
             **_handler("OrderWriter", 'Constants.MODULE + "/OrderWriter"'),
             **_handler("Unregistered", '"orders/Unregistered"'),
             **(_server(loop) if puts is None else _server(loop, puts))}
    return _bus(analyze_files(files))


def test_each_put_into_a_registration_table_is_a_consumer(analyze_files) -> None:
    # The key folds across files (`Constants.MODULE + "/X"`); the handler is the put class's
    # `handle`. The loop's own registerHandler is wiring: no address-less record for it, and a
    # BusModBase subclass that is never put gives nothing.
    assert _tables(analyze_files, ENTRY_LOOP) == [
        ("ServiceServer.java", "eventbus_consumer", "orders/OrderFilter", "handle",
         "OrderFilter.java"),
        ("ServiceServer.java", "eventbus_consumer", "orders/OrderWriter", "handle",
         "OrderWriter.java"),
    ]


def test_registration_table_through_key_set_or_a_direct_get_key(analyze_files) -> None:
    by_keys = """    for (String name : handlers.keySet()) {
      vertx.eventBus().registerHandler(name, handlers.get(name));
    }"""
    direct = """    for (Map.Entry<String, BusModBase> e : this.handlers.entrySet()) {
      vertx.eventBus().registerHandler(e.getKey(), (Handler) e.getValue());
    }"""
    for loop in (by_keys, direct):
        assert [r[2] for r in _tables(analyze_files, loop)] == [
            "orders/OrderFilter", "orders/OrderWriter"]


def test_put_into_a_map_whose_keys_never_reach_register_handler_is_not_a_consumer(
        analyze_files) -> None:
    loop = """    Map<String, String> other = new HashMap<>();
    other.put("not/registered", "x");
    for (Map.Entry<String, BusModBase> e : handlers.entrySet()) {
      System.out.println(e.getKey());
    }"""
    assert _tables(analyze_files, loop) == []


def test_put_value_that_is_not_a_new_instance_has_no_handler(analyze_files) -> None:
    puts = """    BusModBase shared = make();
    handlers.put("orders/shared", shared);
    handlers.put(runtimeKey(), new OrderFilter());"""
    got = _tables(analyze_files, ENTRY_LOOP, puts)
    # A value that is not `new X()` → no handler; a key that does not fold → no address.
    assert got == [
        ("ServiceServer.java", "eventbus_consumer", None, "handle", "OrderFilter.java"),
        ("ServiceServer.java", "eventbus_consumer", "orders/shared", None, None),
    ]


def test_table_filled_in_another_file_keeps_the_loop_record(analyze_files) -> None:
    got = _tables(analyze_files, ENTRY_LOOP, "    // filled by a registry elsewhere")
    assert got == [("ServiceServer.java", "eventbus_consumer", None, None, None)]


# ── address forwarders ──────────────────────────────────────────────────────────────────────

UTIL = _java("EventBusUtil", """public class EventBusUtil {
  public static Object send(Container c, Vertx v, JsonObject msg, String address, long timeout) {
    return v.eventBus().sendWithTimeout(address, msg, timeout, null);
  }
}""")
MESSAGE_HANDLER = _java("MessageHandler", """public class MessageHandler implements Handler<Message<JsonObject>> {
  private final Vertx vertx;
  private final String address;
  public MessageHandler(Vertx vertx, Container c, String address) {
    this.vertx = vertx;
    this.address = address;
  }
  void start() { vertx.eventBus().registerHandler(address, this); }
  public void handle(Message<JsonObject> m) { }
}""")
INFO = _java("InfoHandler", "public class InfoHandler implements Handler<Message<JsonObject>> "
                            "{ public void handle(Message<JsonObject> m) { } }")


def _caller(body: str) -> dict[str, str]:
    return _java("Caller", f"""public class Caller extends Verticle {{
  static final String TARGET = "orders/OrderWriter";
  public void start() {{
{body}
  }}
  private void register(final String address, Handler<Message<JsonObject>> h) {{
    vertx.eventBus().registerHandler(address, h);
  }}
}}""")


def test_address_through_a_helper_or_constructor_is_resolved_at_each_call_site(
        analyze_files) -> None:
    files = {**UTIL, **MESSAGE_HANDLER, **INFO, **_caller("""
    EventBusUtil.send(container, vertx, new JsonObject(), TARGET, 5000L);
    register("orders/info", new InfoHandler());
    new MessageHandler(vertx, container, "orders/view");""")}
    # The helpers' own calls (address = a parameter) are not records.
    assert _bus(analyze_files(files)) == [
        ("Caller.java", "eventbus_consumer", "orders/info", "handle", "InfoHandler.java"),
        ("Caller.java", "eventbus_consumer", "orders/view", "handle", "MessageHandler.java"),
        ("Caller.java", "eventbus_send", "orders/OrderWriter", None, None),
    ]


def test_forwarded_argument_that_is_not_a_constant_leaves_the_address_empty(
        analyze_files) -> None:
    files = {**UTIL, **MESSAGE_HANDLER, **_caller("""
    String where = pick();
    EventBusUtil.send(container, vertx, new JsonObject(), where, 5000L);
    new MessageHandler(vertx, container, where);""")}
    assert _bus(analyze_files(files)) == [
        ("Caller.java", "eventbus_consumer", None, "handle", "MessageHandler.java"),
        ("Caller.java", "eventbus_send", None, None, None),
    ]


def test_helper_that_rewrites_the_address_is_not_a_forwarder(analyze_files) -> None:
    files = {**_java("Prefixed", """public class Prefixed {
  public static void send(Vertx v, String address) {
    v.eventBus().send("orders/" + address, null);
  }
}"""), **_caller('    Prefixed.send(vertx, "x");')}
    # The helper's own send keeps its (unresolved) record; the call site is not an EventBus call.
    assert _bus(analyze_files(files)) == [("Prefixed.java", "eventbus_send", None, None, None)]


def test_same_helper_with_two_shapes_across_files_is_not_followed(analyze_files) -> None:
    other = "src/other/java/com/acme/"
    files = {**UTIL, **_caller(
        '    EventBusUtil.send(container, vertx, new JsonObject(), "orders/x", 5000L);')}
    # The same `EventBusUtil.send/5` elsewhere forwards a different parameter as the address.
    files[f"{other}EventBusUtil.java"] = files[f"{SRC}EventBusUtil.java"].replace(
        "JsonObject msg, String address", "String address, JsonObject msg")
    got = [r for r in _bus(analyze_files(files)) if r[0] == "Caller.java"]
    # Which helper the call reaches is unsure: no address taken from it.
    assert all(r[2] is None for r in got)


# ── HttpClient ──────────────────────────────────────────────────────────────────────────────

def _client(body: str, imports: str = "import org.vertx.java.core.http.HttpClient;\n"):
    return _java("Client", f"""public class Client extends Verticle {{
  static final String ORDERS = "/1.0/data/ORDERS";
  private HttpClient pooled() {{ return vertx.createHttpClient(); }}
  public void start() {{
{body}
  }}
}}""", V2 + imports)


def _api(stmts):
    return [(s.method, s.endpoint, s.framework) for s in stmts if s.semanticType == "api_call"]


def test_vertx2_http_client_calls_are_api_calls(analyze_files) -> None:
    st = analyze_files(_client("""
    HttpClient client = vertx.createHttpClient().setHost("data-api").setPort(8080);
    client.post(ORDERS, response -> { }).end("body");
    client.getNow("/1.0/data/ORDERS/42", response -> { });
    client.request("delete", ORDERS + "/7", response -> { }).end();
    pooled().put("/1.0/data/ITEMS", response -> { }).end();"""))
    assert _api(st) == [
        ("POST", "/1.0/data/ORDERS", "vertx"),
        ("GET", "/1.0/data/ORDERS/42", "vertx"),
        ("DELETE", "/1.0/data/ORDERS/7", "vertx"),
        ("PUT", "/1.0/data/ITEMS", "vertx"),
    ]


def test_http_client_uri_that_is_not_a_constant_is_empty(analyze_files) -> None:
    st = analyze_files(_client("""
    HttpClient client = vertx.createHttpClient();
    String id = next();
    client.getNow("/1.0/data/ORDERS/" + id, response -> { });"""))
    assert _api(st) == [("GET", None, "vertx")]


def test_get_on_something_that_is_not_a_vertx2_http_client_is_not_an_api_call(
        analyze_files) -> None:
    st = analyze_files(_client("""
    Map<String, String> cache = new HashMap<>();
    cache.get("/1.0/data/ORDERS");"""))
    assert _api(st) == []


RELAY_UTIL = _java("EventBusUtil", """public class EventBusUtil {
  public static Object send(Container c, Vertx v, JsonObject msg, String address, Object o) {
    return send(c, v, msg, address, o, 5000L);
  }
  public static Object send(Container c, Vertx v, JsonObject msg, String address, Object o,
                            long timeout) {
    return v.eventBus().sendWithTimeout(address, o, timeout, null);
  }
  public static Object other(Container c, Vertx v, String address) {
    return send(c, v, null, "orders/fixed", address, 1L);
  }
}""")


def test_helper_overload_relaying_to_the_real_helper_is_followed(analyze_files) -> None:
    files = {**RELAY_UTIL, **_caller("""
    EventBusUtil.send(container, vertx, new JsonObject(), TARGET, null);
    EventBusUtil.send(container, vertx, new JsonObject(), "orders/direct", null, 10L);""")}
    # Both overloads resolve at the caller; the relay inside EventBusUtil is plumbing.
    assert _bus(analyze_files(files)) == [
        ("Caller.java", "eventbus_send", "orders/OrderWriter", None, None),
        ("Caller.java", "eventbus_send", "orders/direct", None, None),
        ("EventBusUtil.java", "eventbus_send", "orders/fixed", None, None),
    ]


def test_relay_that_does_not_pass_the_address_on_is_not_a_forwarder(analyze_files) -> None:
    files = {**RELAY_UTIL, **_caller('    EventBusUtil.other(container, vertx, "orders/nope");')}
    got = [r for r in _bus(analyze_files(files)) if r[0] == "Caller.java"]
    # `other` passes its parameter as the message, not the address: the call is no EventBus call.
    assert got == []


# ── forwarders are classes, not simple names ────────────────────────────────────────────────

_FWD = ("package a;\nimport org.vertx.java.core.Vertx;\npublic class Util {\n"
        "  public static void send(Vertx v, String address) { v.eventBus().send(address, null); }\n}\n")
_LOGGER = ("package b;\npublic class Util {\n"
           "  public static void send(Object log, String text) { System.out.println(text); }\n}\n")


def _call(imports: str) -> str:
    return (f"package b;\n{imports}import org.vertx.java.platform.Verticle;\n"
            'public class Caller extends Verticle { public void start() { Util.send(vertx, "x/y"); } }\n')


def _sends(analyze_files, files):
    return [(s.path.rsplit("/", 1)[-1], s.endpoint) for s in analyze_files(files)
            if s.semanticType == "eventbus_send"]


def test_same_named_class_of_another_package_is_not_a_forwarder(analyze_files) -> None:
    files = {"src/main/java/a/Util.java": _FWD, "src/main/java/b/Util.java": _LOGGER,
             "src/main/java/b/Caller.java": _call("")}
    # `Util` in package b is b.Util, a logger: no event-bus send.
    assert _sends(analyze_files, files) == []


def test_forwarder_reached_through_an_import_is_followed(analyze_files) -> None:
    for imports in ("import a.Util;\n", "import a.*;\n"):
        files = {"src/main/java/a/Util.java": _FWD, "src/main/java/b/Caller.java": _call(imports)}
        assert _sends(analyze_files, files) == [("Caller.java", "x/y")]


def test_same_package_class_shadows_a_wildcard_forwarder(analyze_files) -> None:
    files = {"src/main/java/a/Util.java": _FWD, "src/main/java/b/Util.java": _LOGGER,
             "src/main/java/b/Caller.java": _call("import a.*;\n")}
    # Java resolves `Util` to the package's own b.Util before any on-demand import.
    assert _sends(analyze_files, files) == []


def test_vertx3_http_client_is_left_to_the_shared_detection(analyze_files) -> None:
    st = analyze_files({f"{SRC}Client.java": """package com.acme;
import io.vertx.core.AbstractVerticle;
import io.vertx.core.http.HttpClient;
public class Client extends AbstractVerticle {
  public void start() {
    HttpClient client = vertx.createHttpClient();
    client.getNow("/1.0/data/ORDERS/42", response -> { });
  }
}
"""})
    # The Vert.x 2 rule reads only `org.vertx.java.core.http.HttpClient`: no vertx api_call.
    assert [s for s in st if s.semanticType == "api_call" and s.framework == "vertx"] == []


# ── event-bus proxies and Vert.x 3+ ─────────────────────────────────────────────────────────

def _proxy(imports: str) -> dict[str, str]:
    return _java("OrderProxy", """public class OrderProxy {
  private final Vertx vertx;
  private final String address;
  public OrderProxy(Vertx vertx, String address) { this.vertx = vertx; this.address = address; }
  public void listOrders() { vertx.eventBus().send(address, new JsonObject()); }
  public void getOrder() { vertx.eventBus().send(address, new JsonObject()); }
  public static OrderProxy create(Vertx vertx) { return new OrderProxy(vertx, "orders"); }
}""", imports)


V3 = """import io.vertx.core.Vertx;
import io.vertx.core.json.JsonObject;
import io.vertx.core.AbstractVerticle;
"""


def test_send_through_a_constructor_set_address_stays_at_each_send(analyze_files) -> None:
    for imports in (V2, V3):
        files = _proxy(imports)
        lines = next(iter(files.values())).splitlines()
        sends = [i + 1 for i, text in enumerate(lines) if "eventBus().send(" in text]
        # A proxy: one send per method through the address its constructor stored. The
        # construction is not a send, so the sends are not collapsed onto `new OrderProxy(…)`.
        assert sorted((s.semanticType, s.startLine, s.endpoint) for s in analyze_files(files)
                      if s.semanticType == "eventbus_send") == [
            ("eventbus_send", line, None) for line in sends], imports


def test_vertx3_helper_passing_its_address_on_is_not_followed(analyze_files) -> None:
    st = analyze_files(_java("Bus", """public class Bus extends AbstractVerticle {
  void listen(String address) { vertx.eventBus().consumer(address, m -> { }); }
  void ping(String address) { vertx.eventBus().send(address, "x"); }
  public void start() { listen("orders.created"); ping("orders.ping"); }
}""", V3))
    # Forwarders are a Vert.x 2 rule: a 3.x+ helper keeps its own record (in the helper,
    # address unknown); its call sites are no records.
    assert sorted((s.semanticType, s.parentId.rsplit("#", 1)[-1].split("@")[0], s.endpoint) for s in st
                  if s.semanticType in ("eventbus_consumer", "eventbus_send")) == [
        ("eventbus_consumer", "listen", None), ("eventbus_send", "ping", None)]


def test_vertx3_map_and_loop_is_not_a_registration_table(analyze_files) -> None:
    st = analyze_files(_java("Server", """public class Server extends AbstractVerticle {
  static Map<String, Object> handlers = new HashMap<>();
  static { handlers.put("orders/a", new Object()); }
  public void start() {
    for (String k : handlers.keySet()) { vertx.eventBus().consumer(k, m -> { }); }
  }
}""", V3))
    # Registration tables are a Vert.x 2 rule: the loop's consumer stays, the put is no record.
    assert [(s.semanticType, s.endpoint) for s in st if s.semanticType == "eventbus_consumer"] \
        == [("eventbus_consumer", None)]
