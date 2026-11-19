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
    ("python_mcp.tools", "detect_lowlevel", "srv.py", PYTHON, {"search", "lowlevel"}),
    (
        "typescript_mcp.tools",
        "detect_lowlevel",
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


@pytest.mark.parametrize(
    ("module", "reader", "path", "src", "everything"),
    [
        (
            "java_mcp.tools",
            "_sdk_entries",
            "src/main/java/demo/App.java",
            JAVA,
            {"search", "shortcut"},
        ),
        (
            "kotlin_mcp.tools",
            "_entries",
            "src/main/kotlin/demo/App.kt",
            KOTLIN,
            {"search", "listed", "shortcut"},
        ),
    ],
)
def test_a_failing_resource_and_prompt_reader_keeps_every_tool(
    analyze_files, monkeypatch, module, reader, path, src, everything
) -> None:
    """The resource / prompt readers (BREEZEAI-1584 PR 2) run guarded too: forced to fail, the
    file's tools all stay."""

    def boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(f"breezeai_cog.parsers.{module}.{reader}", boom)
    with capture_logs() as logs:
        assert _tool_names(analyze_files({path: src})) == everything
    assert [e for e in logs if e["event"] == "mcp.subreader.failed"]
    assert not [e for e in logs if e["event"] == "additive.detector.failed"]
