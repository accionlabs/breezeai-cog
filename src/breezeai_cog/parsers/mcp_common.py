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

import functools
import inspect
import json
import re
from collections.abc import Callable
from typing import Any, TypeVar

from tree_sitter import Node

from ..errors import ParserTimeout
from ..schemas import FileRecord, Parameter
from .csharp.mappings import CONTROL_FLOW as _CSHARP_CONTROL_FLOW
from .emits import Emit
from .go.mappings import CONTROL_FLOW as _GO_CONTROL_FLOW
from .java.mappings import CONTROL_FLOW as _JAVA_CONTROL_FLOW
from .kotlin.mappings import CONTROL_FLOW as _KOTLIN_CONTROL_FLOW
from .python.mappings import CONTROL_FLOW as _PYTHON_CONTROL_FLOW
from .treesitter import first_line, node_text
from .typescript.mappings import CONTROL_FLOW as _TYPESCRIPT_CONTROL_FLOW

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
    each property's ``type`` and ``description``). A missing ``properties`` reads as no params
    and a missing ``required`` as none required; None (unknown) when the schema is not a dict,
    ``properties`` is not an object or ``required`` is not a list. Shared by every reader of a
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


def function_lines(record: FileRecord) -> dict[str, int]:
    """Name → declaration line of the file's top-level functions, for a handler passed by name.
    A method (a Go receiver method, a class method) is not a top-level function and never lends
    its line; a name two top-level functions share is left out (which one is meant is unknown)."""
    lines: dict[str, int] = {}
    shared: set[str] = set()
    for f in record.functions:
        if f.type == "method" or f.parentId != record.id:
            continue
        if f.name in lines:
            shared.add(f.name)
        lines[f.name] = f.startLine
    return {name: line for name, line in lines.items() if name not in shared}


def guarded(path: str, reader: Callable[..., list[_T]], *args: Any) -> list[_T]:
    """``reader(*args)``, or nothing when it fails: a sub-reader added to a detector never drops
    the records the detector's other readers found in the file (the runner would drop them all)."""
    try:
        return reader(*args)
    except ParserTimeout:
        # Nothing raises it today (a tree-sitter timeout surfaces where the file is parsed);
        # kept so one raised later is handled like any parse timeout, never logged as a
        # sub-reader failure while the file keeps a partial set of records.
        raise
    except Exception as exc:
        from ..logging import get_logger

        get_logger("breezeai_cog.additive").warning(
            "mcp.subreader.failed",
            path=path,
            reader=getattr(reader, "__name__", repr(reader)),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        return []


def guarded_one(path: str, reader: Callable[..., _T], *args: Any) -> _T | None:
    """``reader(*args)`` — a reader of one record (or None) — under :func:`guarded`: None when
    it fails. For a resource or prompt read inside a tool detector's loop."""

    @functools.wraps(reader)
    def one(*a: Any) -> list[_T]:
        return [reader(*a)]

    found = guarded(path, one, *args)
    return found[0] if found else None


def prompt_params(params: list[Parameter] | None) -> list[Parameter] | None:
    """A prompt's arguments: name, required and description only. An MCP prompt argument is a
    string with no type or default (``PromptArgument``), whatever the signature declares."""
    if params is None:
        return None
    return [param(p.name, None, required=p.required, description=p.description) for p in params]


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


#: The statements an ``mcp_call`` never enriches, in every language a client is read in: an
#: ``if`` / ``for`` / ``with`` / ``try`` … spans its body, so a call in its header is a record of
#: its own (docs/architecture/statement-model.md).
_CONTROL_FLOW = frozenset(
    _CSHARP_CONTROL_FLOW
    | _GO_CONTROL_FLOW
    | _JAVA_CONTROL_FLOW
    | _KOTLIN_CONTROL_FLOW
    | _PYTHON_CONTROL_FLOW
    | _TYPESCRIPT_CONTROL_FLOW
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
    innermost captured statement containing the call when that one is unclassified and starts
    on the call's line (``x = 1; await s.call_tool("t")`` → the call's statement, a one-line
    ``with`` / ``catch`` → its body's), which keeps its ``name``; a call
    inside a statement that starts on an earlier line (an argument of a multi-line
    ``gather(…)``, the second line of an ``if`` condition) or in a control-flow header (``if
    (await s.call_tool("x")).isError:``) is appended on its own line with no ``name`` (as an
    ``api_call``)."""
    return Emit(
        node,
        {
            "semanticType": CALL,
            "method": method,
            "endpoint": endpoint,
            "framework": framework,
        },
        enrich="starts",
        skip=_CONTROL_FLOW,
        node_type=node.type,
        text=first_line(node_text(node, source)),
    )
