"""JAX-RS role table: docs/entry-point-roles/jaxrs.md — one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}
SRC = "src/main/java/a/"
RS = "import javax.ws.rs.*;"


def _eps(stmts, file: str | None = None):
    return [
        s
        for s in stmts
        if s.semanticType in EP
        and s.routeKind != "field_resolver"
        and (file is None or s.path == SRC + file)
    ]


def _java(name: str, imports: str, body: str) -> dict[str, str]:
    return {f"{SRC}{name}.java": f"package a;\n{imports}\n{body}\n"}


def test_verb_on_resource_method_is_binding(analyze_files) -> None:
    st = analyze_files(
        _java(
            "Res",
            RS,
            """@Path("/orders")
public class Res {
  @POST
  public void create(String o) {}
}""",
        )
    )
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.framework, r.handler) == (
        "route",
        "POST",
        "route",
        "/orders",
        "jaxrs",
        "create",
    )


def test_method_path_is_part_of_the_binding(analyze_files) -> None:
    st = analyze_files(
        _java(
            "Res",
            RS,
            """@Path("/orders")
public class Res {
  @GET
  @Path("/{id}")
  public String get(@PathParam("id") String id) { return id; }
}""",
        )
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint, r.handler) == ("GET", "/orders/{id}", "get")


def test_class_path_is_wiring(analyze_files) -> None:
    st = analyze_files(
        _java(
            "Res",
            RS,
            """@Path("/orders")
public class Res {
  @GET
  public String list() { return ""; }
  public String helper() { return ""; }
}""",
        )
    )
    [r] = _eps(st)  # one record: the class @Path and the plain method have none
    assert (r.endpoint, r.handler) == ("/orders", "list")


def test_application_path_is_wiring(analyze_files) -> None:
    files = _java(
        "Res",
        RS,
        """@Path("/orders")
public class Res {
  @GET
  public String list() { return ""; }
}""",
    )
    files.update(
        _java(
            "App",
            "import javax.ws.rs.ApplicationPath;\nimport javax.ws.rs.core.Application;",
            '@ApplicationPath("/api")\npublic class App extends Application {}',
        )
    )
    [r] = _eps(analyze_files(files))
    assert r.endpoint == "/api/orders"


def test_sub_resource_locator_is_wiring(analyze_files) -> None:
    files = _java(
        "Res",
        RS,
        """@Path("/orders")
public class Res {
  @Path("/items")
  public ItemRes items() { return new ItemRes(); }
}""",
    )
    files.update(
        _java(
            "ItemRes",
            RS,
            """public class ItemRes {
  @GET
  public String list() { return ""; }
}""",
        )
    )
    st = analyze_files(files)
    assert _eps(st, "Res.java") == []  # the locator itself is wiring
    [r] = _eps(st, "ItemRes.java")
    assert r.endpoint == "/orders/items"


@pytest.mark.xfail(strict=True, reason="security annotations are not recorded in guards")
def test_roles_allowed_is_guard(analyze_files) -> None:
    st = analyze_files(
        _java(
            "Res",
            RS + "\nimport javax.annotation.security.RolesAllowed;",
            """@Path("/orders")
public class Res {
  @DELETE
  @RolesAllowed("admin")
  public void delete() {}
}""",
        )
    )
    [r] = _eps(st)
    assert r.guards and "RolesAllowed" in r.guards[0]


def test_container_filter_has_no_record(analyze_files) -> None:
    st = analyze_files(
        _java(
            "Auth",
            """import javax.ws.rs.container.ContainerRequestFilter;
import javax.ws.rs.container.ContainerRequestContext;
import javax.ws.rs.ext.Provider;""",
            """@Provider
public class Auth implements ContainerRequestFilter {
  public void filter(ContainerRequestContext c) {}
}""",
        )
    )
    assert _eps(st) == []


def test_client_call_is_not_entry_point(analyze_files) -> None:
    st = analyze_files(
        _java(
            "Cl",
            "import javax.ws.rs.client.ClientBuilder;",
            """public class Cl {
  public String fetch() {
    return ClientBuilder.newClient().target("http://billing/api").request().get(String.class);
  }
}""",
        )
    )
    assert _eps(st) == []


def test_application_without_path_is_none(analyze_files) -> None:
    st = analyze_files(
        _java(
            "App", "import javax.ws.rs.core.Application;", "public class App extends Application {}"
        )
    )
    assert _eps(st) == []
