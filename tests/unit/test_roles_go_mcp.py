"""MCP server tools of the official Go SDK (``github.com/modelcontextprotocol/go-sdk``): one
``mcp_tool`` statement per ``AddTool`` registration.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).
"""

from __future__ import annotations

from breezeai_cog.schemas import Statement


def _tools(statements: list[Statement]) -> dict[str | None, Statement]:
    tools = [s for s in statements if s.semanticType == "mcp_tool"]
    for t in tools:
        assert t.method == "TOOL" and t.nodeType
    return {t.endpoint: t for t in tools}


def _params(tool: Statement) -> list[tuple]:
    return [(p.name, p.type, p.required, p.default, p.description) for p in tool.params or []]


GO_SDK = """
package main

import (
	"context"

	"github.com/google/jsonschema-go/jsonschema"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type GreetArgs struct {
	Name   string   `json:"name" jsonschema:"the person to greet"`
	Times  *int     `json:"times,omitempty"`
	Tags   []string `json:"tags,omitzero"`
	Score  float64
	Extra  map[string]any `json:"extra"`
	When   Clock          `json:"when"`
}

func SayHi(ctx context.Context, req *mcp.CallToolRequest, args GreetArgs) (*mcp.CallToolResult, any, error) {
	return nil, nil, nil
}

func main() {
	server := mcp.NewServer(&mcp.Implementation{Name: "greeter"}, nil)
	mcp.AddTool(server, &mcp.Tool{Name: "greet", Description: "say hi"}, SayHi)
	server.AddTool(&mcp.Tool{
		Name:        "raw",
		Description: "Raw schema",
		InputSchema: &jsonschema.Schema{
			Type: "object",
			Properties: map[string]*jsonschema.Schema{
				"q": {Type: "string", Description: "Query"},
				"n": {Type: "integer"},
			},
			Required: []string{"q"},
		},
	}, func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		return nil, nil
	})
	mcp.AddTool(server, &mcp.Tool{Name: "inline"}, func(ctx context.Context, req *mcp.CallToolRequest, in struct {
		City string `json:"city"`
	}) (*mcp.CallToolResult, any, error) {
		return nil, nil, nil
	})
	mcp.AddTool(server, &mcp.Tool{Name: name}, SayHi)
	server.AddTool(&mcp.Tool{Name: "opaque", InputSchema: json.RawMessage(`{}`)}, h.Handle)
}
"""


def test_go_sdk_tools(analyze_files) -> None:
    tools = _tools(analyze_files({"main.go": GO_SDK}))
    assert set(tools) == {"greet", "raw", "inline", "opaque"}  # a computed name is not guessed
    greet = tools["greet"]
    assert (greet.framework, greet.description, greet.handler, greet.handlerLine) == (
        "go-mcp",
        "say hi",
        "SayHi",
        20,
    )
    assert greet.startLine == 26 and greet.nodeType == "call_expression"
    assert _params(greet) == [
        ("name", "string", True, None, "the person to greet"),
        ("times", "integer", False, None, None),
        ("tags", "array", False, None, None),
        ("Score", "number", True, None, None),  # no json tag: the field name, required
        ("extra", "object", True, None, None),
        ("when", None, True, None, None),  # a named type: its JSON type is not read
    ]
    raw = tools["raw"]
    assert raw.description == "Raw schema" and raw.handlerLine == 38  # the literal's line
    assert _params(raw) == [
        ("q", "string", True, None, "Query"),
        ("n", "integer", False, None, None),
    ]
    assert _params(tools["inline"]) == [("city", "string", True, None, None)]
    opaque = tools["opaque"]
    assert opaque.params is None and opaque.handler == "h.Handle"


def test_go_sdk_input_struct_the_reader_cannot_follow_leaves_params_unknown(analyze_files) -> None:
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

type Skip struct {
	Secret string `json:"-"`
}
type Hidden struct {
	visible string
}
type Embeds struct {
	Base
	Name string
}

func main() {
	s := mcp.NewServer(nil, nil)
	mcp.AddTool(s, &mcp.Tool{Name: "skip"}, func(ctx context.Context, r *mcp.CallToolRequest, in Skip) (*mcp.CallToolResult, any, error) { return nil, nil, nil })
	mcp.AddTool(s, &mcp.Tool{Name: "hidden"}, func(ctx context.Context, r *mcp.CallToolRequest, in Hidden) (*mcp.CallToolResult, any, error) { return nil, nil, nil })
	mcp.AddTool(s, &mcp.Tool{Name: "embeds"}, func(ctx context.Context, r *mcp.CallToolRequest, in Embeds) (*mcp.CallToolResult, any, error) { return nil, nil, nil })
	mcp.AddTool(s, &mcp.Tool{Name: "elsewhere"}, other.Handle)
	mcp.AddTool(s, &mcp.Tool{Name: "imported"}, func(ctx context.Context, r *mcp.CallToolRequest, in pkg.Args) (*mcp.CallToolResult, any, error) { return nil, nil, nil })
	mcp.AddTool[Skip, any](s, &mcp.Tool{Name: "explicit"}, h)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert set(tools) == {"skip", "hidden", "embeds", "elsewhere", "imported", "explicit"}
    assert all(t.params is None for t in tools.values())


def test_go_sdk_tool_variable_read_only_when_nothing_changes_it(analyze_files) -> None:
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func register(srv *mcp.Server) {
	kept := &mcp.Tool{Name: "kept", Description: "Kept"}
	srv.AddTool(kept, handleKept)

	value := mcp.Tool{Name: "value"}
	srv.AddTool(&value, handleKept)

	renamed := &mcp.Tool{Name: "before"}
	renamed.Name = "after"
	srv.AddTool(renamed, handleKept)

	reassigned := &mcp.Tool{Name: "first"}
	reassigned = &mcp.Tool{Name: "second"}
	srv.AddTool(reassigned, handleKept)

	closure := &mcp.Tool{Name: "closure"}
	func() { closure.Description = "changed" }()
	srv.AddTool(closure, handleKept)
}

func handleKept(ctx context.Context, r *mcp.CallToolRequest) (*mcp.CallToolResult, error) { return nil, nil }
"""
    tools = _tools(analyze_files({"tools.go": src}))
    assert set(tools) == {"kept", "value"}
    assert (tools["kept"].handler, tools["kept"].handlerLine, tools["kept"].params) == (
        "handleKept",
        26,
        None,  # `Server.AddTool` without an input schema: unknown
    )


def test_go_sdk_tool_variable_with_any_other_use_is_not_read(analyze_files) -> None:
    """A tool variable is read only when the function uses it as nothing but the ``AddTool``
    argument: anything that could change it, or let something else change it, drops it."""
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func register(srv *mcp.Server) {
	twice := &mcp.Tool{Name: "twice"}
	srv.AddTool(twice, h)
	mcp.AddTool(srv, twice, g)

	passed := &mcp.Tool{Name: "passed"}
	decorate(passed)
	srv.AddTool(passed, h)

	aliased := &mcp.Tool{Name: "aliased"}
	p := aliased
	p.Name = "renamed"
	srv.AddTool(aliased, h)

	assignedTo := &mcp.Tool{Name: "assignedTo"}
	var q *mcp.Tool
	q = assignedTo
	srv.AddTool(assignedTo, h)

	deref := &mcp.Tool{Name: "deref"}
	*deref = mcp.Tool{Name: "other"}
	srv.AddTool(deref, h)

	read := &mcp.Tool{Name: "read"}
	log.Println(read.Name)
	srv.AddTool(read, h)
}
"""
    tools = [s for s in analyze_files({"tools.go": src}) if s.semanticType == "mcp_tool"]
    assert sorted(t.endpoint for t in tools) == ["twice", "twice"]  # two registrations


def test_go_sdk_add_tool_on_an_object_not_proven_a_server(analyze_files) -> None:
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

var registry = NewRegistry()

func main() {
	registry.AddTool(&mcp.Tool{Name: "registry"}, h)
	other := makeServer()
	other.AddTool(&mcp.Tool{Name: "other"}, h)
}

func shadow(server *mcp.Server) {
	{
		server := NewFake()
		server.AddTool(&mcp.Tool{Name: "shadowed"}, h)
	}
	server.AddTool(&mcp.Tool{Name: "typed"}, h)
}
"""
    assert set(_tools(analyze_files({"main.go": src}))) == {"typed"}


def test_go_sdk_look_alikes_are_not_tools(analyze_files) -> None:
    """Another module whose package is also called ``mcp``, in a file that also imports the SDK
    (so the detector reads it): its ``NewServer`` / ``AddTool`` are not the SDK's."""
    src = """
package main

import (
	"github.com/acme/agents/mcp"
	gomcp "github.com/modelcontextprotocol/go-sdk/mcp"
)

var impl = &gomcp.Implementation{Name: "unused"}

func main() {
	s := mcp.NewServer(nil, nil)
	mcp.AddTool(s, &mcp.Tool{Name: "acme"}, h)
	s.AddTool(&mcp.Tool{Name: "acme2"}, h)
	s.AddTool(&gomcp.Tool{Name: "acme3"}, h)
}
"""
    assert b"github.com/modelcontextprotocol/go-sdk/mcp" in src.encode()  # the detector's gate
    assert _tools(analyze_files({"other.go": src})) == {}


def test_go_sdk_reassigned_server_is_not_proven(analyze_files) -> None:
    """``server = wrap(server)``: the name may now hold anything, so its ``AddTool`` is not the
    SDK's. The generic ``mcp.AddTool`` still is: the compiler makes its first argument a
    ``*mcp.Server``."""
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func main() {
	server := mcp.NewServer(&mcp.Implementation{Name: "x"}, nil)
	server.AddTool(&mcp.Tool{Name: "before"}, h)
	server = wrap(server)
	server.AddTool(&mcp.Tool{Name: "wrapped"}, h)
	mcp.AddTool(server, &mcp.Tool{Name: "generic"}, Typed)
}
"""
    # Any assignment makes the name not only a server in its block, before the call as well.
    assert set(_tools(analyze_files({"main.go": src}))) == {"generic"}


def test_go_sdk_package_level_server_used_from_another_function(analyze_files) -> None:
    """``var server = mcp.NewServer(…)`` at package level is the server in every function of
    the file that does not bind the name itself; reassigning it anywhere unproves it."""
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

var server = mcp.NewServer(&mcp.Implementation{Name: "x"}, nil)

func register() {
	server.AddTool(&mcp.Tool{Name: "pkglevel"}, h)
}

func local() {
	server := NewFake()
	server.AddTool(&mcp.Tool{Name: "shadowed"}, h)
}
"""
    assert set(_tools(analyze_files({"main.go": src}))) == {"pkglevel"}
    reassigned = src + "\nfunc swap() {\n\tserver = wrap(server)\n}\n"
    assert _tools(analyze_files({"main.go": reassigned})) == {}


def test_go_agent_framework_tools_are_not_mcp_tools(analyze_files) -> None:
    """Genkit, langchaingo and Eino tools in a file that also serves the SDK: only the SDK's
    registration is a tool."""
    src = """
package main

import (
	"context"

	"github.com/cloudwego/eino/components/tool/utils"
	"github.com/firebase/genkit/go/genkit"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/tmc/langchaingo/tools"
)

type Calculator struct{}

func (Calculator) Name() string        { return "calculator" }
func (Calculator) Description() string { return "Calculator" }
func (Calculator) Call(ctx context.Context, input string) (string, error) {
	return "", nil
}

var _ tools.Tool = Calculator{}

func main() {
	g := genkit.Init(context.Background())
	genkit.DefineTool(g, "genkitTool", "Genkit", func(ctx context.Context, in string) (string, error) {
		return in, nil
	})
	einoTool, _ := utils.InferTool("einoTool", "Eino", func(ctx context.Context, in string) (string, error) {
		return in, nil
	})
	agentTools := []tools.Tool{Calculator{}, einoTool}
	_ = agentTools

	server := mcp.NewServer(&mcp.Implementation{Name: "x"}, nil)
	server.AddTool(&mcp.Tool{Name: "real"}, h)
}
"""
    assert set(_tools(analyze_files({"main.go": src}))) == {"real"}


def test_mark3labs_file_is_read_once_by_its_own_detector(analyze_files) -> None:
    """A mark3labs/mcp-go server gives only ``mcp-go`` records, one per tool."""
    src = """
package main

import (
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

func main() {
	s := server.NewMCPServer("demo", "1.0.0")
	s.AddTool(mcp.NewTool("hello", mcp.WithDescription("Hi")), helloHandler)
	s.AddTool(mcp.Tool{Name: "struct"}, helloHandler)
}
"""
    tools = [s for s in analyze_files({"main.go": src}) if s.semanticType == "mcp_tool"]
    assert [(t.endpoint, t.framework) for t in tools] == [("hello", "mcp-go")]


def test_go_sdk_handler_line_is_never_a_same_named_method(analyze_files) -> None:
    """``SayHi`` passed by name is the function, not a method ``(g *G) SayHi`` declared after it."""
    src = """
package main

import (
	"context"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func SayHi(ctx context.Context, req *mcp.CallToolRequest, in any) (*mcp.CallToolResult, any, error) {
	return nil, nil, nil
}

func (g *G) SayHi() {}

func main() {
	server := mcp.NewServer(&mcp.Implementation{Name: "x"}, nil)
	mcp.AddTool(server, &mcp.Tool{Name: "greet"}, SayHi)
	server.AddTool(&mcp.Tool{Name: "only_method"}, Lone)
}

func (g *G) Lone() {}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert (tools["greet"].handler, tools["greet"].handlerLine) == ("SayHi", 10)
    assert (tools["only_method"].handler, tools["only_method"].handlerLine) == ("Lone", None)


def test_go_sdk_handler_line_of_a_local_func_literal(analyze_files) -> None:
    """``h := func(…) {…}`` bound once in the function and never reassigned gives the literal's
    line; reassigned, bound twice, or bound to something else (shadowing a top-level function
    of the name) gives none."""
    src = """
package main

import (
	"context"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func shadow(ctx context.Context, req *mcp.CallToolRequest, in any) (*mcp.CallToolResult, any, error) {
	return nil, nil, nil
}

func main() {
	server := mcp.NewServer(&mcp.Implementation{Name: "x"}, nil)
	h := func(ctx context.Context, req *mcp.CallToolRequest, in any) (*mcp.CallToolResult, any, error) {
		return nil, nil, nil
	}
	mcp.AddTool(server, &mcp.Tool{Name: "local"}, h)
	r := func(ctx context.Context, req *mcp.CallToolRequest, in any) (*mcp.CallToolResult, any, error) {
		return nil, nil, nil
	}
	r = pick()
	mcp.AddTool(server, &mcp.Tool{Name: "reassigned"}, r)
	shadow := pick()
	mcp.AddTool(server, &mcp.Tool{Name: "shadowed"}, shadow)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert (tools["local"].handler, tools["local"].handlerLine) == ("h", 16)
    assert (tools["reassigned"].handler, tools["reassigned"].handlerLine) == ("r", None)
    assert (tools["shadowed"].handler, tools["shadowed"].handlerLine) == ("shadow", None)


def test_go_sdk_select_receive_or_type_switch_alias_is_not_the_top_level_function(
    analyze_files,
) -> None:
    """A name bound by a ``select`` receive (``case Handle := <-ch``) or a type-switch alias
    (``switch Handle := x.(type)``) is that local, not the top-level function of the name: no
    line. A literal later assigned by a receive (``case h = <-ch``) loses its line. A name no
    function binds still gives the top-level function's line."""
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func Handle() {}

func a(s *mcp.Server, ch chan func()) {
	select {
	case Handle := <-ch:
		mcp.AddTool(s, &mcp.Tool{Name: "recv"}, Handle)
	}
}

func b(s *mcp.Server, x any) {
	switch Handle := x.(type) {
	case func():
		mcp.AddTool(s, &mcp.Tool{Name: "tswitch"}, Handle)
	}
}

func c(s *mcp.Server, ch chan func()) {
	h := func() {}
	select {
	case h = <-ch:
	}
	mcp.AddTool(s, &mcp.Tool{Name: "recvassign"}, h)
}

func d(s *mcp.Server, ch chan func()) {
	select {
	case other := <-ch:
		_ = other
	}
	mcp.AddTool(s, &mcp.Tool{Name: "top"}, Handle)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert (tools["recv"].handler, tools["recv"].handlerLine) == ("Handle", None)
    assert (tools["tswitch"].handler, tools["tswitch"].handlerLine) == ("Handle", None)
    assert (tools["recvassign"].handler, tools["recvassign"].handlerLine) == ("h", None)
    assert (tools["top"].handler, tools["top"].handlerLine) == ("Handle", 6)
