"""Shared by the MCP tool detectors (``parsers/<lang>_mcp``).

Each detector emits one ``mcp_tool`` statement per tool an MCP **server** declares::

    semanticType="mcp_tool"  method="TOOL"  endpoint=<tool name clients call>
    framework=<SDK>  handler / handlerLine  description  params=[Parameter…]

A tool is emitted only when the server it is registered on is proven (constructed, typed, or
imported from where it is constructed) and its name is a literal: an agent framework's own
``@agent.tool`` or a chat-client ``@Tool`` is not an MCP tool ("wrong is worse than missing").
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any, TypeVar

from tree_sitter import Node

from ..errors import ParserTimeout
from ..schemas import Parameter
from .emits import Emit
from .treesitter import first_line, node_text

SEMANTIC_TYPE = "mcp_tool"
METHOD = "TOOL"
#: A tool's whole ``params`` JSON (names, types, defaults, descriptions) is kept within this many
#: UTF-8 bytes: the backend drops every param of a tool past 16 KB.
MAX_TOOL_PARAMS_JSON = 15 * 1024

_T = TypeVar("_T")


def param(
    name: str,
    type_: str | None = None,
    *,
    required: bool | None,
    default: str | None = None,
    description: str | None = None,
) -> Parameter:
    return Parameter(
        name=name,
        type=type_ or None,
        required=required,
        default=default,
        description=clean_doc(description),
    )


def schema_params(schema: object) -> list[Parameter] | None:
    """Params of a JSON schema already read into Python values (``properties`` / ``required``,
    each property's ``type`` and ``description``); None when it is not an object schema with a
    ``properties`` object and a ``required`` list (unknown). Shared by every reader of a
    JSON-schema literal: a JSON string (Java / Kotlin), a Python dict, a Go schema literal."""
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(props, dict) or not isinstance(required, list):
        return None
    out = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        type_ = spec.get("type")
        desc = spec.get("description")
        out.append(
            param(
                str(name),
                type_ if isinstance(type_, str) else None,
                required=name in required,
                description=desc if isinstance(desc, str) else None,
            )
        )
    return out


def guarded(path: str, reader: Callable[..., list[_T]], *args: Any) -> list[_T]:
    """``reader(*args)``, or nothing when it fails: a sub-reader added to a detector never drops
    the records the detector's other readers found in the file (the runner would drop them all)."""
    try:
        return reader(*args)
    except ParserTimeout:
        raise  # the file's parse budget is spent: handled like any parse timeout
    except Exception as exc:
        from ..logging import get_logger

        get_logger("breezeai_cog.additive").warning(
            "mcp.subreader.failed",
            path=path,
            reader=getattr(reader, "__name__", repr(reader)),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        return []


def clean_doc(text: str | None) -> str | None:
    """A docstring / description with common indentation removed; None when empty."""
    if text is None:
        return None
    cleaned = inspect.cleandoc(text).strip()
    return cleaned or None


def param_json_bytes(p: Parameter) -> int:
    """Size of ``p`` as compact UTF-8 JSON with its unset fields written as ``null``: never
    less than what the backend measures (the capture leaves unset fields out)."""
    return len(json.dumps(p.model_dump(), ensure_ascii=False, separators=(",", ":")).encode())


def params_json_bytes(params: list[Parameter]) -> int:
    """``[a,b]``: the items, their commas and the brackets, as the backend's JSON.stringify
    counts them."""
    return sum(param_json_bytes(p) for p in params) + max(len(params) - 1, 0) + 2


def fit_params(params: list[Parameter] | None) -> list[Parameter] | None:
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
    params: list[Parameter] | None,
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
