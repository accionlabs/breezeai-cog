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
            First struct{}
            Second interface{}
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

    assert (first.startLine, first.endLine) == (4, 4)
    assert (second.startLine, second.endLine) == (5, 5)


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
    assert "net-http" in GoParser.frameworks
    assert "gorilla-mux" in GoParser.frameworks

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

    routes = {
        (item.framework, item.method, item.endpoint)
        for item in record.statements
        if item.semanticType == "route"
    }
    assert ("net-http", "HANDLEFUNC", "/http") in routes
    assert ("gin", "GET", "/gin") in routes
    assert ("echo", "POST", "/echo") in routes
    assert ("chi", "GET", "/chi") in routes
    assert all(endpoint != "https://example.test" for _, _, endpoint in routes)
    save_routes = [item for item in record.statements if item.semanticType == "route" and item.endpoint == "/http"]
    assert save_routes and save_routes[0].parentId == functions["Save"].id
    assert any(item.semanticType == "api_call" and item.method == "GET" for item in record.statements)
    assert not any(
        item.semanticType == "db_method_call" and item.method in {"GET", "SAVE"}
        for item in record.statements
    )

    fixture_record = GoParser().parse_file(
        ParseContext(
            path="handlers.test.go",
            abs_path=path,
            source=src,
            repo_root=tmp_path,
            capture_statements=True,
        )
    )
    assert not any(item.semanticType == "route" for item in fixture_record.statements)


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


def test_go_captures_anonymous_functions_channels_events_and_timers(tmp_path) -> None:
    src = b'''\
package demo

import (
    "time"
    "github.com/nats-io/nats.go"
)

func run(nc *nats.Conn, ch chan int) {
    for value := range ch {
        ch <- value
    }
    time.AfterFunc(time.Second, func() {})
    nc.Publish("orders.created", nil)
    nc.Subscribe("orders.created", func(msg *nats.Msg) {})
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
    assert any(statement.nodeType == "range_clause" for statement in record.statements)
    assert any(statement.nodeType == "send_statement" for statement in record.statements)
    assert any(
        statement.semanticType == "eventbus_send"
        and statement.framework == "nats"
        and statement.endpoint == "orders.created"
        for statement in record.statements
    )
    assert any(
        statement.semanticType == "eventbus_consumer"
        and statement.framework == "nats"
        and statement.endpoint == "orders.created"
        for statement in record.statements
    )
    assert any(
        statement.semanticType == "timer"
        and statement.framework == "time"
        for statement in record.statements
    )


def test_go_event_detection_requires_typed_bus_receiver(tmp_path) -> None:
    src = b'''\
package demo

import "github.com/nats-io/nats.go"

type Client struct{}

func run(nc *nats.Conn, client *Client) {
    nc.Publish("orders.created", nil)
    client.Publish("not-an-event", nil)
    client.Request("not-an-event", nil)
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

    events = [statement for statement in record.statements if statement.semanticType == "eventbus_send"]
    assert len(events) == 1
    assert events[0].endpoint == "orders.created"


def test_go_captures_fiber_grpc_and_kafka(tmp_path) -> None:
    src = b'''\
package demo

import (
    "github.com/gofiber/fiber/v2"
    "google.golang.org/grpc"
    "github.com/segmentio/kafka-go"
)

func register(app *fiber.App, server grpc.ServiceRegistrar, writer *kafka.Writer, reader *kafka.Reader) {
    app.Get("/health", func(c *fiber.Ctx) error { return nil })
    grpc.RegisterGreeterServer(server, handler{})
    writer.WriteMessages(nil)
    reader.ReadMessage(nil)
}
'''
    path = tmp_path / "transport.go"
    path.write_bytes(src)
    record = GoParser().parse_file(
        ParseContext(
            path="transport.go",
            abs_path=path,
            source=src,
            repo_root=tmp_path,
            capture_statements=True,
        )
    )

    assert any(
        statement.semanticType == "route"
        and statement.framework == "fiber"
        and statement.method == "GET"
        and statement.endpoint == "/health"
        for statement in record.statements
    )
    assert any(
        statement.semanticType == "route" and statement.framework == "grpc"
        for statement in record.statements
    )
    assert any(
        statement.semanticType == "eventbus_send" and statement.framework == "kafka"
        for statement in record.statements
    )
    assert any(
        statement.semanticType == "eventbus_consumer" and statement.framework == "kafka"
        for statement in record.statements
    )
