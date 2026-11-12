"""MCP servers in Python (FastMCP, ``mcp.server.fastmcp``). A registered additive detector
(``tools.py``) and the server instances other files import, a registered fact
(``servers.py``); the package owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
