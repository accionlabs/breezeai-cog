"""net/http role table (docs/entry-point-roles/net-http.md): one test per row. Rows shared by every
Go router are tested in test_roles_gin.py."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point

MOD = "module example.com/app\n\ngo 1.22\n"
HEAD = 'package main\n\nimport "net/http"\n\n'


def _routes(analyze_files, body: str):
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + body})
    return {(s.method, s.endpoint, s.handler, s.framework) for s in st if is_entry_point(s)}


def test_pattern_without_a_verb_has_no_method(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tmux := http.NewServeMux()\n\tmux.HandleFunc("/users", list)\n}\n') \
        == {(None, "/users", "list", "net-http")}


def test_go_122_verb_in_the_pattern(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tmux := http.NewServeMux()\n\tmux.HandleFunc("GET /items/{id}", get)\n}\n') \
        == {("GET", "/items/{id}", "get", "net-http")}


def test_host_is_not_part_of_the_path(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tmux := http.NewServeMux()\n\tmux.Handle("example.com/x", h)\n}\n') \
        == {(None, "/x", "h", "net-http")}


def test_default_mux(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\thttp.HandleFunc("/health", health)\n}\n') \
        == {(None, "/health", "health", "net-http")}


def test_wrapped_handler(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\thttp.Handle("/a", http.HandlerFunc(a))\n\thttp.Handle("/b", auth(b))\n}\n') \
        == {(None, "/a", "a", "net-http"), (None, "/b", "b", "net-http")}


def test_framework_served_handler(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\thttp.Handle("/static/", http.FileServer(dir))\n}\n') \
        == {(None, "/static/", None, "net-http")}


@pytest.mark.xfail(strict=True, reason="http.StripPrefix mounts are not followed yet")
def test_strip_prefix_mount(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tapi := http.NewServeMux()\n\tapi.HandleFunc("/users", list)\n'
                                  '\thttp.Handle("/api/", http.StripPrefix("/api", api))\n}\n') \
        >= {(None, "/api/users", "list", "net-http")}


def test_client_calls_and_listen_are_not_routes(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\thttp.Get("http://x/y")\n\thttp.ListenAndServe(":8080", nil)\n}\n') == set()


def test_pattern_from_a_wrapper_parameter_has_no_endpoint(analyze_files) -> None:
    assert _routes(analyze_files, 'type App struct{ mux *http.ServeMux }\n\n'
                                  'func (a *App) Handle(path string, h http.HandlerFunc) {\n\ta.mux.HandleFunc(path, h)\n}\n') \
        == {(None, None, "h", "net-http")}
