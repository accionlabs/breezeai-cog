"""Shared by the MCP tool detectors (``parsers/<lang>_mcp``).

Each detector emits one ``mcp_tool`` statement per tool an MCP **server** declares::

    semanticType="mcp_tool"  method="TOOL"  endpoint=<tool name clients call>
    framework=<SDK>  handler / handlerLine  description  params=[ToolParam…]

A tool is emitted only when the server it is registered on is proven (constructed, typed, or
imported from where it is constructed) and its name is a literal: an agent framework's own
``@agent.tool`` or a chat-client ``@Tool`` is not an MCP tool ("wrong is worse than missing").
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from tree_sitter import Node

from ..schemas import ToolParam
from .emits import Emit
from .treesitter import first_line, node_text

SEMANTIC_TYPE = "mcp_tool"
METHOD = "TOOL"
#: A tool's whole ``params`` JSON (names, types, defaults, descriptions) is kept within this many
#: UTF-8 bytes: the backend drops every param of a tool past 16 KB.
MAX_TOOL_PARAMS_JSON = 15 * 1024


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


def param_json_bytes(p: ToolParam) -> int:
    """``p`` as the backend measures it: compact JSON, UTF-8, nulls included."""
    return len(json.dumps(p.model_dump(), ensure_ascii=False, separators=(",", ":")).encode())


def params_json_bytes(params: list[ToolParam]) -> int:
    """``[a,b]``: the items, their commas and the brackets, as the backend's JSON.stringify
    counts them."""
    return sum(param_json_bytes(p) for p in params) + max(len(params) - 1, 0) + 2


def fit_params(params: list[ToolParam] | None) -> list[ToolParam] | None:
    """``params`` within :data:`MAX_TOOL_PARAMS_JSON`: when the list is over it, descriptions
    are kept in param order while they fit, and this one and the rest go without (one overflow
    would drop every param). Names, types and defaults are always kept."""
    if not params or params_json_bytes(params) <= MAX_TOOL_PARAMS_JSON:
        return params
    out = [p.model_copy(update={"description": None}) if p.description else p for p in params]
    size = params_json_bytes(out)
    for i, p in enumerate(params):
        if not p.description:
            continue
        size += param_json_bytes(p) - param_json_bytes(out[i])
        if size > MAX_TOOL_PARAMS_JSON:
            break
        out[i] = p
    return out


def tool_emit(
    node: Node,
    source: bytes,
    *,
    endpoint: str,
    framework: str,
    handler: str | None,
    handler_line: int | None,
    description: str | None,
    params: list[ToolParam] | None,
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
        "params": fit_params(params),
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
