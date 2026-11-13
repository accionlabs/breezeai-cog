"""Additive detector: tools of a Kotlin MCP server (Spring AI, the MCP Java SDK, the official
Kotlin SDK).

::

    @McpTool(name = "weather", description = "Current weather")     → mcp_tool  spring-ai-mcp  TOOL  weather
    fun weather(@McpToolParam(description = "City") city: String): String

    @Tool(description = "Current weather")                          → mcp_tool  (only in an MCP server)
    fun weather(@ToolParam(description = "City") city: String, units: String? = null, ctx: ToolContext)

* The rules of the Java detector (``java_mcp``), in Kotlin syntax: ``@McpTool`` always;
  Spring AI's ``@Tool`` (imported from ``org.springframework.ai.tool.annotation`` or written
  fully qualified) only in a module whose build file declares a Spring AI MCP server starter
  (fact :data:`~..java_mcp.tools.FACT`, registered for Kotlin too: ``pom.xml`` /
  ``build.gradle[.kts]`` in the file's folder or an ancestor).
* ``endpoint`` — ``name =`` when a literal, else the function name (Spring AI's default).
* ``params`` — ``description`` / ``required`` from the parameter annotation, required by
  default as in Java: Spring AI's schema does not read Kotlin nullability (``String?``) or a
  default value. ``default`` is the default's source text (cut at 200 characters).
  ``ToolContext`` and MCP exchange / context parameters are injected, not inputs.

MCP Java SDK (``io.modelcontextprotocol``) called from Kotlin::

    McpServerFeatures.SyncToolSpecification(                        → mcp_tool  mcp-java-sdk  TOOL  calc
        McpSchema.Tool("calc", "Calculator", SCHEMA_JSON)) { exchange, args -> … }
    McpServerFeatures.SyncToolSpecification.builder()               → (Async… and the builders alike)
        .tool(McpSchema.Tool.builder().name("calc").build()).callHandler(::calc).build()

* As in Java: the tool is a ``Tool`` constructor or builder, inline or a ``val`` / ``var``
  declared once in the file; a non-literal name → no record; ``params`` from a JSON-schema
  string literal (``.trimIndent()`` allowed), else None (unknown); a callable reference
  (``::calc``, ``this::calc``) names the handler, a lambda gives only its line.

Official Kotlin SDK (``io.modelcontextprotocol.kotlin.sdk``)::

    server.addTool(name = "calc", description = "Calculator",       → mcp_tool  mcp-kotlin  TOOL  calc
        inputSchema = Tool.Input(properties = buildJsonObject {
            putJsonObject("a") { put("type", "number") } }, required = listOf("a"))) { request -> … }

* Only in a file importing the SDK's server package (``io.modelcontextprotocol.kotlin.sdk.server``):
  an ``addTool`` on anything else is not a tool. ``name`` / ``description`` named or the first
  two positional arguments, or the ``name =`` / ``description =`` of a ``Tool(…)`` passed first.
* ``params`` — none without an ``inputSchema`` (the SDK's empty default); from a
  ``Tool.Input(…)`` / ``ToolSchema(…)`` whose ``properties`` is a ``buildJsonObject { … }`` of
  ``putJsonObject("name") { put("type", …); put("description", …) }`` and whose ``required`` is
  a ``listOf("…")`` literal; any other schema → None (unknown).
* ``addTools(listOf(RegisteredTool(Tool(name = …, …)) { … }, …))`` — one record per
  ``RegisteredTool`` of an inline ``listOf`` / ``mutableListOf`` / ``arrayOf``, read as
  ``addTool(Tool(…))``; a list from a variable or a call → no record.

Import aliases (``import org.springframework.ai.tool.annotation.Tool as AiTool``) are followed
for the types above when the aliased class is in an MCP / Spring AI package; an alias of any
other class (``import com.acme.Tool as Tool``) hides the name instead.
"""

from __future__ import annotations

import re
from typing import Any

from tree_sitter import Node

from ...schemas import ToolParam
from ..additive import (
    DetectContext,
    Detection,
    Detector,
    FactSpec,
    Gate,
    index_fact,
    register_detector,
    register_fact,
)
from ..emits import Emit
from ..java_mcp.tools import (
    _INJECTED,
    FACT,
    FRAMEWORK,
    SDK_FRAMEWORK,
    _collect,
    _in_mcp_module,
    _json_schema_params,
    _resolve,
    _walk,
    concat_strings,
)
from ..mcp_common import param, tool_emit
from ..treesitter import node_text

_MAX_DEFAULT = 200

KOTLIN_SDK_FRAMEWORK = "mcp-kotlin"

# The Spring AI MCP server starter, read from the same build files as for Java.
register_fact(
    FactSpec(
        name=FACT,
        language="kotlin",
        gate=(b"mcp-server",),
        collect=_collect,
        resolve=_resolve,
        sources=(".xml", ".gradle", ".kts"),
        grammar=None,
    )
)

#: Packages whose classes an import alias may stand for (BREEZEAI-1457).
_MCP_PACKAGES = ("org.springframework.ai.", "org.springaicommunity.mcp.", "io.modelcontextprotocol.")
_IMPORT_ALIAS = re.compile(rb"^[ \t]*import[ \t]+([\w.]+)[ \t]+as[ \t]+(\w+)", re.MULTILINE)


def _aliases(source: bytes) -> dict[str, str]:
    """``import a.b.C as D`` → ``{"D": "a.b.C"}``."""
    return {m.group(2).decode(): m.group(1).decode() for m in _IMPORT_ALIAS.finditer(source)}


def _canon(name: str, aliases: dict[str, str]) -> str:
    """The class a simple type name stands for: an alias of an MCP / Spring AI class → that
    class's simple name; an alias of any other class → ``""`` (never a match)."""
    fqcn = aliases.get(name)
    if fqcn is None:
        return name
    return fqcn.rsplit(".", 1)[-1] if fqcn.startswith(_MCP_PACKAGES) else ""


def _type_name(node: Node, source: bytes, aliases: dict[str, str]) -> str:
    """Simple class name of a type reference (``a.b.C`` → ``C``; an alias resolved)."""
    text = node_text(node, source).split("<")[0]
    return text.rsplit(".", 1)[-1] if "." in text else _canon(text, aliases)


def _string(node: Node | None, source: bytes) -> str | None:
    """A Kotlin string literal without templates (``+`` of literals, ``.trimIndent()``
    allowed); escapes kept as written."""
    if node is None:
        return None
    if node.type == "additive_expression":
        return concat_strings(node, source, "additive_expression", _string)
    if node.type == "call_expression":  # """…""".trimIndent()
        callee = node.named_children[0] if node.named_children else None
        if callee is not None and callee.type == "navigation_expression":
            if _suffix_name(callee, source) == "trimIndent" and not _call_args(node, source):
                return _string(callee.named_children[0], source)
        return None
    if node.type != "string_literal":
        return None
    if any(c.type != "string_content" for c in node.named_children):
        return None  # a "$template"
    text = node_text(node, source)
    if text.startswith('"""'):
        return text[3:-3]
    return text[1:-1]


_KOTLIN_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b"}


def _unescape(text: str) -> str | None:
    """Kotlin string escapes → characters; None for a unicode escape (not read)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1 : i + 2]
        if nxt in ('"', "'", "\\", "$"):
            out.append(nxt)
        elif nxt in _KOTLIN_ESCAPES:
            out.append(_KOTLIN_ESCAPES[nxt])
        else:
            return None
        i += 2
    return "".join(out)


def _literal_text(node: Node | None, source: bytes) -> str | None:
    """A string literal's value: escapes decoded in a ``"…"`` string, none in a raw one."""
    raw = _string(node, source)
    if raw is None or node is None:
        return None
    return raw if b'"""' in source[node.start_byte : node.end_byte] else _unescape(raw)


def _suffix_name(nav: Node, source: bytes) -> str:
    """The member a ``navigation_expression`` selects (``a.b.c`` → ``c``)."""
    suffix = nav.named_children[-1] if nav.named_children else None
    if suffix is None or suffix.type != "navigation_suffix":
        return ""
    name = next((c for c in suffix.named_children if c.type == "simple_identifier"), None)
    return node_text(name, source) if name is not None else ""


def _type_callee(call: Node, source: bytes, aliases: dict[str, str]) -> str:
    """``_callee_name`` with a called simple name resolved through the import aliases (a
    constructor call: ``Tool(…)``, ``AliasOfTool(…)``)."""
    callee = call.named_children[0] if call.named_children else None
    if callee is not None and callee.type == "simple_identifier":
        return _canon(node_text(callee, source), aliases)
    return _callee_name(call, source)


def _callee_name(call: Node, source: bytes) -> str:
    """Simple name a ``call_expression`` calls (``McpSchema.Tool(…)`` → ``Tool``)."""
    callee = call.named_children[0] if call.named_children else None
    if callee is None:
        return ""
    if callee.type == "simple_identifier":
        return node_text(callee, source)
    if callee.type == "navigation_expression":
        return _suffix_name(callee, source)
    return ""


def _call_args(call: Node, source: bytes) -> list[tuple[str | None, Node]]:
    """``(name or None, value)`` of a call's arguments, a trailing lambda last (unnamed)."""
    suffix = next((c for c in call.named_children if c.type == "call_suffix"), None)
    out: list[tuple[str | None, Node]] = []
    for part in suffix.named_children if suffix is not None else []:
        if part.type == "value_arguments":
            for arg in part.named_children:
                if arg.type != "value_argument" or not arg.named_children:
                    continue
                named = any(c.type == "=" for c in arg.children)
                key = node_text(arg.named_children[0], source) if named else None
                out.append((key, arg.named_children[-1]))
        elif part.type == "annotated_lambda":
            lam = next((c for c in part.named_children if c.type == "lambda_literal"), None)
            if lam is not None:
                out.append((None, lam))
    return out


def _named(args: list[tuple[str | None, Node]], key: str) -> Node | None:
    return next((v for k, v in args if k == key), None)


def _positional(args: list[tuple[str | None, Node]]) -> list[Node]:
    return [v for k, v in args if k is None]


# ── Spring AI ────────────────────────────────────────────────────────────────


def _ann_parts(ann: Node) -> tuple[Node | None, Node | None]:
    """``(user_type, value_arguments)`` of an annotation (``@A`` / ``@A(…)`` / ``@param:A``)."""
    inv = next((c for c in ann.named_children if c.type == "constructor_invocation"), None)
    if inv is not None:
        type_ = next((c for c in inv.named_children if c.type == "user_type"), None)
        args = next((c for c in inv.named_children if c.type == "value_arguments"), None)
        return type_, args
    return next((c for c in ann.named_children if c.type == "user_type"), None), None


def _annotations(mods: Node | None, source: bytes, aliases: dict[str, str]) -> dict[str, Node]:
    """Annotations in a ``modifiers`` / ``parameter_modifiers`` node, by simple name (an
    import alias resolved)."""
    out: dict[str, Node] = {}
    for a in mods.named_children if mods is not None else []:
        if a.type == "annotation":
            type_, _ = _ann_parts(a)
            if type_ is not None:
                out[_type_name(type_, source, aliases)] = a
    return out


def _element(annotation: Node, key: str, source: bytes) -> Node | None:
    _, args = _ann_parts(annotation)
    for a in args.named_children if args is not None else []:
        if a.type != "value_argument" or not a.named_children:
            continue
        if any(c.type == "=" for c in a.children):
            if node_text(a.named_children[0], source) == key:
                return a.named_children[-1]
        elif key == "value":
            return a.named_children[-1]  # a lone value: @ToolParam("…")
    return None


def _params(
    function: Node, source: bytes, param_annotation: str, aliases: dict[str, str]
) -> list[ToolParam]:
    params = next((c for c in function.named_children if c.type == "function_value_parameters"), None)
    # (parameter, its modifiers, its default): a modifier precedes its parameter, a default
    # follows it, all as siblings.
    entries: list[list[Node | None]] = []
    mods: Node | None = None
    for c in params.named_children if params is not None else []:
        if c.type == "parameter_modifiers":
            mods = c
        elif c.type == "parameter":
            entries.append([c, mods, None])
            mods = None
        elif c.type not in ("line_comment", "multiline_comment") and entries:
            entries[-1][2] = c
    out: list[ToolParam] = []
    for p, p_mods, default in entries:
        assert p is not None
        name_node = next((c for c in p.named_children if c.type == "simple_identifier"), None)
        type_node = next((c for c in p.named_children if c.type != "simple_identifier"), None)
        if name_node is None or type_node is None:
            continue
        type_text = node_text(type_node, source)
        base = type_node.named_children[0] if type_node.type == "nullable_type" else type_node
        if _type_name(base, source, aliases) in _INJECTED:
            continue
        ann = _annotations(p_mods, source, aliases).get(param_annotation)
        description = required = None
        if ann is not None:
            description = _string(
                _element(ann, "description", source) or _element(ann, "value", source), source
            )
            req = _element(ann, "required", source)
            required = node_text(req, source) != "false" if req is not None else None
        # Spring AI reads `required` from the annotation only (true by default, as in Java):
        # Kotlin nullability and a default value do not reach its schema.
        default_text = " ".join(node_text(default, source).split()) if default is not None else None
        if default_text is not None and len(default_text) > _MAX_DEFAULT:
            default_text = default_text[: _MAX_DEFAULT - 1] + "…"
        out.append(
            param(
                node_text(name_node, source),
                type_text,
                required=True if required is None else required,
                default=default_text,
                description=description,
            )
        )
    return out


#: ``@Tool`` is Spring AI's only when imported from its package (BREEZEAI-1457).
_SPRING_TOOL_IMPORT = re.compile(
    rb"import\s+org\.springframework\.ai\.tool\.annotation\.(?:Tool\b(?!\s+as\b)|\*)"
)
_SPRING_TOOL_FQN = "org.springframework.ai.tool.annotation.Tool"


def _spring_tool(ann: Node, source: bytes, imported: bool, aliases: dict[str, str]) -> bool:
    type_, _ = _ann_parts(ann)
    text = node_text(type_, source) if type_ is not None else ""
    if text in aliases:  # import org.springframework.ai.tool.annotation.Tool as AiTool
        return aliases[text] == _SPRING_TOOL_FQN
    return text == _SPRING_TOOL_FQN or (imported and text == "Tool")


def _annotated_tools(dc: DetectContext, functions: dict[tuple[str, int], Any]) -> list[Emit]:
    """Spring AI ``@McpTool`` / ``@Tool`` functions."""
    source = dc.source
    mcp_annotations = b"McpTool" in source
    plain = b"springframework.ai" in source and _in_mcp_module(
        dc.record.path, index_fact(dc.ctx.resolution_index, FACT)
    )
    if not mcp_annotations and not plain:
        return []
    imported = bool(_SPRING_TOOL_IMPORT.search(source))
    aliases = _aliases(source)
    found: list[Emit] = []
    for function in _walk(dc.root):
        if function.type != "function_declaration":
            continue
        mods = next((c for c in function.named_children if c.type == "modifiers"), None)
        anns = _annotations(mods, source, aliases)
        if "McpTool" in anns:
            ann, param_ann = anns["McpTool"], "McpToolParam"
        elif plain and "Tool" in anns and _spring_tool(anns["Tool"], source, imported, aliases):
            ann, param_ann = anns["Tool"], "ToolParam"
        else:
            continue
        name_node = next((c for c in function.named_children if c.type == "simple_identifier"), None)
        if name_node is None:
            continue
        function_name = node_text(name_node, source)
        named = _element(ann, "name", source)
        endpoint = _string(named, source) if named is not None else function_name
        if not endpoint:
            continue
        line = function.start_point[0] + 1
        owner = functions.get((function_name, line))
        found.append(
            tool_emit(
                ann,
                source,
                endpoint=endpoint,
                framework=FRAMEWORK,
                handler=function_name,
                handler_line=line,
                description=_string(_element(ann, "description", source), source),
                params=_params(function, source, param_ann, aliases),
                parent=owner.id if owner is not None else None,
            )
        )
    return found


# ── MCP Java SDK (BREEZEAI-1457) ────────────────────────────────────────────


def _builder_chain(
    node: Node, source: bytes, aliases: dict[str, str]
) -> tuple[str, dict[str, list[Node]]] | None:
    """``X.builder().a(1).b(2).build()`` → ``("X", {"builder": [], "a": [1], …, "build": []})``;
    None when ``node`` is not such a chain ending in ``build()``."""
    calls: list[tuple[str, list[Node]]] = []
    cur: Node | None = node
    while cur is not None and cur.type == "call_expression":
        callee = cur.named_children[0] if cur.named_children else None
        if callee is None or callee.type != "navigation_expression":
            return None
        calls.append((_suffix_name(callee, source), _positional(_call_args(cur, source))))
        cur = callee.named_children[0]
    if cur is None or len(calls) < 2 or calls[-1][0] != "builder" or calls[0][0] != "build":
        return None
    return _type_name(cur, source, aliases), dict(reversed(calls))


def _local_variables(root: Node, source: bytes) -> dict[str, Node | None]:
    """``val`` / ``var`` declared once in the file → their initializer (None when declared
    twice)."""
    out: dict[str, Node | None] = {}
    for node in _walk(root):
        if node.type != "property_declaration":
            continue
        decl = next((c for c in node.named_children if c.type == "variable_declaration"), None)
        name = (
            next((c for c in decl.named_children if c.type == "simple_identifier"), None)
            if decl is not None
            else None
        )
        if name is None:
            continue
        value: Node | None = None
        after_eq = False
        for c in node.children:
            if c.type == "=":
                after_eq = True
            elif after_eq and c.is_named:
                value = c
                break
        key = node_text(name, source)
        out[key] = None if key in out else value
    return out


def _tool_fields(
    node: Node | None, source: bytes, variables: dict[str, Node | None], aliases: dict[str, str]
) -> tuple[str, str | None, list[ToolParam] | None] | None:
    """``(name, description, params)`` of an MCP ``Tool`` expression: ``McpSchema.Tool(…)``,
    ``McpSchema.Tool.builder()…build()``, or a variable assigned one of them once."""
    if node is not None and node.type == "simple_identifier":
        node = variables.get(node_text(node, source))
    if node is None or node.type != "call_expression":
        return None
    chain = _builder_chain(node, source, aliases)
    if chain is None:
        if _type_callee(node, source, aliases) != "Tool":
            return None
        args = _call_args(node, source)
        pos = _positional(args)
        name = _string(_named(args, "name") or (pos[0] if pos else None), source)
        if not name:
            return None
        if _named(args, "name") is not None:
            schema = _named(args, "inputSchema")
            desc = _string(_named(args, "description"), source)
            return name, desc, _json_schema_params(_literal_text(schema, source)) if schema else None
        if len(pos) == 3:  # Tool(name, description, inputSchema)
            return name, _string(pos[1], source), _json_schema_params(_literal_text(pos[2], source))
        return name, None, None  # other overloads: positions differ by SDK version
    root, calls = chain
    if root != "Tool":
        return None
    name_args = calls.get("name") or []
    name = _string(name_args[0], source) if len(name_args) == 1 else None
    if not name:
        return None
    desc_args = calls.get("description") or []
    description = _string(desc_args[0], source) if len(desc_args) == 1 else None
    schema_args = calls.get("inputSchema")
    params = _json_schema_params(_literal_text(schema_args[-1], source)) if schema_args else []
    return name, description, params


def _handler(
    node: Node | None, source: bytes, methods: dict[str, int | None]
) -> tuple[str | None, int | None]:
    """A lambda → its line; a callable reference (``::calc``, ``this::calc``) → its name."""
    if node is None:
        return None, None
    if node.type == "lambda_literal":
        return None, node.start_point[0] + 1
    name = ""
    if node.type == "callable_reference":
        ident = [c for c in node.named_children if c.type == "simple_identifier"]
        name = node_text(ident[-1], source) if ident else ""
    elif node.type == "navigation_expression":
        suffix = node.named_children[-1] if node.named_children else None
        if suffix is not None and node_text(suffix, source).startswith("::"):
            name = _suffix_name(node, source)
    if name:
        return name, methods.get(name)
    return None, None


def _methods(dc: DetectContext) -> dict[str, int | None]:
    lines: dict[str, list[int]] = {}
    for f in dc.record.functions:
        lines.setdefault(f.name, []).append(f.startLine)
    return {name: ls[0] if len(ls) == 1 else None for name, ls in lines.items()}


def _sdk_tools(dc: DetectContext) -> list[Emit]:
    """``SyncToolSpecification`` / ``AsyncToolSpecification`` of the MCP Java SDK."""
    source = dc.source
    if b"io.modelcontextprotocol" not in source or b"ToolSpecification" not in source:
        return []
    variables = _local_variables(dc.root, source)
    methods = _methods(dc)
    aliases = _aliases(source)
    found: list[Emit] = []
    for node in _walk(dc.root):
        if node.type != "call_expression":
            continue
        tool: Node | None = None
        handler: Node | None = None
        chain = _builder_chain(node, source, aliases)
        if chain is not None:
            if not chain[0].endswith("ToolSpecification"):
                continue
            tool_args = chain[1].get("tool") or []
            handler_args = chain[1].get("callHandler") or []
            tool = tool_args[0] if len(tool_args) == 1 else None
            handler = handler_args[0] if len(handler_args) == 1 else None
        elif _type_callee(node, source, aliases).endswith("ToolSpecification"):
            args = _call_args(node, source)
            pos = _positional(args)
            tool = _named(args, "tool") or (pos[0] if pos else None)
            rest = [v for v in pos if v is not tool and v.type != "null_literal"]
            handler = (
                _named(args, "callHandler") or _named(args, "call") or (rest[-1] if rest else None)
            )
            if handler is None:
                continue
        else:
            continue
        fields = _tool_fields(tool, source, variables, aliases)
        if fields is None:
            continue
        name, description, params = fields
        handler_name, handler_line = _handler(handler, source, methods)
        found.append(
            tool_emit(
                node,
                source,
                endpoint=name,
                framework=SDK_FRAMEWORK,
                handler=handler_name,
                handler_line=handler_line,
                description=description,
                params=params,
                node_type=None,
            )
        )
    return found


# ── Official Kotlin SDK (BREEZEAI-1457) ─────────────────────────────────────


def _string_list(node: Node | None, source: bytes) -> list[str] | None:
    """``listOf("a", "b")`` / ``arrayOf(…)`` of literals; None for anything else."""
    if node is None or node.type != "call_expression":
        return None
    if _callee_name(node, source) not in ("listOf", "arrayOf", "setOf"):
        return None
    out = [_string(v, source) for v in _positional(_call_args(node, source))]
    return [v for v in out if v is not None] if all(v is not None for v in out) else None


def _lambda_statements(call: Node, source: bytes) -> list[Node] | None:
    """The statements of a call's trailing lambda (``f { a; b }``); None without one."""
    lam = next((v for k, v in reversed(_call_args(call, source)) if v.type == "lambda_literal"), None)
    if lam is None:
        return None
    body = next((c for c in lam.named_children if c.type == "statements"), None)
    return [
        s
        for s in (body.named_children if body is not None else [])
        if s.type not in ("line_comment", "multiline_comment")
    ]


def _json_properties(node: Node | None, source: bytes) -> dict[str, dict[str, str]] | None:
    """``buildJsonObject { putJsonObject("a") { put("type", "number") } }`` → ``{"a": {"type":
    "number"}}`` (string ``put``s only); None when any statement is something else."""
    if node is None or node.type != "call_expression" or _callee_name(node, source) != "buildJsonObject":
        return None
    statements = _lambda_statements(node, source)
    if statements is None:
        return None
    out: dict[str, dict[str, str]] = {}
    for stmt in statements:
        if stmt.type != "call_expression" or _callee_name(stmt, source) != "putJsonObject":
            return None
        pos = [v for v in _positional(_call_args(stmt, source)) if v.type != "lambda_literal"]
        name = _string(pos[0], source) if len(pos) == 1 else None
        inner = _lambda_statements(stmt, source)
        if not name or inner is None:
            return None
        spec: dict[str, str] = {}
        for put in inner:
            if put.type == "call_expression" and _callee_name(put, source) == "put":
                kv = _positional(_call_args(put, source))
                key = _string(kv[0], source) if len(kv) == 2 else None
                value = _literal_text(kv[1], source) if len(kv) == 2 else None
                if key is not None and value is not None:
                    spec[key] = value
        out[name] = spec
    return out


def _kotlin_schema_params(
    node: Node | None, source: bytes, aliases: dict[str, str]
) -> list[ToolParam] | None:
    """Params of a ``Tool.Input(…)`` / ``ToolSchema(…)``; None when it cannot be read whole."""
    if node is None or node.type != "call_expression":
        return None
    if _type_callee(node, source, aliases) not in ("Input", "ToolSchema"):
        return None
    args = _call_args(node, source)
    pos = _positional(args)
    props_node = _named(args, "properties") or (pos[0] if pos else None)
    required_node = _named(args, "required") or (pos[1] if len(pos) > 1 else None)
    if props_node is None:
        return [] if required_node is None else None
    props = _json_properties(props_node, source)
    required = _string_list(required_node, source) if required_node is not None else []
    if props is None or required is None:
        return None
    return [
        param(name, spec.get("type"), required=name in required, description=spec.get("description"))
        for name, spec in props.items()
    ]


_LIST_LITERALS = ("listOf", "mutableListOf", "arrayOf")


def _kotlin_sdk_tool(
    node: Node,
    fields: list[tuple[str | None, Node]],
    pos: list[Node],
    handler: Node | None,
    source: bytes,
    methods: dict[str, int | None],
    aliases: dict[str, str],
) -> Emit | None:
    """One Kotlin SDK tool: ``fields`` are ``addTool``'s (or its ``Tool(…)``'s) arguments, ``pos``
    the positional ones (``name``, ``description``, ``inputSchema``)."""
    name = _string(_named(fields, "name") or (pos[0] if pos else None), source)
    if not name or handler is None:
        return None
    description = _string(_named(fields, "description") or (pos[1] if len(pos) > 1 else None), source)
    schema = _named(fields, "inputSchema") or (pos[2] if len(pos) > 2 else None)
    params = _kotlin_schema_params(schema, source, aliases) if schema is not None else []
    handler_name, handler_line = _handler(handler, source, methods)
    return tool_emit(
        node,
        source,
        endpoint=name,
        framework=KOTLIN_SDK_FRAMEWORK,
        handler=handler_name,
        handler_line=handler_line,
        description=description,
        params=params,
        node_type=None,
    )


def _registered_tools(node: Node, source: bytes, aliases: dict[str, str]) -> list[tuple[Node, Node, Node | None]]:
    """``(element, Tool(…) call, handler)`` of each ``RegisteredTool(Tool(…), handler)`` in an
    ``addTools(listOf(…))`` whose list is written inline; nothing for any other list."""
    args = _call_args(node, source)
    pos = _positional(args)
    lst = _named(args, "toolsToAdd") or (pos[0] if len(pos) == 1 else None)
    callee = lst.named_children[0] if lst is not None and lst.named_children else None
    if (
        lst is None
        or lst.type != "call_expression"
        or callee is None
        or callee.type != "simple_identifier"
        or node_text(callee, source) not in _LIST_LITERALS
    ):
        return []
    out: list[tuple[Node, Node, Node | None]] = []
    for element in _positional(_call_args(lst, source)):
        if element.type != "call_expression" or _type_callee(element, source, aliases) != "RegisteredTool":
            continue
        el_args = _call_args(element, source)
        el_pos = _positional(el_args)  # a trailing lambda is the last one
        tool = _named(el_args, "tool") or (el_pos[0] if el_pos else None)
        handler = _named(el_args, "handler") or (el_pos[1] if len(el_pos) > 1 else None)
        if tool is not None and tool.type == "call_expression" and _type_callee(tool, source, aliases) == "Tool":
            out.append((element, tool, handler))
    return out


def _kotlin_sdk_tools(dc: DetectContext) -> list[Emit]:
    """``addTool(name, description, inputSchema) { … }`` / ``addTools(listOf(RegisteredTool(…)))``
    of the official Kotlin SDK."""
    source = dc.source
    if b"io.modelcontextprotocol.kotlin.sdk.server" not in source or b"addTool" not in source:
        return []
    methods = _methods(dc)
    aliases = _aliases(source)
    found: list[Emit] = []
    for node in _walk(dc.root):
        if node.type != "call_expression":
            continue
        method = _callee_name(node, source)
        emits: list[Emit | None] = []
        if method == "addTool":
            args = _call_args(node, source)
            pos = [v for v in _positional(args) if v.type != "lambda_literal"]
            lam = next((v for k, v in args if k is None and v.type == "lambda_literal"), None)
            handler = _named(args, "handler") or lam
            fields = args
            first = _named(args, "tool") or (pos[0] if pos else None)
            if (
                first is not None
                and first.type == "call_expression"
                and _type_callee(first, source, aliases) == "Tool"
            ):
                fields, pos = _call_args(first, source), []  # addTool(Tool(name = …, …)) { … }
            emits.append(_kotlin_sdk_tool(node, fields, pos, handler, source, methods, aliases))
        elif method == "addTools":
            for element, tool, handler in _registered_tools(node, source, aliases):
                emits.append(
                    _kotlin_sdk_tool(
                        element, _call_args(tool, source), [], handler, source, methods, aliases
                    )
                )
        found.extend(e for e in emits if e is not None)
    return found


def _detect(dc: DetectContext) -> Detection | None:
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found = _annotated_tools(dc, functions) + _sdk_tools(dc) + _kotlin_sdk_tools(dc)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=found[0].fields.get("framework") or FRAMEWORK)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="kotlin-mcp",
        gate=Gate("kotlin", order=30, guard=b"Tool", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK, SDK_FRAMEWORK, KOTLIN_SDK_FRAMEWORK),
    )
)
