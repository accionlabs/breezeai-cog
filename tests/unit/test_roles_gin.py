"""Gin role table (docs/entry-point-roles/gin.md): one test per row. The rows marked (shared) cover
the router analysis every Go router detector uses (parsers/go/routing.py)."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point

MOD = "module example.com/app\n\ngo 1.22\n"
HEAD = 'package main\n\nimport "github.com/gin-gonic/gin"\n\n'


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _routes(stmts):
    return {(r.method, r.endpoint, r.handler) for r in _eps(stmts)}


def _app(analyze_files, body: str, **extra: str):
    return analyze_files({"go.mod": MOD, "main.go": HEAD + body, **extra})


def test_verb_with_a_function_of_this_file(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.Default()\n\tr.GET("/users", listUsers)\n}\n\n'
                             "func listUsers(c *gin.Context) {}\n")
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.framework, r.endpoint, r.handler,
            r.handlerLine, r.handlerPath) == ("route", "GET", "route", "gin", "/users",
                                              "listUsers", 10, None)


def test_function_literal_has_handler_line_only(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tr.POST("/x",\n\t\tfunc(c *gin.Context) {})\n}\n')
    [r] = _eps(st)
    assert (r.method, r.endpoint, r.handler, r.handlerLine) == ("POST", "/x", None, 8)


def test_middleware_before_the_handler(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tr.GET("/x", auth, show)\n}\n'
                             "func auth(c *gin.Context) {}\nfunc show(c *gin.Context) {}\n")
    assert _routes(st) == {("GET", "/x", "show")}


def test_handler_in_another_file_and_package(analyze_files) -> None:
    st = _app(analyze_files,
              'func main() {\n\tr := gin.New()\n\tr.GET("/a", other)\n\tr.GET("/b", users.List)\n}\n',
              **{"other.go": "package main\n\nfunc other() {}\n",
                 "users/h.go": "package users\n\n\nfunc List() {}\n"})
    st = [s for s in st if s.path == "main.go"]
    # the import is added here so the shared HEAD stays a single import
    assert {(r.handler, r.handlerLine, r.handlerPath) for r in _eps(st)} >= {("other", 3, "other.go")}


def test_handler_in_another_package(analyze_files) -> None:
    st = analyze_files({
        "go.mod": MOD,
        "main.go": ('package main\n\nimport (\n\t"github.com/gin-gonic/gin"\n\t"example.com/app/users"\n)\n\n'
                    'func main() {\n\tr := gin.New()\n\tr.GET("/b", users.List)\n}\n'),
        "users/h.go": "package users\n\n\nfunc List() {}\n",
    })
    [r] = _eps(st)
    assert (r.handler, r.handlerLine, r.handlerPath) == ("users.List", 4, "users/h.go")


def test_method_value_on_a_known_receiver(analyze_files) -> None:
    st = _app(analyze_files, "type Handler struct{}\n\nfunc (h *Handler) List(c *gin.Context) {}\n\n"
                             'func main() {\n\tr := gin.New()\n\th := &Handler{}\n\tr.GET("/x", h.List)\n}\n')
    [r] = _eps(st)
    assert (r.handler, r.handlerLine) == ("h.List", 7)


def test_method_value_on_an_unknown_receiver(analyze_files) -> None:
    st = _app(analyze_files,
              "type A struct{}\ntype B struct{}\n\nfunc (A) Show(c *gin.Context) {}\nfunc (B) Show(c *gin.Context) {}\n"
              "func (A) Only(c *gin.Context) {}\n\n"
              'func main() {\n\tr := gin.New()\n\th := build()\n\tr.GET("/s", h.Show)\n\tr.GET("/o", h.Only)\n}\n')
    lines = {r.handler: r.handlerLine for r in _eps(st)}
    assert lines == {"h.Show": None, "h.Only": 10}


def test_wrapped_handler(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tr.GET("/x", gin.WrapF(raw))\n}\n'
                             "func raw() {}\n")
    assert _routes(st) == {("GET", "/x", "raw")}


def test_any_is_any(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tr.Any("/proxy", proxy)\n}\n')
    assert _routes(st) == {("ANY", "/proxy", "proxy")}


def test_handle_takes_the_verb_as_an_argument(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tr.Handle("PUT", "/a", h)\n'
                             '\tr.Handle(verb, "/b", h)\n}\n')
    assert _routes(st) == {("PUT", "/a", "h"), (None, "/b", "h")}


def test_group_prefix(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tapi := r.Group("/api")\n'
                             '\tv1 := api.Group("/v1")\n\tv1.GET("/users", h)\n}\n')
    assert _routes(st) == {("GET", "/api/v1/users", "h")}


def test_router_passed_to_a_function_in_another_package(analyze_files) -> None:
    st = analyze_files({
        "go.mod": MOD,
        "main.go": ('package main\n\nimport (\n\t"github.com/gin-gonic/gin"\n\t"example.com/app/users"\n)\n\n'
                    'func main() {\n\tr := gin.Default()\n\tv1 := r.Group("/api")\n'
                    '\tusers.Register(v1.Group("/users"))\n}\n'),
        "users/routers.go": ('package users\n\nimport "github.com/gin-gonic/gin"\n\n'
                             'func Register(router *gin.RouterGroup) {\n\trouter.POST("/login", Login)\n}\n'),
    })
    assert _routes(st) == {("POST", "/api/users/login", "Login")}


def test_function_given_two_prefixes_has_no_endpoint(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tmount(r.Group("/a"))\n\tmount(r.Group("/b"))\n}\n\n'
                             'func mount(g *gin.RouterGroup) {\n\tg.GET("/x", h)\n}\n')
    assert _routes(st) == {("GET", None, "h")}


def test_router_parameter_nothing_calls_is_served_at_its_own_paths(analyze_files) -> None:
    st = _app(analyze_files, 'func Routes(g *gin.RouterGroup) {\n\tg.GET("/x", h)\n}\n')
    assert _routes(st) == {("GET", "/x", "h")}


def test_app_field_declared_in_another_file_is_the_root(analyze_files) -> None:
    st = _app(analyze_files, 'func (s *Server) routes() {\n\ts.router.GET("/health", s.health)\n}\n',
              **{"server.go": HEAD + "type Server struct {\n\trouter *gin.Engine\n}\n\n"
                              "func (s *Server) health(c *gin.Context) {}\n"})
    [r] = _eps(st)
    assert (r.endpoint, r.handler, r.handlerLine, r.handlerPath) == ("/health", "s.health", 9, "server.go")


def test_group_field_has_no_endpoint(analyze_files) -> None:
    st = _app(analyze_files, "type API struct {\n\tg *gin.RouterGroup\n}\n\n"
                             'func (a *API) routes() {\n\ta.g.GET("/x", h)\n}\n')
    assert _routes(st) == {("GET", None, "h")}


def test_path_from_constants(analyze_files) -> None:
    st = _app(analyze_files, 'const base = "/api"\n\nfunc main() {\n\tr := gin.New()\n'
                             '\tr.GET(base+"/users", h)\n}\n')
    assert _routes(st) == {("GET", "/api/users", "h")}


def test_path_from_a_variable_has_no_endpoint(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tp := cfg.Path()\n\tr.GET(p, h)\n}\n')
    assert _routes(st) == {("GET", None, "h")}


def test_static_is_framework_served(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tr.Static("/assets", "./public")\n}\n')
    [r] = _eps(st)
    assert (r.method, r.endpoint, r.handler, r.handlerLine) == ("GET", "/assets", None, None)


@pytest.mark.xfail(strict=True, reason="Gin middleware is not recorded as guards yet")
def test_use_is_a_guard(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tr.Use(auth)\n\tr.GET("/x", h)\n}\n')
    [r] = _eps(st)
    assert r.guards == ["auth"]


def test_no_route_and_run_are_not_routes(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tr := gin.New()\n\tr.NoRoute(h)\n\tr.Run(":8080")\n}\n')
    assert _eps(st) == []


def test_a_value_that_is_not_a_router(analyze_files) -> None:
    st = _app(analyze_files, 'func main() {\n\tclient := api.NewClient()\n\tclient.GET("/users", h)\n'
                             "\tvar router Router\n\trouter.GET(\"/x\", h)\n}\n")
    assert _eps(st) == []


def test_fixture_file_has_no_routes(analyze_files) -> None:
    st = analyze_files({"go.mod": MOD, "mock/routes.go": HEAD + 'func m() {\n\tr := gin.New()\n\tr.GET("/x", h)\n}\n'})
    assert _eps(st) == []
