"""Spring Boot framework parser: route detection across v2 (javax) and v3 (jakarta)."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java_springboot.parser import SpringBootParser
from breezeai_cog.parsers.java_springboot.routes import spring_version
from breezeai_cog.schemas import FileRecord

SRC_V3 = b'''package com.acme.orders;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    @GetMapping("/{id}")
    public Order get(@PathVariable Long id) { return null; }

    @PostMapping
    public Order create(@RequestBody @Valid OrderDto dto) { return null; }

    @RequestMapping(value="/legacy", method=RequestMethod.PUT)
    public void legacy() {}
}
'''

SRC_V2 = b'''package com.acme.web;
import javax.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;

@Controller
@RequestMapping("/v2")
public class PingController {
    @GetMapping("/ping")
    public String ping() { return "ok"; }
}
'''


def _parse(tmp_path, src: bytes, name: str, *, capture: bool = True) -> FileRecord:
    p = tmp_path / name
    p.write_text(src.decode())
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return SpringBootParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with statement capture (spec A4).
    rec = _parse(tmp_path, SRC_V3, "OrderController.java", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_v3_routes(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_V3, "OrderController.java")
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/api/orders/{id}") in routes
    assert ("POST", "/api/orders") in routes
    assert ("PUT", "/api/orders/legacy") in routes
    assert all(r.framework == "spring" for r in routes.values())
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes.values())  # parented to handler methods
    assert rec.framework == "spring"
    assert spring_version(rec) == 3  # jakarta.* -> Spring Boot 3


def test_v2_routes(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_V2, "PingController.java")
    routes = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert routes == {("GET", "/v2/ping")}
    assert spring_version(rec) == 2  # javax.* -> Spring Boot 2


def test_route_attributes(tmp_path) -> None:
    # spec C5 — @RequestBody type → requestDTO, return type → responseDTO, isRegex False.
    rec = _parse(tmp_path, SRC_V3, "OrderController.java")
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    assert routes["create"].requestDTO == "OrderDto"
    assert routes["create"].responseDTO == "Order"
    assert routes["create"].isRegex is False
    assert routes["get"].requestDTO is None  # only @PathVariable, no body


def test_non_controller_has_no_routes(tmp_path) -> None:
    src = b"package x;\npublic class Plain { public int add(int a){ return a; } }\n"
    rec = _parse(tmp_path, src, "Plain.java")
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_V3, "OrderController.java")
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_claims_selects_spring() -> None:
    registry.clear()
    from breezeai_cog.parsers.java.parser import JavaParser

    registry.register(JavaParser())
    registry.register(SpringBootParser())
    spring = b"import org.springframework.web.bind.annotation.RestController;"
    assert registry.select("X.java", spring).name == "java-springboot"
    assert registry.select("X.java", b"package x;").name == "java"  # plain Java -> base
    registry.clear()


_FN_SRC = b'''package com.example;
import org.springframework.web.servlet.function.RouterFunction;
import org.springframework.web.servlet.function.ServerResponse;
import static org.springframework.web.servlet.function.RequestPredicates.GET;
import static org.springframework.web.servlet.function.RequestPredicates.POST;
import static org.springframework.web.servlet.function.RequestPredicates.contentType;
import static org.springframework.web.servlet.function.RequestPredicates.accept;
import static org.springframework.web.servlet.function.RouterFunctions.route;

public class RouterConfig {
    public RouterFunction<ServerResponse> productRoutes(ProductHandler h) {
        return route(GET("/api/products"), h::list)
                .andRoute(POST("/api/products").and(contentType(JSON)), h::create)
                .andRoute(GET("/api/products/{id}"), h::getById);
    }
    public RouterFunction<ServerResponse> catalogRoutes(ProductHandler h) {
        return route()
                .nest(accept(JSON), b -> b
                        .GET("/api/v2/catalog", h::list)
                        .POST("/api/v2/catalog", h::create))
                .build();
    }
}
'''


def test_functional_router_routes(tmp_path) -> None:
    # #2: WebMvc.fn functional routing — static route()/andRoute() + nested builder DSL.
    rec = _parse(tmp_path, _FN_SRC, "RouterConfig.java")
    got = {(s.method, s.endpoint, s.handler) for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/api/products", "list") in got
    assert ("POST", "/api/products", "create") in got          # composed .and(contentType)
    assert ("GET", "/api/products/{id}", "getById") in got
    assert ("GET", "/api/v2/catalog", "list") in got           # nested builder form
    assert ("POST", "/api/v2/catalog", "create") in got
    assert all(s.framework == "spring" for s in rec.statements if s.semanticType == "route")


_FN_HANDLERS = b"""package demo;
import org.springframework.context.annotation.Bean;
import org.springframework.web.servlet.function.RouterFunction;
import static org.springframework.web.servlet.function.RouterFunctions.route;
import static org.springframework.web.servlet.function.RequestPredicates.*;

public class Routes {
    @Bean
    public RouterFunction<?> r(Handler h) {
        return route(GET("/a"), this::own)
                .andRoute(GET("/b"), h::other)
                .andRoute(GET("/c"), req -> ok().build());
    }

    ServerResponse own(ServerRequest req) { return null; }
}
"""


def test_functional_handler_line_is_the_declaration_or_the_lambda(tmp_path) -> None:
    rec = _parse(tmp_path, _FN_HANDLERS, "Routes.java")
    got = {s.endpoint: (s.handler, s.handlerLine) for s in rec.statements if s.semanticType == "route"}
    assert got == {
        "/a": ("own", 15),     # this::own — declared in this class
        "/b": ("other", None),  # h::other — a method of another class: no line
        "/c": (None, 12),      # lambda: no name, its line
    }


def test_functional_routing_gated(tmp_path) -> None:
    # A Spring file without RouterFunction must not trigger the functional walk.
    src = b"package x; import org.springframework.stereotype.Service;\n@Service class S { void GET(String p){} }"
    rec = _parse(tmp_path, src, "S.java")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


_ARRAY_SRC = b'''package com.example;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.GetMapping;

@RestController
@RequestMapping("/orders")
public class ReportController {
    @GetMapping({"/report.*", "/report/{fmt}"})
    public String report() { return "r"; }

    @GetMapping("/x/{id:[0-9]{1,3}}")
    public String ranged() { return "x"; }
}
'''


def test_array_path_mapping_splits(tmp_path) -> None:
    # #4: @GetMapping({"/a", "/b"}) -> one route per path; a single regex path with a
    # comma ({1,3} quantifier) is NOT split.
    rec = _parse(tmp_path, _ARRAY_SRC, "ReportController.java")
    eps = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/orders/report.*") in eps
    assert ("GET", "/orders/report/{fmt}") in eps
    assert ("GET", "/orders/x/{id:[0-9]{1,3}}") in eps   # single path, comma preserved
    assert not any('{"' in ep or ", " in ep for _, ep in eps)  # no raw array literal leaked


# ── Context path (server.servlet.context-path) and functional nest prefixes ──────────────────
_CTRL = (
    "package a;\nimport org.springframework.web.bind.annotation.*;\n"
    "@RestController\npublic class C {\n  @GetMapping(\"/x\")\n  public String x() { return \"\"; }\n}\n"
)


def _endpoints(stmts) -> dict[str, str | None]:
    return {s.path: s.endpoint for s in stmts if s.semanticType == "route"}


def test_context_path_prefixes_routes_of_its_module(analyze_files) -> None:
    st = analyze_files({
        # module "app": properties in resources and the same value in resources/config
        "app/src/main/java/a/C.java": _CTRL,
        "app/src/main/resources/application.properties": "server.port=8080\nserver.servlet.context-path=/shop\n",
        "app/src/main/resources/config/application.properties": "server.servlet.context-path = /shop\n",
        "app/src/main/resources/application-dev.properties": "server.servlet.context-path=/dev\n",
        "app/src/test/resources/application.properties": "server.servlet.context-path=/test\n",
        # module "yml": nested YAML; a profile document is ignored
        "yml/src/main/java/a/C.java": _CTRL,
        "yml/src/main/resources/application.yml": (
            "server:\n  servlet:\n    context-path: /api\n---\n"
            "spring:\n  config:\n    activate:\n      on-profile: dev\nserver:\n  servlet:\n    context-path: /x\n"
        ),
        # module "legacy": Spring Boot 1.x key
        "legacy/src/main/java/a/C.java": _CTRL,
        "legacy/src/main/resources/application.properties": "server.context-path=/old\n",
        # module "plain": no context path → unchanged
        "plain/src/main/java/a/C.java": _CTRL,
    })
    assert _endpoints(st) == {
        "app/src/main/java/a/C.java": "/shop/x",
        "yml/src/main/java/a/C.java": "/api/x",
        "legacy/src/main/java/a/C.java": "/old/x",
        "plain/src/main/java/a/C.java": "/x",
    }


def test_unresolvable_context_path_leaves_endpoint_empty(analyze_files) -> None:
    st = analyze_files({
        # base files disagree
        "a/src/main/java/a/C.java": _CTRL,
        "a/src/main/resources/application.properties": "server.servlet.context-path=/one\n",
        "a/src/main/resources/config/application.yml": "server:\n  servlet:\n    context-path: /two\n",
        # a placeholder cog does not resolve
        "b/src/main/java/a/C.java": _CTRL,
        "b/src/main/resources/application.properties": "server.servlet.context-path=${CTX:/b}\n",
    })
    routes = [s for s in st if s.semanticType == "route"]
    assert {s.path: s.endpoint for s in routes} == {
        "a/src/main/java/a/C.java": None, "b/src/main/java/a/C.java": None,
    }
    assert all(s.handler == "x" and s.method == "GET" for s in routes)  # the rest is kept


_FN_NEST = b"""package demo;
import org.springframework.context.annotation.Bean;
import org.springframework.web.servlet.function.RouterFunction;
import static org.springframework.web.servlet.function.RouterFunctions.*;
import static org.springframework.web.servlet.function.RequestPredicates.*;

public class Routes {
    @Bean
    public RouterFunction<?> r(Handler h) {
        return route()
            .nest(path("/v2").and(accept(JSON)), b -> b
                .GET("/catalog", h::list)
                .nest(path("/admin"), a -> a.DELETE("/items/{id}", h::delete)))
            .nest(accept(JSON), b -> b.GET("/plain", h::plain))
            .path("/v3", b -> b.POST("/orders", h::create))
            .nest(path(PREFIX), b -> b.GET("/dyn", h::dyn))
            .build();
    }

    @Bean
    public RouterFunction<?> s(Handler h) {
        return nest(path("/static"), route(GET("/s"), h::s));
    }
}
"""


def test_functional_nest_prefixes_compose(tmp_path) -> None:
    rec = _parse(tmp_path, _FN_NEST, "Routes.java")
    got = {s.handler: s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert got == {
        "list": "/v2/catalog",
        "delete": "/v2/admin/items/{id}",   # nested nests compose
        "plain": "/plain",                  # accept(...) adds nothing
        "create": "/v3/orders",             # builder .path(prefix, ...)
        "dyn": None,                        # path(PREFIX): unresolved prefix → empty
        "s": "/static/s",                   # static RouterFunctions.nest
    }
