"""Additive detector: tools of a Python MCP server built with the standalone ``fastmcp`` package.

::

    from fastmcp import FastMCP
    mcp = FastMCP("breeze")

    @mcp.tool(name="Code_Graph_Search")           → mcp_tool  fastmcp  TOOL  Code_Graph_Search
    async def code_graph_search(query: str, limit: int = 25) -> str: …

The ``@<server>.tool`` reader and the server proof are shared with ``python_mcp``
(:mod:`..python_mcp_common`, where the rules are); this detector emits the tools of a server
built with the standalone ``fastmcp`` package, wherever the tool is declared.
"""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..python_mcp_common import FASTMCP, detect_tools


def _detect(dc: DetectContext) -> Detection | None:
    return detect_tools(dc, FASTMCP)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="python-fastmcp",
        gate=Gate("python", order=50, skip_fixtures=True),
        detect=_detect,
        frameworks=(FASTMCP,),
    )
)
