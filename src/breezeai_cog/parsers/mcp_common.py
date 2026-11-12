"""Shared by the MCP tool detectors (``parsers/<lang>_mcp``, BREEZEAI-1457).

Each detector emits one ``mcp_tool`` statement per tool an MCP **server** declares::

    semanticType="mcp_tool"  method="TOOL"  endpoint=<tool name clients call>
    framework=<SDK>  handler / handlerLine  description  params=[ToolParam…]

A tool is emitted only when the server it is registered on is proven (constructed, typed, or
imported from where it is constructed) and its name is a literal: an agent framework's own
``@agent.tool`` or a chat-client ``@Tool`` is not an MCP tool ("wrong is worse than missing").
"""

from __future__ import annotations

import inspect
from typing import Any

from tree_sitter import Node

from ..schemas import ToolParam
from .emits import Emit
from .treesitter import first_line, node_text

SEMANTIC_TYPE = "mcp_tool"
METHOD = "TOOL"


def param(
    name: str,
    type_: str | None = None,
    *,
    required: bool = True,
    default: str | None = None,
    description: str | None = None,
) -> ToolParam:
    return ToolParam(
        name=name,
        type=type_ or None,
        required=required,
        default=default,
        description=clean_doc(description),
    )


def clean_doc(text: str | None) -> str | None:
    """A docstring / description with common indentation removed; None when empty."""
    if text is None:
        return None
    cleaned = inspect.cleandoc(text).strip()
    return cleaned or None


def tool_emit(
    node: Node,
    source: bytes,
    *,
    endpoint: str,
    framework: str,
    handler: str | None,
    handler_line: int | None,
    description: str | None,
    params: list[ToolParam],
    parent: str | None = None,
    node_type: str | None = "synthetic",
    extra: dict[str, Any] | None = None,
) -> Emit:
    fields: dict[str, Any] = {
        "semanticType": SEMANTIC_TYPE,
        "method": METHOD,
        "endpoint": endpoint,
        "framework": framework,
        "handler": handler,
        "handlerLine": handler_line,
        "description": clean_doc(description),
        "params": params,
        "name": endpoint,
    }
    if extra:
        fields.update(extra)
    return Emit(
        node,
        fields,
        parent=parent,
        node_type=node_type,
        text=first_line(node_text(node, source)),
    )
