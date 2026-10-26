"""gorilla/mux role table (docs/entry-point-roles/gorilla-mux.md): one test per row. Rows shared by
every Go router are tested in test_roles_gin.py."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point

MOD = "module example.com/app\n\ngo 1.22\n"
HEAD = 'package main\n\nimport (\n\t"net/http"\n\n\t"github.com/gorilla/mux"\n)\n\n'


def _routes(analyze_files, body: str):
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + body})
    return {(s.method, s.endpoint, s.handler, s.framework) for s in st if is_entry_point(s)}


def test_methods_is_one_binding_per_verb(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := mux.NewRouter()\n\tr.HandleFunc("/u/{id}", u).Methods("GET", "PUT")\n}\n') \
        == {("GET", "/u/{id}", "u", "gorilla-mux"), ("PUT", "/u/{id}", "u", "gorilla-mux")}


def test_methods_with_http_constants(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := mux.NewRouter()\n\tr.Handle("/x", h).Methods(http.MethodPost)\n}\n') \
        == {("POST", "/x", "h", "gorilla-mux")}


def test_no_methods_has_no_method(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := mux.NewRouter()\n\tr.HandleFunc("/x", h)\n}\n') \
        == {(None, "/x", "h", "gorilla-mux")}


def test_path_prefix_subrouter(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := mux.NewRouter()\n\tapi := r.PathPrefix("/api").Subrouter()\n'
                                  '\tapi.HandleFunc("/users", list).Methods("GET")\n}\n') == {("GET", "/api/users", "list", "gorilla-mux")}


def test_matcher_subrouter_keeps_the_prefix(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := mux.NewRouter()\n\ts := r.Host("example.com").Subrouter()\n'
                                  '\ts.HandleFunc("/x", h)\n}\n') == {(None, "/x", "h", "gorilla-mux")}


@pytest.mark.xfail(strict=True, reason="routes on a PathPrefix (Handler) are not recorded yet")
def test_path_prefix_handler(analyze_files) -> None:
    assert _routes(analyze_files, 'func main() {\n\tr := mux.NewRouter()\n\tr.PathPrefix("/static/").Handler(files)\n}\n') \
        == {(None, "/static/", "files", "gorilla-mux")}


@pytest.mark.xfail(strict=True, reason="gorilla/mux middleware is not recorded as guards yet")
def test_use_is_a_guard(analyze_files) -> None:
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + 'func main() {\n\tr := mux.NewRouter()\n\tr.Use(auth)\n\tr.HandleFunc("/x", h)\n}\n'})
    assert [s.guards for s in st if is_entry_point(s)] == [["auth"]]
