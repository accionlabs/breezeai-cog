"""Additive detector: tools of a Python MCP server (FastMCP 2/3, ``mcp.server.fastmcp``).

::

    mcp = FastMCP("breeze")                       # or: def register(mcp: FastMCP): …
                                                  # or: from .server import mcp
    @mcp.tool(name="Code_Graph_Search")           → mcp_tool  fastmcp  TOOL  Code_Graph_Search
    @handle_errors("Code Graph API")                              handler code_graph_search
    async def code_graph_search(query: str, limit: int = 25) -> str:
        \"\"\"Semantic search over the code graph.\"\"\"   description, params

* **Server receiver** — the decorator's object must be proven a server: a name assigned
  ``FastMCP(…)`` in this file, a parameter annotated ``FastMCP``, or a name imported from a
  module that assigns one (fact :mod:`.servers`). ``@agent.tool`` (pydantic-ai) and any other
  ``.tool`` decorator are left alone.
* ``endpoint`` — ``name=`` (or the first positional string), else the function name; a
  non-literal name → no record.
* ``description`` — ``description=`` when a literal, else the docstring (FastMCP's rule);
  a non-literal ``description=`` → none.
* ``params`` — from the signature: annotation text, default, ``Annotated[T, Field(description=…)]``
  / ``Annotated[T, "…"]`` and ``= Field(…)`` descriptions, else the param's entry in the
  docstring's Google ``Args:`` / NumPy ``Parameters`` / Sphinx ``:param`` section (following
  FastMCP 4 (griffe) for the common forms, a little more lenient on Google headers: the first
  style with an entry wins, a later entry of a name replaces an earlier one; only exact param
  names match; each entry cut at 1024 characters, added while the tool's whole ``params`` JSON
  stays within 15 KB; read even when
  ``description=`` replaces the docstring); ``self`` /
  ``cls``, ``*args`` / ``**kwargs``, the injected ``Context`` (also ``Context | None`` / ``Optional[Context]``) and
  the names in a literal ``exclude_args=[…]`` are skipped (a non-literal ``exclude_args`` →
  params unknown). Required = no default.
* ``@x.tool(enabled=False)`` (a literal) → no record: the tool is never served.
* ``framework`` — ``fastmcp`` when the server's file imports the standalone ``fastmcp``
  package, else ``mcp-python`` (the official SDK's ``mcp.server.fastmcp``); for an imported
  server, the file that builds it decides.
"""

from __future__ import annotations

import ast
import inspect
import json
import re

from tree_sitter import Node

from ...logging import get_logger
from ...schemas import ToolParam
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import clean_doc, param, tool_emit
from ..treesitter import node_text
from .servers import (
    mcp_servers,
    sdk_framework,
    server_assignments,
    server_class_names,
)

_MAX_DEFAULT = 200
#: A param description read from the docstring is cut here, so one runaway entry cannot push
#: the tool's ``params`` past the backend's size limit (which drops them all).
_MAX_PARAM_DOC = 1024
#: Docstring-sourced descriptions are added only while a tool's whole ``params`` JSON (names,
#: types, defaults and ``Field`` descriptions included) stays within this many UTF-8 bytes: the
#: backend drops every param of a tool past 16 KB, so a docstring must never cost the ones the
#: code declares.
_MAX_TOOL_PARAMS_JSON = 15 * 1024
#: ``:param x:`` and the field names griffe reads the same way.
_SPHINX_PARAM_FIELDS = frozenset({"param", "parameter", "arg", "argument", "keyword"})
_GOOGLE_PARAM_SECTIONS = frozenset(
    {
        "args",
        "arguments",
        "parameters",
        "params",
        "keyword args",
        "keyword arguments",
        "other parameters",
    }
)
#: ``@x.tool`` — a cheap byte check before any tree walk (``.tools`` / ``.toolbar`` do not match).
_TOOL_DECORATOR = re.compile(rb"@[ \t]*\w+[ \t]*\.[ \t]*tool\b")
_SKIPPED_PARAMS = frozenset({"self", "cls"})


def _literal(node: Node | None, source: bytes) -> str | None:
    """The value of a plain string literal (implicit concatenation allowed; no f-strings)."""
    if node is None or node.type not in (
        "string",
        "concatenated_string",
        "parenthesized_expression",
    ):
        return None
    try:
        value = ast.literal_eval(node_text(node, source))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, str) else None


def _kwarg(call: Node, name: str, source: bytes) -> Node | None:
    args = call.child_by_field_name("arguments")
    for a in args.named_children if args is not None else []:
        key = a.child_by_field_name("name") if a.type == "keyword_argument" else None
        if key is not None and node_text(key, source) == name:
            return a.child_by_field_name("value")
    return None


def _positional(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return [
        a
        for a in (args.named_children if args is not None else [])
        if a.type not in ("keyword_argument", "comment", "list_splat", "dictionary_splat")
    ]


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def _is_server_type(node: Node | None, source: bytes, classes: set[str]) -> bool:
    return node is not None and node_text(node, source).strip("\"' ") in classes


def _typed_server_params(root: Node, source: bytes, classes: set[str]) -> set[str]:
    out: set[str] = set()
    if not classes:
        return out
    for node in _walk(root):
        if node.type in ("typed_parameter", "typed_default_parameter") and _is_server_type(
            node.child_by_field_name("type"), source, classes
        ):
            name = node.child_by_field_name("name") or (
                node.named_children[0] if node.named_children else None
            )
            if name is not None and name.type == "identifier":
                out.add(node_text(name, source))
    return out


def _import_aliases(root: Node, source: bytes) -> dict[str, str]:
    """``from m import mcp as server`` → ``{"server": "mcp"}``."""
    out: dict[str, str] = {}
    for node in _walk(root):
        if node.type != "import_from_statement":
            continue
        for child in node.named_children:
            if child.type == "aliased_import":
                name, alias = child.child_by_field_name("name"), child.child_by_field_name("alias")
                if name is not None and alias is not None:
                    out[node_text(alias, source)] = node_text(name, source).rsplit(".", 1)[-1]
    return out


def _tool_decorator(decorator: Node, source: bytes) -> tuple[str, Node | None] | None:
    """``@x.tool`` / ``@x.tool(…)`` → ``(x, call or None)``."""
    expr = decorator.named_children[0] if decorator.named_children else None
    call = None
    if expr is not None and expr.type == "call":
        call, expr = expr, expr.child_by_field_name("function")
    if expr is None or expr.type != "attribute":
        return None
    obj, attr = expr.child_by_field_name("object"), expr.child_by_field_name("attribute")
    if obj is None or attr is None or obj.type != "identifier" or node_text(attr, source) != "tool":
        return None
    return node_text(obj, source), call


def _docstring(fn: Node, source: bytes) -> str | None:
    body = fn.child_by_field_name("body")
    first = body.named_children[0] if body is not None and body.named_children else None
    if first is not None and first.type == "expression_statement":  # grammar versions differ
        first = first.named_children[0] if first.named_children else None
    return _literal(first, source)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _cap_doc(text: str) -> str:
    return text if len(text) <= _MAX_PARAM_DOC else text[: _MAX_PARAM_DOC - 1] + "…"


#: One docstring entry: the names it documents (``[]`` when invalid) and its description lines.
_Item = tuple[list[str], list[str]]


def _collect(items: list[_Item], out: dict[str, str]) -> None:
    """``[(names, description lines)]`` → ``out[name] = description``; a later entry of a name
    replaces an earlier one, as FastMCP's dict does (an empty description leaves none)."""
    for names, lines in items:
        text = "\n".join(lines).strip()
        for name in names:
            if text:
                out[name] = _cap_doc(text)
            else:
                out.pop(name, None)


def _google_entry(text: str) -> tuple[str | None, str]:
    """``name: desc`` / ``name (type): desc`` → ``(name, desc)``; ``(None, "")`` otherwise."""
    colon = text.find(":")
    paren = text.find("(")
    if 0 <= paren < colon:  # `name (type): desc` — the type may hold a colon
        close = text.find(")", paren)
        if close < 0 or not text[close + 1 :].lstrip().startswith(":"):
            return None, ""
        name, desc = text[:paren].strip(), text[close + 1 :].lstrip()[1:]
    elif colon >= 0:
        name, desc = text[:colon].strip(), text[colon + 1 :]
    else:
        return None, ""
    return (name, desc.strip()) if name.isidentifier() else (None, "")


def _google_params(lines: list[str]) -> tuple[bool, dict[str, str]]:
    """Google style: an ``Args:`` (…) header, then entries at the first entry's indent; a deeper
    line continues the entry, a line at or left of the header's indent ends the section. Found
    when a section has a ``name: desc``-shaped entry, even one naming no identifier
    (``limit / page: …``)."""
    out: dict[str, str] = {}
    found = False
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not (stripped.endswith(":") and stripped[:-1].strip().lower() in _GOOGLE_PARAM_SECTIONS):
            i += 1
            continue
        header = _indent(lines[i])
        entry = -1
        items: list[_Item] = []
        i += 1
        while i < n:
            line = lines[i]
            if not line.strip():
                if items:
                    items[-1][1].append("")
                i += 1
                continue
            indent = _indent(line)
            if indent <= header:
                break
            if entry < 0:
                entry = indent
            if indent <= entry:  # a new item, valid or not: never glued onto the previous one
                name, desc = _google_entry(line.strip())
                found = found or ":" in line
                items.append(([name] if name else [], [desc]))
            else:
                items[-1][1].append(line.strip())
            i += 1
        _collect(items, out)
    return found, out


def _is_underline(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.strip("-")


def _numpy_params(lines: list[str]) -> tuple[bool, dict[str, str]]:
    """NumPy style: ``Parameters`` over a ``----------`` underline, then ``name : type`` /
    ``x, y : type`` entries at the header's indent with deeper description lines, up to the next
    header (a line at the header's indent, underlined at that indent)."""
    out: dict[str, str] = {}
    found = False
    i, n = 0, len(lines)
    while i < n:
        if not (
            lines[i].strip().lower() in ("parameters", "other parameters")
            and i + 1 < n
            and _is_underline(lines[i + 1])
        ):
            i += 1
            continue
        header = _indent(lines[i])
        items: list[_Item] = []
        i += 2
        while i < n:
            line = lines[i]
            if not line.strip():
                if items:
                    items[-1][1].append("")
                i += 1
                continue
            indent = _indent(line)
            if indent < header or (
                indent == header
                and i + 1 < n
                and _is_underline(lines[i + 1])
                and _indent(lines[i + 1]) == header
            ):
                break  # dedent, or the next section's header (a deeper `---` is description)
            if indent == header:
                names = [x.strip() for x in line.split(":", 1)[0].split(",")]
                found = True
                items.append(([x for x in names if x.isidentifier()], []))
            elif items:
                items[-1][1].append(line.strip())
            i += 1
        _collect(items, out)
    return found, out


def _sphinx_params(lines: list[str]) -> tuple[bool, dict[str, str]]:
    """Sphinx style: ``:param name: desc`` / ``:param type name: desc`` (also ``:parameter``,
    ``:arg``, ``:argument``, ``:keyword``), deeper lines continue."""
    out: dict[str, str] = {}
    items: list[_Item] = []
    current = -1  # the indent of the open ``:param`` item, -1 when none is open
    for line in lines:
        stripped = line.strip()
        if current >= 0 and (not stripped or _indent(line) > current):
            items[-1][1].append(stripped)
            continue
        current = -1
        rest = stripped[1:] if stripped.startswith(":") else ""
        cut = min((k for k in (rest.find(" "), rest.find(":")) if k >= 0), default=-1)
        if cut < 0 or rest[:cut] not in _SPHINX_PARAM_FIELDS:
            continue
        head, sep, desc = rest[cut:].partition(":")
        words = head.split()
        name = words[-1] if sep and words and head[:1].isspace() else None
        items.append(([name] if name and name.isidentifier() else [], [desc.strip()]))
        current = _indent(line)
    _collect(items, out)
    return bool(items), out


def _docstring_param_docs(doc: str | None) -> dict[str, str]:
    """Param descriptions from a docstring's Google ``Args:``, NumPy ``Parameters`` or Sphinx
    ``:param`` entries, tried in that order — the first style with an entry wins, following
    FastMCP 4 (griffe) for the common forms. A line scanner (cost linear in the docstring);
    ``{}`` on anything it cannot read."""
    if not doc:
        return {}
    try:
        lines = inspect.cleandoc(doc).splitlines()
        for style in (_google_params, _numpy_params, _sphinx_params):
            found, docs = style(lines)
            if found:
                return docs
    except Exception as exc:  # a docstring never costs the file its tools
        get_logger("breezeai_cog.parsers").debug("python_mcp.docstring.skipped", error=str(exc))
    return {}


def _split_annotated(type_text: str) -> str:
    """``Annotated[str, Field(...)]`` → ``str``."""
    inner = type_text.strip()
    if not inner.startswith("Annotated[") or not inner.endswith("]"):
        return inner
    inner = inner[len("Annotated[") : -1]
    depth = 0
    for i, ch in enumerate(inner):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return inner[:i].strip()
    return inner.strip()


def _top_level_split(text: str, sep: str) -> list[str]:
    """``text`` split on ``sep`` outside brackets."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == sep and depth == 0:
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return [part.strip() for part in out]


def _is_context(type_text: str) -> bool:
    """True for FastMCP's injected ``Context``, optionally nullable: ``Context``,
    ``mcp.server.fastmcp.Context``, ``Context[S, L]``, ``Context | None``, ``Optional[Context]``,
    ``Union[Context, None]``."""
    text = type_text.strip()
    members = _top_level_split(text, "|")
    if len(members) == 1:
        head, _, rest = text.partition("[")
        generic = head.strip().rsplit(".", 1)[-1]
        if generic in ("Optional", "Union") and rest.endswith("]"):
            members = _top_level_split(rest[:-1], ",")
    named = [m for m in members if m not in ("None", "NoneType")]
    if len(named) != 1:
        return False
    return named[0].split("[")[0].strip().rsplit(".", 1)[-1] == "Context"


def _field_call(node: Node | None, source: bytes) -> Node | None:
    if node is None or node.type != "call":
        return None
    fn = node.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    return node if callee == "Field" or callee.endswith(".Field") else None


def _annotated_metadata(type_node: Node, source: bytes) -> list[Node]:
    """The metadata arguments of ``Annotated[T, m1, m2]`` → ``[m1, m2]`` (top level only: a
    string inside ``T``, as in ``Literal["asc", "desc"]``, is not metadata); ``[]`` for any
    other type."""
    generic = type_node.named_children[0] if type_node.named_children else None
    if generic is None or generic.type != "generic_type" or not generic.named_children:
        return []
    if node_text(generic.named_children[0], source).rsplit(".", 1)[-1] != "Annotated":
        return []
    params = next((c for c in generic.named_children if c.type == "type_parameter"), None)
    args = [c for c in (params.named_children if params is not None else []) if c.type != "comment"]
    return [a.named_children[0] if a.type == "type" and a.named_children else a for a in args[1:]]


def _annotated_description(metadata: list[Node], source: bytes) -> str | None:
    """``Annotated[T, Field(description=…)]`` or ``Annotated[T, "…"]`` → the description."""
    for m in metadata:
        field = _field_call(m, source)
        if field is not None:
            text = _literal(_kwarg(field, "description", source), source)
            if text:
                return text
    return next((t for t in (_literal(m, source) for m in metadata) if t), None)


def _field_default(field: Node, source: bytes) -> tuple[bool, Node | None]:
    """``Field(…)`` → ``(has_default, default node)``. ``Field(...)`` / ``Field(default=...)``
    mark the field required; ``default_factory=`` gives a default with no
    literal value."""
    given = _kwarg(field, "default", source)
    if given is None:
        pos = _positional(field)
        given = pos[0] if pos else None
    if given is not None and given.type != "ellipsis" and node_text(given, source) != "...":
        return True, given
    return _kwarg(field, "default_factory", source) is not None, None


def _short(node: Node, source: bytes) -> str:
    text = " ".join(node_text(node, source).split())
    return text if len(text) <= _MAX_DEFAULT else text[: _MAX_DEFAULT - 1] + "…"


def _params(fn: Node, source: bytes, doc_params: dict[str, str]) -> list[ToolParam]:
    """The signature's params; a param's description is its ``Field`` / ``Annotated`` one, else
    its entry in ``doc_params`` (the docstring's), added in signature order while the whole list
    stays within ``_MAX_TOOL_PARAMS_JSON``."""
    params = fn.child_by_field_name("parameters")
    out: list[ToolParam] = []
    from_doc: dict[int, str] = {}  # index in ``out`` → its docstring description
    for p in params.named_children if params is not None else []:
        name: Node | None
        type_node: Node | None
        value: Node | None
        if p.type == "identifier":
            name, type_node, value = p, None, None
        elif p.type in ("typed_parameter", "typed_default_parameter", "default_parameter"):
            name = p.child_by_field_name("name") or (
                p.named_children[0] if p.named_children else None
            )
            type_node, value = p.child_by_field_name("type"), p.child_by_field_name("value")
        else:  # *args / **kwargs / separators
            continue
        if name is None or name.type != "identifier":
            continue
        pname = node_text(name, source)
        type_text = node_text(type_node, source) if type_node is not None else None
        base = _split_annotated(type_text) if type_text else None
        if pname in _SKIPPED_PARAMS or (base and _is_context(base)):
            continue
        metadata = _annotated_metadata(type_node, source) if type_node is not None else []
        description = _annotated_description(metadata, source)
        default: str | None = None
        required = value is None
        # `= Field(…)` decides the default; without `= value`, a `Field(…)` in `Annotated`
        # does (`x: Annotated[int, Field(default=5)]` is optional).
        field = _field_call(value, source) if value is not None else None
        if value is None:
            field = next((f for f in (_field_call(m, source) for m in metadata) if f), None)
        if field is not None:
            if value is not None:
                description = _literal(_kwarg(field, "description", source), source) or description
            has_default, given = _field_default(field, source)
            required = not has_default
            default = _short(given, source) if given is not None else None
        elif value is not None:
            default = _short(value, source)
        doc = None if description else doc_params.get(pname)
        if doc is not None:
            from_doc[len(out)] = doc
        out.append(param(pname, base, required=required, default=default, description=description))
    # `[a,b]`: the items, their commas and the brackets, as the backend's JSON.stringify counts
    size = sum(_json_bytes(p) for p in out) + max(len(out) - 1, 0) + 2
    for i, doc in from_doc.items():
        with_doc = out[i].model_copy(update={"description": clean_doc(doc)})
        size += _json_bytes(with_doc) - _json_bytes(out[i])
        if size > _MAX_TOOL_PARAMS_JSON:
            break  # this one and the rest stay without: one overflow would drop them all
        out[i] = with_doc
    return out


def _json_bytes(p: ToolParam) -> int:
    """``p`` as the backend measures it: compact JSON, UTF-8, nulls included."""
    return len(json.dumps(p.model_dump(), ensure_ascii=False, separators=(",", ":")).encode())


def _disabled(call: Node, source: bytes) -> bool:
    """``@x.tool(enabled=False)`` with a literal ``False``."""
    value = _kwarg(call, "enabled", source)
    return value is not None and node_text(value, source) == "False"


def _excluded_args(call: Node, source: bytes) -> set[str] | None:
    """The names in a literal ``exclude_args=[…]`` (FastMCP hides them from the schema);
    ``set()`` when absent, None when it is not a list / tuple of string literals."""
    value = _kwarg(call, "exclude_args", source)
    if value is None:
        return set()
    if value.type not in ("list", "tuple"):
        return None
    names = [_literal(item, source) for item in value.named_children if item.type != "comment"]
    return None if any(n is None for n in names) else {n for n in names if n is not None}


def _detect(dc: DetectContext) -> Detection | None:
    source, record = dc.source, dc.record
    if not _TOOL_DECORATOR.search(source):
        return None
    classes = server_class_names(dc.root, source)
    servers = server_assignments(
        dc.root, source, classes, module_only=False
    ) | _typed_server_params(dc.root, source, classes)
    imported = mcp_servers(dc.ctx.resolution_index)
    bindings = dc.bindings or {}
    aliases = _import_aliases(dc.root, source)

    local_framework = sdk_framework(source)

    def server_framework(name: str) -> str | None:
        """The SDK framework of the server ``name`` refers to, or None when it is not one."""
        if name in servers:
            return local_framework
        target = bindings.get(name)
        names, framework = imported.get(target, (frozenset(), "")) if target else (frozenset(), "")
        return framework if aliases.get(name, name) in names else None

    functions = {(f.name, f.startLine): f for f in record.functions}
    found: list[Emit] = []
    for node in _walk(dc.root):
        if node.type != "decorated_definition":
            continue
        fn = node.child_by_field_name("definition")
        if fn is None or fn.type != "function_definition":
            continue
        for decorator in (c for c in node.named_children if c.type == "decorator"):
            hit = _tool_decorator(decorator, source)
            framework = server_framework(hit[0]) if hit is not None else None
            if hit is None or framework is None:
                continue
            call = hit[1]
            fn_name = node_text(fn.child_by_field_name("name"), source)  # type: ignore[arg-type]
            endpoint: str | None = fn_name
            docstring = _docstring(fn, source)
            description: str | None = docstring
            if call is not None:
                name_node = _kwarg(call, "name", source)
                pos = _positional(call)
                if name_node is None and pos and pos[0].type in ("string", "concatenated_string"):
                    name_node = pos[0]
                if name_node is not None:
                    endpoint = _literal(name_node, source)
                desc_node = _kwarg(call, "description", source)
                if desc_node is not None:
                    description = _literal(desc_node, source)
            if not endpoint:
                continue  # a computed name: the tool exists, but its name is unknown
            if call is not None and _disabled(call, source):
                break  # `enabled=False`: registered but never served
            # the docstring itself, not `description` (which `description=` replaces)
            doc_params = _docstring_param_docs(docstring)
            params: list[ToolParam] | None = _params(fn, source, doc_params)
            excluded = _excluded_args(call, source) if call is not None else set()
            if excluded is None:
                params = None  # `exclude_args=` not a literal: which params remain is unknown
            elif excluded and params is not None:
                params = [p for p in params if p.name not in excluded]
            line = fn.start_point[0] + 1
            owner = functions.get((fn_name, line))
            found.append(
                tool_emit(
                    decorator,
                    source,
                    endpoint=endpoint,
                    framework=framework,
                    handler=fn_name,
                    handler_line=line,
                    description=description,
                    params=params,
                    parent=owner.id if owner is not None else None,
                )
            )
            break
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=found[0].fields.get("framework") or local_framework)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="python-mcp",
        gate=Gate("python", order=50, skip_fixtures=True),
        detect=_detect,
        frameworks=("fastmcp", "mcp-python"),
    )
)
