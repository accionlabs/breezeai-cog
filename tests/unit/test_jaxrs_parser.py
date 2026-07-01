"""JAX-RS framework parser: route detection across javax (≤8) and jakarta (≥9)."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java_jaxrs.parser import JaxRsParser
from breezeai_cog.parsers.java_jaxrs.routes import jaxrs_version
from breezeai_cog.schemas import FileRecord

SRC_JAKARTA = b'''package com.acme.orders;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.PathParam;
import jakarta.ws.rs.Produces;

@Path("/orders")
public class OrderResource {
    @GET
    @Path("/{id}")
    @Produces("application/json")
    public Order get(@PathParam("id") Long id) { return null; }

    @POST
    public Order create(OrderDto dto) { return null; }
}
'''

SRC_JAVAX = b'''package com.acme.web;
import javax.ws.rs.GET;
import javax.ws.rs.Path;

@Path("/v1")
public class PingResource {
    @GET
    @Path("/ping")
    public String ping() { return "ok"; }
}
'''


def _parse(tmp_path, src: bytes, name: str, *, capture: bool = True) -> FileRecord:
    p = tmp_path / name
    p.write_text(src.decode())
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return JaxRsParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with --capture-statements (spec A4).
    rec = _parse(tmp_path, SRC_JAKARTA, "OrderResource.java", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_jakarta_routes(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_JAKARTA, "OrderResource.java")
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/orders/{id}") in routes
    assert ("POST", "/orders") in routes
    assert all(r.framework == "jaxrs" for r in routes.values())
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes.values())  # parented to handler methods
    assert rec.framework == "jaxrs"
    assert jaxrs_version(rec) == 9  # jakarta.ws.rs → Jakarta RESTful WS


def test_javax_routes(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_JAVAX, "PingResource.java")
    routes = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert routes == {("GET", "/v1/ping")}
    assert jaxrs_version(rec) == 8  # javax.ws.rs → legacy


def test_route_attributes(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_JAKARTA, "OrderResource.java")
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    assert routes["get"].responseDTO == "Order"
    assert routes["get"].requestDTO is None  # JAX-RS has no @RequestBody
    assert routes["get"].isRegex is False


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_JAKARTA, "OrderResource.java")
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_claims_selects_jaxrs() -> None:
    registry.clear()
    from breezeai_cog.parsers.java.parser import JavaParser

    registry.register(JavaParser())
    registry.register(JaxRsParser())
    assert registry.select("X.java", b"import jakarta.ws.rs.GET;").name == "java-jaxrs"
    assert registry.select("X.java", b"import javax.ws.rs.Path;").name == "java-jaxrs"
    assert registry.select("X.java", b"package x;").name == "java"  # plain Java -> base
    registry.clear()
