"""A sub-reader added inside an existing MCP detector (a low-level server, a builder shortcut, an
``addTools`` list read from a ``val``) never drops the tools the detector's other readers found
in the file: forced to fail, it costs only its own records, and the failure is logged."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

PYTHON = """
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
import mcp.types as types

mcp = FastMCP("demo")
server = Server("low")


@mcp.tool()
def search(query: str) -> str:
    return query


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(name="lowlevel", inputSchema={})]
"""

TYPESCRIPT = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

const mcp = new McpServer({ name: "demo", version: "1" });
mcp.tool("search", async () => ({ content: [] }));

const server = new Server({ name: "low", version: "1" }, { capabilities: { tools: {} } });
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{ name: "lowlevel", inputSchema: { type: "object", properties: {} } }],
}));
"""

JAVA = """
package demo;

import io.modelcontextprotocol.server.McpServer;
import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;

public class App {
    McpServerFeatures.SyncToolSpecification search() {
        return new McpServerFeatures.SyncToolSpecification(
            new McpSchema.Tool("search", "Search", "{}"), (exchange, args) -> null);
    }

    void start(Object transport) {
        McpServer.sync(transport).tool(new McpSchema.Tool("shortcut", "S", "{}"), (e, a) -> null).build();
    }
}
"""

KOTLIN = """
package demo

import io.modelcontextprotocol.kotlin.sdk.RegisteredTool
import io.modelcontextprotocol.kotlin.sdk.Tool
import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.server.McpServer
import io.modelcontextprotocol.spec.McpSchema

val listed = listOf(RegisteredTool(Tool(name = "listed")) { _ -> null })

fun Server.register(transport: Any) {
    addTool("search", "Search") { _ -> null }
    addTools(listed)
    McpServer.sync(transport).tool(McpSchema.Tool("shortcut", "S", "{}")) { _, _ -> null }.build()
}
"""

CASES = [
    ("python_mcp.tools", "detect_lowlevel_tools", "srv.py", PYTHON, {"search", "lowlevel"}),
    (
        "typescript_mcp.tools",
        "detect_lowlevel_tools",
        "src/index.ts",
        TYPESCRIPT,
        {"search", "lowlevel"},
    ),
    (
        "java_mcp.tools",
        "_shortcut_tools",
        "src/main/java/demo/App.java",
        JAVA,
        {"search", "shortcut"},
    ),
    (
        "kotlin_mcp.tools",
        "_shortcut_tools",
        "src/main/kotlin/demo/App.kt",
        KOTLIN,
        {"search", "listed", "shortcut"},
    ),
    (
        "kotlin_mcp.tools",
        "_registered_tools",
        "src/main/kotlin/demo/App.kt",
        KOTLIN,
        {"search", "listed", "shortcut"},
    ),
]


def _tool_names(statements) -> set[str | None]:
    return {s.endpoint for s in statements if s.semanticType == "mcp_tool"}


@pytest.mark.parametrize(("module", "reader", "path", "src", "everything"), CASES)
def test_a_failing_subreader_keeps_the_detectors_other_tools(
    analyze_files, monkeypatch, module, reader, path, src, everything
) -> None:
    assert _tool_names(analyze_files({path: src})) == everything

    def boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(f"breezeai_cog.parsers.{module}.{reader}", boom)
    with capture_logs() as logs:
        kept = _tool_names(analyze_files({path: src}))
    assert "search" in kept and kept < everything  # only the failing reader's records go
    failed = [e for e in logs if e["event"] == "mcp.subreader.failed"]
    assert failed and failed[0]["reader"] == "boom" and failed[0]["error"] == "ValueError: boom"
    assert not [e for e in logs if e["event"] == "additive.detector.failed"]


def test_a_parse_timeout_in_a_subreader_is_not_swallowed() -> None:
    """``guarded`` turns a sub-reader's failure into a warning, but not a ``ParserTimeout``: the
    file's parse budget is spent, and the file is handled like any parse timeout. Nothing
    raises it today; the re-raise keeps one raised later from becoming a partial record set."""
    from breezeai_cog.errors import ParserTimeout
    from breezeai_cog.parsers.mcp_common import guarded

    def timeout(*args):
        raise ParserTimeout("x.ts")

    with capture_logs() as logs, pytest.raises(ParserTimeout):
        guarded("x.ts", timeout, 1)
    assert not [e for e in logs if e["event"] == "mcp.subreader.failed"]
