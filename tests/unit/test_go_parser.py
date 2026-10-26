from __future__ import annotations

import gzip
import json
from textwrap import dedent
from breezeai_cog import analyze_repo, capabilities
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.go.parser import GoParser


def test_go_index_keeps_a_deterministic_target_for_multi_file_packages(tmp_path) -> None:
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    first = package_dir / "a.go"
    second = package_dir / "b.go"
    first.write_text("package pkg\n")
    second.write_text("package pkg\n")
    (tmp_path / "go.mod").write_text("module example.com/demo\n")

    index = GoParser().build_index(tmp_path, [second, first])

    assert index.modules["example.com/demo/pkg"] == "pkg/a.go"


def test_go_resolves_calls_in_named_and_anonymous_functions(tmp_path) -> None:
    src = b'''\
package demo

func helper() {}

func main() {
    helper()
    func() { helper() }()
}
'''
    path = tmp_path / "main.go"
    path.write_bytes(src)

    record = GoParser().parse_file(
        ParseContext(
            path="main.go",
            abs_path=path,
            source=src,
            repo_root=tmp_path,
        )
    )

    assert record.functions[0].name == "helper"
    assert [call.path for call in record.functions[1].calls] == ["main.go"]
    anonymous = next(function for function in record.functions if function.type == "anonymous")
    assert [call.path for call in anonymous.calls] == ["main.go"]


# def test_go_grouped_type_declaration_emits_each_type_with_its_span(tmp_path) -> None:
#     src = b'''\
# package demo

# type (
#     First struct{}
#     Second interface{}
# )
# '''
#     path = tmp_path / "types.go"
#     path.write_bytes(src)

#     record = GoParser().parse_file(
#         ParseContext(path="types.go", abs_path=path, source=src, repo_root=tmp_path)
#     )

#     classes = {item.name: item for item in record.classes}
#     assert set(classes) == {"First", "Second"}
#     assert classes["First"].type == "struct"
#     assert classes["Second"].type == "interface"
#     assert (classes["First"].startLine, classes["First"].endLine) == (4, 4)
#     assert (classes["Second"].startLine, classes["Second"].endLine) == (5, 5)


def test_go_grouped_type_declaration_emits_each_type_with_its_span(tmp_path) -> None:
    source = dedent(
        """
        package demo

        type (
            First struct {
                Value int
            }
            Second interface {
                Read() error
            }
        )
        """
    ).encode()

    path = tmp_path / "types.go"
    path.write_bytes(source)

    record = GoParser().parse_file(
        ParseContext(
            path="types.go",
            abs_path=path,
            source=source,
            repo_root=tmp_path,
        )
    )

    classes = {item.name: item for item in record.classes}

    assert set(classes) == {"First", "Second"}

    first = classes["First"]
    second = classes["Second"]

    assert first.type == "struct"
    assert second.type == "interface"

    assert (first.startLine, first.endLine) == (5, 7)
    assert (second.startLine, second.endLine) == (8, 10)


def test_go_generic_receiver_is_parented_to_base_type(tmp_path) -> None:
    src = b'''\
package demo

type List[T any] struct{}

func (l *List[T]) Add(value T) {}
'''
    path = tmp_path / "list.go"
    path.write_bytes(src)

    record = GoParser().parse_file(
        ParseContext(path="list.go", abs_path=path, source=src, repo_root=tmp_path)
    )

    classes = {item.name: item for item in record.classes}
    functions = {item.name: item for item in record.functions}
    assert functions["Add"].parentId == classes["List"].id
    assert functions["Add"].receiverType == "List"


def test_go_constructor_target_uses_return_type(tmp_path) -> None:
    src = b'''\
package demo

type Config struct{}

func NewFromEnv(host string) *Config {
    return &Config{}
}
'''
    path = tmp_path / "config.go"
    path.write_bytes(src)

    record = GoParser().parse_file(
        ParseContext(path="config.go", abs_path=path, source=src, repo_root=tmp_path)
    )

    config = next(item for item in record.classes if item.name == "Config")
    assert [param.name for param in config.constructorParams] == ["host"]


def test_go_code_and_config_parsing(tmp_path) -> None:
    src = b'''
package main

import (
    "fmt"
    "net/http"
)

type User struct {
    ID int
}

func main() {
    http.HandleFunc("/hello", func(w http.ResponseWriter, r *http.Request) {
        fmt.Fprintf(w, "hello")
    })
    _ = http.ListenAndServe(":8080", nil)
}

func GetUser(id int) User {
    return User{ID: id}
}
'''
    p = tmp_path / "main.go"
    p.write_bytes(src)

    rec = GoParser().parse_file(
        ParseContext(
            path="main.go",
            abs_path=p,
            source=src,
            repo_root=tmp_path,
            capture_statements=True,
        )
    )

    assert rec.type == "code"
    assert rec.language == "go"
    assert {f.name for f in rec.functions} >= {"main", "GetUser"}
    assert any(c.name == "User" for c in rec.classes)

    mod = tmp_path / "go.mod"
    mod.write_text("module example.com/demo\n\ngo 1.22\n\nrequire github.com/gin-gonic/gin v1.10.0\n")
    mod_rec = GoParser().parse_file(ParseContext(path="go.mod", abs_path=mod, source=mod.read_bytes(), repo_root=tmp_path))
    assert mod_rec.type == "config"
    assert mod_rec.language == "go"
    assert mod_rec.metadata["kind"] == "gomod"
    assert mod_rec.metadata["module"] == "example.com/demo"
    assert mod_rec.metadata["dependencies"] == ["github.com/gin-gonic/gin"]
    assert "require" not in mod_rec.metadata

    sumf = tmp_path / "go.sum"
    sumf.write_text("github.com/gin-gonic/gin v1.10.0 h1:abc\n")
    sum_rec = GoParser().parse_file(ParseContext(path="go.sum", abs_path=sumf, source=sumf.read_bytes(), repo_root=tmp_path))
    assert sum_rec.type == "config"
    assert sum_rec.language == "go"
    assert sum_rec.metadata["kind"] == "gosum"
    assert sum_rec.metadata["dependencyCount"] == 1


def test_go_acceptance_contract(tmp_path) -> None:
    src = b'''\
package demo

import (
    "net/http"
    "github.com/gin-gonic/gin"
    "github.com/labstack/echo/v4"
    "github.com/go-chi/chi/v5"
)

type PublicType struct{}
type privateType struct{}
type Base struct{}
type Wrapper struct { Base }
type Alias PublicType
type Reader interface { Read([]byte) (int, error) }

func NewPublicType(value int) *PublicType { return &PublicType{} }
func (p *PublicType) Save(value int) (int, error) {
    http.HandleFunc("/http", func(w http.ResponseWriter, r *http.Request) {
        http.Get("https://example.test")
    })
    return value, nil
}

func register(g *gin.Engine, e *echo.Echo, r chi.Router) {
    g.GET("/gin", func(c *gin.Context) {})
    e.POST("/echo", func(c echo.Context) error { return nil })
    r.Get("/chi", func(w http.ResponseWriter, r *http.Request) {})
}

func ordinary(client *Client, cache *Cache) {
    client.Get("key")
    cache.Save("value")
}
'''
    path = tmp_path / "main.go"
    path.write_bytes(src)
    record = GoParser().parse_file(
        ParseContext(
            path="main.go",
            abs_path=path,
            source=src,
            repo_root=tmp_path,
            capture_statements=True,
        )
    )

    classes = {item.name: item for item in record.classes}
    assert classes["PublicType"].type == "struct"
    assert classes["PublicType"].visibility == "public"
    assert classes["privateType"].visibility == "package"
    assert classes["Wrapper"].extends == "Base"
    assert classes["Alias"].type == "class"
    assert classes["Reader"].type == "interface"
    assert classes["Reader"].implements == []
    assert [item.name for item in classes["PublicType"].constructorParams] == ["value"]

    functions = {item.name: item for item in record.functions}
    assert functions["Save"].parentId == classes["PublicType"].id
    assert functions["Save"].returnType == "(int, error)"
    assert functions["Save"].visibility == "public"
    assert functions["Save"].metadata == {"receiverKind": "pointer"}

    assert any(item.semanticType == "api_call" and item.method == "GET" for item in record.statements)
    assert not any(
        item.semanticType == "db_method_call" and item.method in {"GET", "SAVE"}
        for item in record.statements
    )



def test_go_repo_is_analyzed_and_capabilities_match_ticket(tmp_path) -> None:
    repo = tmp_path / "gorepo"
    repo.mkdir()
    (repo / "main.go").write_text(
        'package demo\n\nimport "net/http"\n\ntype User struct{}\n\nfunc NewUser() *User { return &User{} }\n\nfunc main() {\n    http.HandleFunc("/ping", func(w http.ResponseWriter, r *http.Request) {})\n}\n',
        encoding="utf-8",
    )
    (repo / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
    (repo / "go.sum").write_text("example.com/demo v0.0.0 h1:abc\n", encoding="utf-8")

    result = analyze_repo(repo, capture_statements=True)
    assert result.written is True
    assert result.out_path is not None and result.out_path.exists()

    with gzip.open(result.out_path, "rt", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]

    assert records[0]["__type"] == "projectMetaData"
    assert "go" in records[0]["analyzedLanguages"]
    assert any(r.get("language") == "go" for r in records[1:])

    caps = capabilities()
    assert "go" in caps["languages"]
    assert ".go" in caps["extensions"]
    assert "go.mod" in caps["extensions"]
    assert "go.sum" in caps["extensions"]


def test_go_captures_anonymous_functions_and_channels(tmp_path) -> None:
    src = b'''\
package demo

import "time"

func run(ch chan int) {
    for value := range ch {
        ch <- value
    }
    time.AfterFunc(time.Second, func() {})
}
'''
    path = tmp_path / "events.go"
    path.write_bytes(src)
    record = GoParser().parse_file(
        ParseContext(
            path="events.go",
            abs_path=path,
            source=src,
            repo_root=tmp_path,
            capture_statements=True,
        )
    )

    assert any(function.type == "anonymous" for function in record.functions)
    assert any(
        statement.nodeType == "for_statement" and "range ch" in statement.text
        for statement in record.statements
    )
    assert any(statement.nodeType == "send_statement" for statement in record.statements)


def _parse(tmp_path, src: bytes, name: str = "main.go"):
    path = tmp_path / name
    path.write_bytes(src)
    return GoParser().parse_file(
        ParseContext(path=name, abs_path=path, source=src, repo_root=tmp_path, capture_statements=True)
    )


def test_go_call_statement_is_one_record_classified_on_the_statement(tmp_path) -> None:
    record = _parse(tmp_path, b'''package p

import "net/http"

func f() {
    http.Get("http://x/y")
    log(1)
}
''')

    rows = [(s.startLine, s.nodeType) for s in record.statements]
    assert rows == [(6, "expression_statement"), (7, "expression_statement")]
    call = record.statements[0]
    assert (call.semanticType, call.method, call.endpoint) == ("api_call", "GET", "http://x/y")


def test_go_switch_inc_and_package_level_declarations_are_captured(tmp_path) -> None:
    record = _parse(tmp_path, b'''package p

const BaseURL = "/api"

var (
    db *Store
)

func f(x int) {
    switch x {
    case 1:
        x++
        fallthrough
    default:
        x--
    }
}
''')

    by_line = {s.startLine: s for s in record.statements}
    fid = record.id
    assert (by_line[3].nodeType, by_line[3].name, by_line[3].parentId) == ("const_declaration", "BaseURL", fid)
    assert (by_line[5].nodeType, by_line[5].name, by_line[5].parentId) == ("var_declaration", "db", fid)
    f = next(fn for fn in record.functions if fn.name == "f")
    assert by_line[10].nodeType == "expression_switch_statement"
    assert by_line[12].nodeType == "inc_statement"
    assert by_line[13].nodeType == "fallthrough_statement"
    assert by_line[15].nodeType == "dec_statement"
    assert all(by_line[line].parentId == f.id for line in (10, 12, 13, 15))


def test_go_params_keep_every_name_and_variadic(tmp_path) -> None:
    record = _parse(tmp_path, b'''package p

func Add(a, b int, rest ...string) (sum int, err error) { return 0, nil }
func Pair(int, string) *User { return nil }
''')

    fns = {fn.name: fn for fn in record.functions}
    assert [(p.name, p.type) for p in fns["Add"].params] == [
        ("a", "int"), ("b", "int"), ("rest", "...string"),
    ]
    assert fns["Add"].returnType == "(int, error)"
    assert [(p.name, p.type) for p in fns["Pair"].params] == [("", "int"), ("", "string")]
    assert fns["Pair"].returnType == "*User"


def test_go_interface_method_set_is_captured(tmp_path) -> None:
    record = _parse(tmp_path, b'''package p

type Store interface {
    fmt.Stringer
    Get(id string) (*User, error)
    save(u *User) error
}
''')

    store = next(c for c in record.classes if c.name == "Store")
    methods = {fn.name: fn for fn in record.functions}
    assert set(methods) == {"Get", "save"}
    get = methods["Get"]
    assert (get.parentId, get.type, get.visibility, get.startLine) == (store.id, "method", "public", 5)
    assert [(p.name, p.type) for p in get.params] == [("id", "string")]
    assert get.returnType == "(*User, error)"
    assert methods["save"].visibility == "package"


def test_go_closures_are_parented_by_tree_not_by_line(tmp_path) -> None:
    record = _parse(tmp_path, b'''package p

func f() {
    a := func() {}; b := func() { _ = func() {} }
    _, _ = a, b
}
''')

    f = next(fn for fn in record.functions if fn.name == "f")
    anon = sorted((fn for fn in record.functions if fn.type == "anonymous"), key=lambda fn: fn.id)
    assert len(anon) == 3
    parents = {fn.parentId for fn in anon}
    # a and b both belong to f; the literal inside b belongs to b — never a sibling on the same line.
    assert sum(1 for fn in anon if fn.parentId == f.id) == 2
    inner = next(fn for fn in anon if fn.parentId != f.id)
    assert inner.parentId in {fn.id for fn in anon} and parents - {f.id} == {inner.parentId}


def test_go_index_resolves_packages_in_nested_modules(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/root\n")
    api = tmp_path / "services" / "api"
    (api / "users").mkdir(parents=True)
    (api / "go.mod").write_text("module example.com/api\n")
    (api / "users" / "h.go").write_text("package users\n")
    (tmp_path / "util").mkdir()
    (tmp_path / "util" / "u.go").write_text("package util\n")

    files = [tmp_path / "go.mod", api / "go.mod", api / "users" / "h.go", tmp_path / "util" / "u.go"]
    index = GoParser().build_index(tmp_path, files)

    assert index.modules == {
        "example.com/api/users": "services/api/users/h.go",
        "example.com/root/util": "util/u.go",
    }


def test_generated_mocks_are_ignored_and_protobuf_is_kept(tmp_path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/r\n")
    for name in ("store.go", "store_mock.go", "mock_client.go", "zz_generated.deepcopy.go",
                 "auth.pb.go", "auth_grpc.pb.go"):
        (repo / name).write_text("package r\n\nfunc F() {}\n")

    result = analyze_repo(repo, capture_statements=True)
    with gzip.open(result.out_path, "rt", encoding="utf-8") as fh:
        paths = {json.loads(line).get("path") for line in fh}

    assert {"store.go", "auth.pb.go", "auth_grpc.pb.go"} <= paths
    assert not paths & {"store_mock.go", "mock_client.go", "zz_generated.deepcopy.go"}


def test_type_alias_is_a_class(tmp_path) -> None:
    record = _parse(tmp_path, b'''package p

type ID = string

type (
    Handler = func() error
    Role string
)
''')

    classes = {c.name: (c.type, c.startLine, c.visibility) for c in record.classes}
    assert classes == {"ID": ("class", 3, "public"), "Handler": ("class", 6, "public"),
                       "Role": ("class", 7, "public")}


def test_import_files_are_the_files_declaring_the_names_used(tmp_path) -> None:
    repo = tmp_path / "r"
    (repo / "users").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/app\n")
    (repo / "users" / "routers.go").write_text("package users\n\nfunc Register() {}\n")
    (repo / "users" / "models.go").write_text("package users\n\ntype User struct{}\n\nconst Admin = 1\n")
    (repo / "users" / "validators.go").write_text("package users\n\nfunc Validate() {}\n")
    (repo / "main.go").write_text(
        'package main\n\nimport (\n\t"fmt"\n\t"example.com/app/users"\n)\n\n'
        "func main() {\n\tvar u users.User\n\tusers.Register()\n\tfmt.Println(u, users.Admin)\n}\n"
    )

    result = analyze_repo(repo, capture_statements=True)
    with gzip.open(result.out_path, "rt", encoding="utf-8") as fh:
        main = next(r for r in map(json.loads, fh) if r.get("path") == "main.go")

    assert main["importFiles"] == ["users/models.go", "users/routers.go"]
    assert main["externalImports"] == ["fmt"]
    register = next(c for f in main["functions"] for c in f["calls"] if c["name"] == "Register")
    assert register["path"] == "users/routers.go"
