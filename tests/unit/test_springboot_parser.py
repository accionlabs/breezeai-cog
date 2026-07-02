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
    # Routes are statements — only emitted with --capture-statements (spec A4).
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
