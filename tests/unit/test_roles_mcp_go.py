"""MCP server tools of ``mark3labs/mcp-go``: one ``mcp_tool`` statement per tool.

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


GO_SERVER = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func main() {
    s := server.NewMCPServer("demo", "1.0.0")
    tool := mcp.NewTool("hello",
        mcp.WithDescription("Say hello"),
        mcp.WithString("name", mcp.Required(), mcp.Description("Who to greet")),
        mcp.WithNumber("times", mcp.DefaultNumber(1)),
    )
    s.AddTool(tool, helloHandler)
    s.AddTool(mcp.NewTool("ping"), func(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
        return nil, nil
    })
}

func helloHandler(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
    return nil, nil
}
"""


def test_go_mcp_tools(analyze_files) -> None:
    tools = _tools(analyze_files({"main.go": GO_SERVER}))
    hello = tools["hello"]
    assert (hello.framework, hello.description, hello.handler) == (
        "mcp-go",
        "Say hello",
        "helloHandler",
    )
    assert hello.handlerLine == 22
    assert _params(hello) == [
        ("name", "string", True, None, "Who to greet"),
        ("times", "number", False, "1", None),
    ]
    ping = tools["ping"]
    assert ping.description is None and ping.params == [] and ping.handlerLine == 17


def test_go_reused_tool_variable_keeps_each_handler(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

var shared = mcp.NewTool("shared")

func regA(s *Server) {
    tool := mcp.NewTool("a")
    s.AddTool(tool, handleA)
}

func regB(s *Server) {
    tool := mcp.NewTool("b")
    s.AddTool(tool, handleB)
    tool = mcp.NewTool("c")
    s.AddTool(tool, handleC)
    s.AddTool(shared, handleShared)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert {k: t.handler for k, t in tools.items()} == {
        "a": "handleA",
        "b": "handleB",
        "c": "handleC",
        "shared": "handleShared",
    }


def test_go_typed_tool_handler_is_looked_through(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func main() {
    s := server.NewMCPServer("demo", "1.0.0")
    s.AddTool(mcp.NewTool("typed"), mcp.NewTypedToolHandler(typedHandler))
    s.AddTool(mcp.NewTool("other"), server.NewTypedToolHandler(typedHandler))
}

func typedHandler(ctx context.Context, r mcp.CallToolRequest, args Args) (*mcp.CallToolResult, error) {
    return nil, nil
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    for name in ("typed", "other"):
        assert (tools[name].handler, tools[name].handlerLine) == ("typedHandler", 15), name


def test_go_options_that_cannot_be_read_leave_params_unknown(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

func reg(s *Server) {
    s.AddTool(mcp.NewTool("helper", mcp.WithString("q"), commonOpts()), h)
    s.AddTool(mcp.NewTool("raw", mcp.WithRawInputSchema(schema)), h)
    s.AddTool(mcp.NewTool("known", mcp.WithString("q"), mcp.WithReadOnlyHintAnnotation(true)), h)
    s.AddTool(mcp.NewTool("titled", mcp.WithTitleAnnotation("T"), mcp.WithOutputSchema[Out]()), h)
    s.AddTool(mcp.NewTool("newer", mcp.WithString("q"), mcp.WithStringEnum("mode", "a", "b")), h)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert tools["helper"].params is None and tools["raw"].params is None
    assert _params(tools["known"]) == [("q", "string", False, None, None)]
    # An option the detector does not know may add a parameter: unknown, not a partial list.
    assert tools["newer"].params is None
    assert tools["titled"].params == []


def test_go_package_tool_var_is_not_paired_through_a_shadowing_local(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

var tool = mcp.NewTool("pkg_level")

func other(s *Server) {
    tool := mcp.NewTool("other")
    s.AddTool(tool, otherHandler)
}

func pkg(s *Server) {
    s.AddTool(tool, pkgHandler)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert tools["other"].handler == "otherHandler"
    assert tools["pkg_level"].handler == "pkgHandler"


def test_go_package_tool_var_registered_twice_has_no_handler(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

var tool = mcp.NewTool("pkg_level")

func a(s *Server) { s.AddTool(tool, aHandler) }

func b(s *Server) { s.AddTool(tool, bHandler) }
"""
    assert _tools(analyze_files({"main.go": src}))["pkg_level"].handler is None


def test_go_reassigned_tool_var_stops_at_the_next_assignment(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func reg(s *server.MCPServer) {
    t := mcp.NewTool("first")
    s.AddTools(server.ServerTool{Tool: t, Handler: firstHandler})
    t = mcp.NewTool("second")
    s.AddTool(t, secondHandler)
    u := mcp.NewTool("unregistered")
    u = mcp.NewTool("third")
    s.AddTool(u, thirdHandler)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert {k: t.handler for k, t in tools.items()} == {
        "first": "firstHandler",
        "second": "secondHandler",
        "unregistered": None,
        "third": "thirdHandler",
    }


def test_go_server_tool_literal_gives_the_handler(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    srv "github.com/mark3labs/mcp-go/server"
)

func reg(s *srv.MCPServer) {
    tools := []srv.ServerTool{
        {Tool: mcp.NewTool("listed"), Handler: listedHandler},
    }
    s.AddTools(
        srv.ServerTool{Tool: mcp.NewTool("inline"), Handler: inlineHandler},
        &srv.ServerTool{Tool: mcp.NewTool("pointer"), Handler: pointerHandler},
    )
    s.AddTools(tools...)
}

func inlineHandler(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
    return nil, nil
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert (tools["inline"].handler, tools["inline"].handlerLine) == ("inlineHandler", 20)
    assert tools["pointer"].handler == "pointerHandler"
    assert tools["listed"].handler == "listedHandler"  # element of a []ServerTool literal
