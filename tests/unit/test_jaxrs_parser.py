"""JAX-RS framework parser: route detection across javax (≤8) and jakarta (≥9)."""

from __future__ import annotations

import pytest

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
    # Routes are statements — only emitted with statement capture (spec A4).
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


# ── Prefixes from outside the resource class: @ApplicationPath, sub-resource locators ─────────
_RS = "package a;\nimport javax.ws.rs.*;\n"
_MAIN = "src/main/java/a/"


def _eps(analyze_files, files: dict[str, str]) -> dict[tuple[str, str | None], str | None]:
    """(file, handler) → endpoint of every JAX-RS route."""
    return {(s.path.rsplit("/", 1)[-1], s.handler): s.endpoint
            for s in analyze_files({_MAIN + k: _RS + v for k, v in files.items()})
            if s.semanticType == "route" and s.framework == "jaxrs"}


def test_application_path_and_nested_locators_compose(analyze_files) -> None:
    got = _eps(analyze_files, {
        "App.java": '@ApplicationPath(App.BASE)\npublic class App extends javax.ws.rs.core.Application {\n'
                    '  static final String BASE = "/api";\n}\n',
        "Orders.java": '@Path("/orders")\npublic class Orders {\n'
                       '  @GET public String list() { return ""; }\n'
                       '  @Path("/{id}/items") public Items items() { return new Items(); }\n}\n',
        "Items.java": 'public class Items {\n  @GET public String all() { return ""; }\n'
                      '  @Path("/{n}/notes") public Notes notes() { return new Notes(); }\n}\n',
        "Notes.java": 'public class Notes {\n  @POST public void add(String n) {}\n}\n',
    })
    assert got == {
        ("Orders.java", "list"): "/api/orders",
        ("Items.java", "all"): "/api/orders/{id}/items",
        ("Notes.java", "add"): "/api/orders/{id}/items/{n}/notes",
    }


def test_unresolved_application_or_locator_path_leaves_endpoint_empty(analyze_files) -> None:
    got = _eps(analyze_files, {
        "App.java": "@ApplicationPath(Config.ROOT)\npublic class App extends javax.ws.rs.core.Application {}\n",
        "Orders.java": '@Path("/orders")\npublic class Orders {\n  @GET public String list() { return ""; }\n}\n',
    })
    assert got == {("Orders.java", "list"): None}


def test_sub_resource_reached_twice_keeps_its_own_path(analyze_files) -> None:
    got = _eps(analyze_files, {
        "A.java": '@Path("/a")\npublic class A {\n  @Path("/x") public Shared x() { return null; }\n}\n',
        "B.java": '@Path("/b")\npublic class B {\n  @Path("/y") public Shared y() { return null; }\n}\n',
        "Shared.java": 'public class Shared {\n  @GET public String get() { return ""; }\n}\n',
    })
    assert got == {("Shared.java", "get"): "/"}


def test_locator_on_a_root_resource_class_does_not_move_it(analyze_files) -> None:
    # A returned class with its own @Path is a root resource: it keeps its own path.
    got = _eps(analyze_files, {
        "A.java": '@Path("/a")\npublic class A {\n  @Path("/x") public Root x() { return null; }\n}\n',
        "Root.java": '@Path("/root")\npublic class Root {\n  @GET public String get() { return ""; }\n}\n',
    })
    assert got == {("Root.java", "get"): "/root"}


def test_security_annotations_are_guards(analyze_files) -> None:
    st = analyze_files({"src/main/java/a/Res.java": """package a;
import javax.ws.rs.*;
import javax.annotation.security.*;

@Path("/orders")
@RolesAllowed({"user"})
public class Res {
  @GET
  public String list() { return ""; }

  @DELETE
  @DenyAll
  public void purge() {}

  @POST
  @PermitAll
  public void create(String o) {}
}
"""})
    got = {s.method: (s.guards, s.authRequired) for s in st if s.semanticType == "route"}
    # A method annotation replaces the class one of its family: @DenyAll and @PermitAll on the
    # method set that method's access, whatever the class says.
    assert got == {
        "GET": (['RolesAllowed({"user"})'], True),
        "DELETE": (["DenyAll"], True),
        "POST": (None, None),  # @PermitAll: open to everyone
    }


def test_provider_request_filters_guard_bound_and_unbound_routes(analyze_files) -> None:
    st = analyze_files({_MAIN + k: _RS + v for k, v in {
        "Secured.java": "@NameBinding\npublic @interface Secured {}\n",
        "LogFilter.java": "@Provider\npublic class LogFilter implements ContainerRequestFilter {}\n",
        "AuthFilter.java": "@Provider\n@Secured\npublic class AuthFilter implements ContainerRequestFilter {}\n",
        "Orders.java": (
            '@Path("/orders")\npublic class Orders {\n'
            '  @GET public String list() { return ""; }\n'
            '  @GET @Path("/{id}") @Secured @RolesAllowed("admin") public String one() { return ""; }\n}\n'
        ),
        "Admin.java": '@Path("/admin")\n@Secured\npublic class Admin {\n  @GET public String stats() { return ""; }\n}\n',
    }.items()})
    got = {s.handler: (tuple(s.guards or ()), s.authRequired)
           for s in st if s.semanticType == "route" and s.framework == "jaxrs"}
    assert got == {
        "list": (("LogFilter",), None),                                  # unbound filter only
        # filters in a deterministic order (by file; JAX-RS orders by @Priority, unset here)
        "one": (("AuthFilter", "LogFilter", "RolesAllowed(admin)"), True),  # bound on the method
        "stats": (("AuthFilter", "LogFilter"), None),                    # bound on the class
    }


def test_path_values_resolve_or_leave_the_endpoint_empty(analyze_files) -> None:
    # value =, a constant of the class, a constant of another class, a concatenation: resolved.
    # A constant that is not in the repo: endpoint empty — never the constant's name.
    st = analyze_files({
        "src/main/java/demo/Api.java": (
            "package demo;\npublic class Api {\n  public static final String BASE = \"/api\";\n}\n"
        ),
        "src/main/java/demo/Users.java": (
            "package demo;\nimport javax.ws.rs.*;\n"
            "@Path(value = \"users\")\n"
            "public class Users {\n"
            "  static final String ONE = \"{id}\";\n"
            "  @GET public String list() { return \"\"; }\n"
            "  @GET @Path(ONE) public String one() { return \"\"; }\n"
            "  @GET @Path(Api.BASE + \"/x\") public String x() { return \"\"; }\n"
            "  @GET @Path(Missing.PATH) public String gone() { return \"\"; }\n"
            "}\n"
        ),
    })
    got = {s.handler: s.endpoint for s in st if s.semanticType == "route"}
    assert got == {
        "list": "/users",
        "one": "/users/{id}",
        "x": "/users/api/x",
        "gone": None,
    }


def test_locator_owner_path_is_its_own_not_a_same_named_class(analyze_files) -> None:
    # Two classes named Api in different packages: the locator in a.Api serves Items under
    # a.Api's path, never b.Api's.
    st = analyze_files({
        "src/main/java/a/Api.java": _RS + '@Path("/alpha")\npublic class Api {\n'
                                    '  @Path("items") public Items items() { return null; }\n}\n',
        "src/main/java/a/Items.java": _RS + "public class Items {\n"
                                      '  @GET public String list() { return ""; }\n}\n',
        "src/main/java/b/Api.java": _RS.replace("package a", "package b")
                                    + '@Path("/beta")\npublic class Api {\n'
                                    '  @GET public String ping() { return ""; }\n}\n',
    })
    got = {s.handler: s.endpoint for s in st if s.semanticType == "route"}
    assert got == {"list": "/alpha/items", "ping": "/beta"}


_ORDERS = _RS + '@Path("/orders")\npublic class Orders {\n  @GET public String list() { return ""; }\n}\n'
_WEB_XML = (
    '<web-app xmlns="https://jakarta.ee/xml/ns/jakartaee">\n'
    "  <servlet><servlet-name>jersey</servlet-name>\n"
    "    <servlet-class>org.glassfish.jersey.servlet.ServletContainer</servlet-class></servlet>\n"
    "  <servlet-mapping><servlet-name>jersey</servlet-name><url-pattern>/rest/*</url-pattern>"
    "</servlet-mapping>\n</web-app>\n"
)


def _endpoint(analyze_files, files: dict[str, str]) -> str | None:
    [r] = [s for s in analyze_files(files) if s.semanticType == "route"]
    return r.endpoint


_APP = _RS + '@ApplicationPath("/api")\npublic class App extends Application {}\n'
_PROPS = "src/main/resources/application.properties"


@pytest.mark.parametrize(("extra", "expected"), [
    ({"src/main/webapp/WEB-INF/web.xml": _WEB_XML}, "/rest/orders"),
    ({_PROPS: "quarkus.rest.path=/q\n"}, "/q/orders"),
    ({_PROPS: "quarkus.resteasy.path=/q\n"}, "/q/orders"),
    ({_PROPS: "spring.jersey.application-path=/q\n"}, "/q/orders"),
    # @ApplicationPath /api and web.xml /rest disagree
    ({_MAIN + "App.java": _APP, "src/main/webapp/WEB-INF/web.xml": _WEB_XML}, None),
    # Quarkus joins its path with @ApplicationPath: not read
    ({_MAIN + "App.java": _APP, _PROPS: "quarkus.rest.path=/q\n"}, None),
    ({_PROPS: "quarkus.rest.path=${BASE}\n"}, None),
])
def test_base_path_from_web_xml_or_properties(analyze_files, extra, expected) -> None:
    assert _endpoint(analyze_files, {_MAIN + "Orders.java": _ORDERS, **extra}) == expected


@pytest.mark.parametrize(("files", "expected"), [
    # the application is in another module: Orders may be served under /api — never bare /orders
    ({"shop-api/src/main/java/a/Orders.java": _ORDERS,
      "shop-app/src/main/java/a/App.java": _APP}, None),
    ({"shop-api/src/main/java/a/Orders.java": _ORDERS}, "/orders"),  # no base anywhere
])
def test_module_without_a_base_is_unknown_when_another_module_sets_one(
        analyze_files, files, expected) -> None:
    assert _endpoint(analyze_files, files) == expected
