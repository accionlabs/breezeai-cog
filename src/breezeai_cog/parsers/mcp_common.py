"""Shared by the MCP detectors (``parsers/<lang>_mcp``).

Each detector emits one statement per tool, resource or prompt an MCP **server** declares::

    semanticType="mcp_tool"      method="TOOL"      endpoint=<tool name clients call>
    semanticType="mcp_resource"  method="RESOURCE"  endpoint=<URI or URI template>
    semanticType="mcp_prompt"    method="PROMPT"    endpoint=<prompt name>
    framework=<SDK>  handler / handlerLine  description  params=[Parameter…]

and one ``mcp_call`` (outbound) per literal call of a proven MCP **client**::

    semanticType="mcp_call"  method=TOOL | RESOURCE | PROMPT  endpoint=<name or URI>
    framework=<client SDK>

A record is emitted only when the server (or client) is proven (constructed, typed, or
imported from where it is constructed) and its name is a literal: an agent framework's own
``@agent.tool`` or a chat-client ``@Tool`` is not an MCP tool ("wrong is worse than missing").
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

from tree_sitter import Node

from ..schemas import Parameter
from .emits import Emit
from .treesitter import first_line, node_text

SEMANTIC_TYPE = "mcp_tool"
METHOD = "TOOL"
#: ``semanticType`` / ``method`` of a resource, a prompt and a client call.
RESOURCE = ("mcp_resource", "RESOURCE")
PROMPT = ("mcp_prompt", "PROMPT")
TOOL = (SEMANTIC_TYPE, METHOD)
CALL = "mcp_call"
#: A statement's whole ``params`` JSON (names, types, defaults, descriptions) is kept within
#: this many UTF-8 bytes: the backend drops every param of a statement past 16 KB.
MAX_TOOL_PARAMS_JSON = 15 * 1024

#: One RFC 6570 expression of a URI template.
_EXPRESSION = re.compile(r"\{([^{}]*)\}")
#: A simple-expansion expression of one variable (``{id}``, ``{user.id}``): no operator, no
#: explode, no prefix, no second variable.
_SIMPLE_VARIABLE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")


def uri_template_params(uri: str) -> list[Parameter] | None:
    """The variables of a resource URI template as params: ``users://{id}/posts/{post}`` →
    ``id`` and ``post``, required strings. ``[]`` for a literal URI. None (unknown) when any
    expression is not a single simple variable — an RFC 6570 operator (``{?q}``, ``{+p}``,
    ``{#f}``, ``{/s}``, ``{.x}``, ``{;x}``, ``{&x}``), an explode (``{x*}``), a prefix (``{x:3}``),
    several variables (``{x,y}``) — or a brace is unmatched: what such a template accepts is not
    a plain list of required strings."""
    names: list[str] = []
    for m in _EXPRESSION.finditer(uri):
        if not _SIMPLE_VARIABLE.fullmatch(m.group(1)):
            return None
        if m.group(1) not in names:
            names.append(m.group(1))
    rest = _EXPRESSION.sub("", uri)
    if "{" in rest or "}" in rest:
        return None
    return [param(n, "string", required=True) for n in names]


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
    """An ``mcp_tool`` (:func:`mcp_emit` with ``TOOL``)."""
    return mcp_emit(
        node,
        source,
        kind=TOOL,
        endpoint=endpoint,
        framework=framework,
        handler=handler,
        handler_line=handler_line,
        description=description,
        params=params,
        parent=parent,
        node_type=node_type,
        extra=extra,
    )


def mcp_emit(
    node: Node,
    source: bytes,
    *,
    kind: tuple[str, str],
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
    """A server statement: ``kind`` is :data:`TOOL`, :data:`RESOURCE` or :data:`PROMPT`
    (``(semanticType, method)``); ``name`` = ``endpoint``."""
    semantic_type, method = kind
    fields: dict[str, Any] = {
        "semanticType": semantic_type,
        "method": method,
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


def call_emit(
    node: Node,
    source: bytes,
    *,
    method: str,
    endpoint: str,
    framework: str,
) -> Emit:
    """An ``mcp_call`` (outbound): a proven client calls a tool (``TOOL``), reads a resource
    (``RESOURCE``) or gets a prompt (``PROMPT``) by the literal ``endpoint``. It enriches the
    call's own unclassified statement (one statement per call line), which keeps its ``name``,
    else is appended with no ``name`` (as an ``api_call``)."""
    return Emit(
        node,
        {
            "semanticType": CALL,
            "method": method,
            "endpoint": endpoint,
            "framework": framework,
        },
        enrich="unclassified",
        node_type=node.type,
        text=first_line(node_text(node, source)),
    )
