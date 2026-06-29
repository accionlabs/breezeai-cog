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


def _parse(tmp_path, src: bytes, name: str) -> FileRecord:
    p = tmp_path / name
    p.write_text(src.decode())
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path)
    return SpringBootParser().parse_file(ctx)


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


def test_override_registered() -> None:
    registry.clear()
    from breezeai_cog.parsers.java.parser import JavaParser

    registry.register(JavaParser())
    registry.register(SpringBootParser())
    assert isinstance(registry.parser_for("X.java"), SpringBootParser)  # base Java skipped
    registry.clear()
