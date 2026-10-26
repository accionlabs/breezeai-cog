"""chi role table (docs/entry-point-roles/chi.md): one test per row. Rows shared by every Go router
are tested in test_roles_gin.py."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point

MOD = "module example.com/app\n\ngo 1.22\n"
HEAD = 'package main\n\nimport "github.com/go-chi/chi/v5"\n\n'


def _routes(analyze_files, body: str, **extra: str):
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + body, **extra})
    return {(s.method, s.endpoint, s.handler, s.framework) for s in st if is_entry_point(s)}


def test_verb_method(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tr.Get("/a", a)\n}\n') == {("GET", "/a", "a", "chi")}


def test_handle_has_no_method(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tr.HandleFunc("/a", a)\n}\n') == {(None, "/a", "a", "chi")}


def test_method_takes_the_verb(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tr.Method("PATCH", "/a", a)\n}\n') \
        == {("PATCH", "/a", "a", "chi")}


def test_with_keeps_the_prefix(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tr.Route("/api", func(r chi.Router) {\n'
                                  '\t\tr.With(auth).Get("/me", me)\n\t})\n}\n') == {("GET", "/api/me", "me", "chi")}


def test_route_closure_prefix(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tr.Route("/articles", func(r chi.Router) {\n'
                                  '\t\tr.Get("/{id}", get)\n\t\tr.Route("/{id}/comments", func(r chi.Router) {\n'
                                  '\t\t\tr.Post("/", add)\n\t\t})\n\t})\n}\n') \
        == {("GET", "/articles/{id}", "get", "chi"), ("POST", "/articles/{id}/comments/", "add", "chi")}


def test_group_closure_keeps_the_prefix(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tr.Group(func(r chi.Router) {\n\t\tr.Get("/x", x)\n\t})\n}\n') \
        == {("GET", "/x", "x", "chi")}


def test_mount_of_a_local_router(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tsub := chi.NewRouter()\n'
                                  '\tsub.Get("/users", list)\n\tr.Mount("/admin", sub)\n}\n') == {("GET", "/admin/users", "list", "chi")}


def test_mount_of_a_router_returned_by_another_file(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tr.Mount("/admin", adminRouter())\n}\n',
                   **{"admin.go": HEAD + 'func adminRouter() chi.Router {\n\tr := chi.NewRouter()\n'
                                         '\tr.Get("/stats", stats)\n\treturn r\n}\n'}) \
        == {("GET", "/admin/stats", "stats", "chi")}


def test_router_mounted_twice_has_no_endpoint(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tsub := chi.NewRouter()\n'
                                  '\tsub.Get("/x", x)\n\tr.Mount("/a", sub)\n\tr.Mount("/b", sub)\n}\n') == {("GET", None, "x", "chi")}


@pytest.mark.xfail(strict=True, reason="chi middleware is not recorded as guards yet")
def test_use_is_a_guard(analyze_files) -> None:
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + 'func main() {\n\tr := chi.NewRouter()\n\tr.Use(auth)\n\tr.Get("/x", h)\n}\n'})
    assert [s.guards for s in st if is_entry_point(s)] == [["auth"]]


def test_not_found_is_not_a_route(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := chi.NewRouter()\n\tr.NotFound(h)\n\tr.MethodNotAllowed(h)\n}\n') == set()
