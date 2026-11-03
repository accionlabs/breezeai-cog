"""Fiber role table (docs/entry-point-roles/fiber.md): one test per row. Rows shared by every Go
router are tested in test_roles_gin.py."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

MOD = "module example.com/app\n\ngo 1.22\n"
HEAD = 'package main\n\nimport "github.com/gofiber/fiber/v2"\n\n'


def _routes(analyze_files, body: str):
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + body})
    return {(s.method, s.endpoint, s.handler, s.framework) for s in st if is_entry_point(s)}


def test_verb_method(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tapp := fiber.New()\n\tapp.Get("/users/:id", getUser)\n}\n') \
        == {("GET", "/users/:id", "getUser", "fiber")}


def test_all_is_any(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tapp := fiber.New()\n\tapp.All("/p", p)\n}\n') == {("ANY", "/p", "p", "fiber")}


def test_add_takes_the_verb(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tapp := fiber.New()\n\tapp.Add("PUT", "/x", put)\n}\n') == {("PUT", "/x", "put", "fiber")}


def test_group_prefix(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tapp := fiber.New()\n\tapi := app.Group("/api", auth)\n\tapi.Post("/x", x)\n}\n') \
        == {("POST", "/api/x", "x", "fiber")}


def test_route_closure_prefix(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tapp := fiber.New()\n\tapp.Route("/admin", func(r fiber.Router) {\n'
                                  '\t\tr.Delete("/x", x)\n\t})\n}\n') == {("DELETE", "/admin/x", "x", "fiber")}


def test_mount_of_a_sub_app(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tapp := fiber.New()\n\tsub := fiber.New()\n\tsub.Get("/x", x)\n'
                                  '\tapp.Mount("/v2", sub)\n}\n') == {("GET", "/v2/x", "x", "fiber")}


def test_static_is_framework_served(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tapp := fiber.New()\n\tapp.Static("/", "./public")\n}\n') \
        == {("GET", "/", None, "fiber")}


def test_use_is_a_guard(analyze_files) -> None:
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + 'func main() {\n\tapp := fiber.New()\n\tapp.Use(auth)\n\tapp.Get("/x", h)\n}\n'})
    assert [s.guards for s in st if is_entry_point(s)] == [["auth"]]


def test_path_scoped_use_is_not_recorded(analyze_files) -> None:
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + 'func main() {\n\tapp := fiber.New()\n\tapp.Use("/admin", auth)\n'
                                                   '\tapp.Get("/admin/x", h)\n}\n'})
    assert [s.guards for s in st if is_entry_point(s)] == [None]
