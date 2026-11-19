"""Additive detector: tools of a Python MCP server built with the official SDK (``mcp.server.fastmcp``).

::

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("breeze")

    @mcp.tool(name="Code_Graph_Search")           → mcp_tool  python-mcp  TOOL  Code_Graph_Search
    async def code_graph_search(query: str, limit: int = 25) -> str: …

The ``@<server>.tool`` reader and the server proof are shared with ``python_fastmcp``
(:mod:`..python_mcp_common`, where the rules are); this detector emits the tools of a server
built with the official SDK (``mcp.server.fastmcp``), wherever the tool is declared.

It also emits the tools, resources and prompts of a low-level server
(``mcp.server.lowlevel.Server``): each ``Tool(…)`` its ``@<server>.list_tools()`` function returns,
and the same for ``list_resources`` / ``list_resource_templates`` / ``list_prompts``
(:mod:`.lowlevel`, where the rules are). Resources and prompts declared with ``@<server>.resource``
/ ``@<server>.prompt`` are read like tools (:mod:`..python_mcp_common`).
"""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..python_mcp_common import PYTHON_MCP, detect_decorated
from .lowlevel import detect_lowlevel


def _detect(dc: DetectContext) -> Detection | None:
    found = detect_decorated(dc, PYTHON_MCP)
    lowlevel = detect_lowlevel(dc)
    if not lowlevel:
        return found
    emits = (found.emits if found is not None else []) + lowlevel
    emits.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(emits, label=PYTHON_MCP)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="python-mcp",
        gate=Gate("python", order=60, skip_fixtures=True),
        detect=_detect,
        frameworks=(PYTHON_MCP,),
    )
)
