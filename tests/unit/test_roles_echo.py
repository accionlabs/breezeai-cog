"""Echo role table (docs/entry-point-roles/echo.md): one test per row. Rows shared by every Go
router are tested in test_roles_gin.py."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point

MOD = "module example.com/app\n\ngo 1.22\n"
HEAD = 'package main\n\nimport "github.com/labstack/echo/v4"\n\n'


def _routes(analyze_files, body: str):
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + body})
    return {(s.method, s.endpoint, s.handler, s.framework) for s in st if is_entry_point(s)}


def test_verb_method(analyze_files) -> None:
    assert _routes(analyze_files, 'func NewHandler(e *echo.Echo) {\n\te.GET("/articles", list, mw)\n}\n') \
        == {("GET", "/articles", "list", "echo")}  # middleware follows the handler
    assert _routes(analyze_files, 'func main() {\n\te := echo.New()\n\te.POST("/articles", create)\n}\n') \
        == {("POST", "/articles", "create", "echo")}


def test_any_is_any(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\te := echo.New()\n\te.Any("/p", proxy)\n}\n') == {("ANY", "/p", "proxy", "echo")}


def test_add_takes_the_verb(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\te := echo.New()\n\te.Add("DELETE", "/x", drop)\n}\n') \
        == {("DELETE", "/x", "drop", "echo")}


def test_connect_has_no_method(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\te := echo.New()\n\te.Add("CONNECT", "/x", h)\n}\n') \
        == {(None, "/x", "h", "echo")}


def test_group_prefix(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\te := echo.New()\n\tg := e.Group("/admin", auth)\n\tg.GET("/users", list)\n}\n') \
        == {("GET", "/admin/users", "list", "echo")}


def test_static_is_framework_served(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\te := echo.New()\n\te.Static("/assets", "public")\n}\n') \
        == {("GET", "/assets", None, "echo")}


@pytest.mark.xfail(strict=True, reason="Echo middleware is not recorded as guards yet")
def test_use_is_a_guard(analyze_files) -> None:
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + 'func main() {\n\te := echo.New()\n\te.Use(auth)\n\te.GET("/x", h)\n}\n'})
    assert [s.guards for s in st if is_entry_point(s)] == [["auth"]]


@pytest.mark.xfail(strict=True, reason="Echo Match is not recorded yet")
def test_match_is_one_binding_per_verb(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\te := echo.New()\n\te.Match([]string{"GET", "PUT"}, "/x", h)\n}\n') \
        == {("GET", "/x", "h", "echo"), ("PUT", "/x", "h", "echo")}
