"""Vert.x parser: event-bus / verticle / timer / service-proxy / route detection,
gated by --capture-statements; selection via claims; schema validity."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java_vertx.parser import VertxParser
from breezeai_cog.schemas import FileRecord

SRC = b'''package com.acme;

import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;

@ProxyGen
interface OrderService {
    void create();
}

public class MainVerticle extends AbstractVerticle {
    public void start() {
        Router router = Router.router(vertx);
        router.get("/health").handler(ctx -> ctx.end("ok"));
        router.post("/orders").handler(this::create);

        EventBus eventBus = vertx.eventBus();
        eventBus.send("orders.create", payload);
        eventBus.publish("orders.events", evt);
        eventBus.consumer("orders.query", msg -> handle(msg));

        vertx.setPeriodic(1000, id -> tick());
        vertx.deployVerticle(new WorkerVerticle());
    }
}
'''

REL = "MainVerticle.java"


def _parse(tmp_path, *, capture: bool = True) -> FileRecord:
    p = tmp_path / REL
    p.write_text(SRC.decode())
    ctx = ParseContext(path=REL, abs_path=p, source=SRC, repo_root=tmp_path,
                       capture_statements=capture)
    return VertxParser().parse_file(ctx)


def test_event_and_route_detection(tmp_path) -> None:
    rec = _parse(tmp_path)
    by_semantic: dict[str, list] = {}
    for s in rec.statements:
        if s.semanticType:
            by_semantic.setdefault(s.semanticType, []).append(s)

    assert {"eventbus_send", "eventbus_publish", "eventbus_consumer",
            "timer", "verticle_deploy", "service_proxy", "route"} <= set(by_semantic)

    assert by_semantic["eventbus_send"][0].endpoint == "orders.create"
    assert by_semantic["eventbus_publish"][0].endpoint == "orders.events"
    assert by_semantic["eventbus_consumer"][0].endpoint == "orders.query"

    routes = {(s.method, s.endpoint) for s in by_semantic["route"]}
    assert ("GET", "/health") in routes and ("POST", "/orders") in routes

    assert all(s.framework == "vertx" for group in by_semantic.values() for s in group)
    assert rec.framework == "vertx"


def test_routes_require_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType] == []
    assert rec.framework is None


def test_claims_selects_vertx() -> None:
    registry.clear()
    from breezeai_cog.parsers.java.parser import JavaParser

    registry.register(JavaParser())
    registry.register(VertxParser())
    assert registry.select("X.java", b"import io.vertx.core.Vertx;").name == "java-vertx"
    assert registry.select("X.java", b"package x;").name == "java"  # plain Java -> base
    registry.clear()


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


_ADDR_SRC = b'''package com.acme;

import io.vertx.core.AbstractVerticle;

public class AddrVerticle extends AbstractVerticle {
    static final String TOPIC = "orders.audit";

    public void start() {
        var bus = vertx.eventBus();
        bus.request("orders.ask", req, reply -> handle(reply));   // modern send-with-reply
        bus.send(TOPIC, auditMsg);                                // constant address
        bus.consumer(TOPIC).handler(m -> process(m));             // constant address consumer
    }
}
'''


def test_eventbus_request_and_constant_addresses(tmp_path) -> None:
    # Regression: eventBus.request(...) → eventbus_send, and constant/variable addresses
    # (not just string literals) are captured, with the symbol name as endpoint.
    p = tmp_path / "AddrVerticle.java"
    p.write_text(_ADDR_SRC.decode())
    ctx = ParseContext(path="AddrVerticle.java", abs_path=p, source=_ADDR_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = VertxParser().parse_file(ctx)
    by_sem: dict[str, list] = {}
    for s in rec.statements:
        if s.semanticType:
            by_sem.setdefault(s.semanticType, []).append(s)

    sends = {s.endpoint for s in by_sem.get("eventbus_send", [])}
    consumers = {s.endpoint for s in by_sem.get("eventbus_consumer", [])}
    assert "orders.ask" in sends   # request(...) mapped to eventbus_send
    assert "TOPIC" in sends        # constant-address send captured (endpoint = symbol)
    assert "TOPIC" in consumers    # constant-address consumer captured
    assert all(s.framework == "vertx" for s in by_sem.get("eventbus_send", []))
