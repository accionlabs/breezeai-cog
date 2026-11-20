"""Additive detector: tools of a Kotlin MCP server built on Spring AI.

::

    @McpTool(name = "weather", description = "Current weather")     → mcp_tool  spring-ai-mcp  TOOL  weather
    fun weather(@McpToolParam(description = "City") city: String): String

    @Tool(description = "Current weather")                          → mcp_tool  (only in an MCP server)
    fun weather(@ToolParam(description = "City") city: String, units: String? = null, ctx: ToolContext)

* The rules of the Java detector (``java_springai``), in Kotlin syntax: ``@McpTool`` always;
  Spring AI's ``@Tool`` (imported from ``org.springframework.ai.tool.annotation`` or written
  fully qualified) only in a module whose build file declares a Spring AI MCP server starter
  (:func:`~..jvm_mcp_common.in_mcp_server_module`).
* ``endpoint`` — ``name =`` when a literal, else the function name (Spring AI's default).
* ``params`` — ``description`` / ``required`` from the parameter annotation, required by
  default as in Java: Spring AI's schema does not read Kotlin nullability (``String?``) or a
  default value. ``default`` is the default's source text (cut at 200 characters).
  ``ToolContext`` and MCP exchange / context parameters are injected, not inputs.
* An import alias (``import org.springframework.ai.tool.annotation.Tool as AiTool``) is
  followed when the aliased class is in an MCP / Spring AI package; an alias of any other class
  (``import com.acme.Tool as Tool``) hides the name instead.

``@McpResource`` / ``@McpPrompt`` (with ``@McpArg`` parameters) are read by the Java detector's
rules (``java_springai``): ``uri =`` a literal (params: the template's simple ``{variables}``);
``name =`` else the function name, the parameters as the prompt's arguments (None when an
``@McpArg`` ``name =`` / ``required =`` is not a literal). The annotation must be the
annotations library's: imported from its package (by name, ``*`` or an alias) or written fully
qualified.

Only annotated functions are read here. A tool registered through the MCP Java SDK or the
official Kotlin SDK belongs to ``kotlin_mcp``, also in a Spring application.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..jvm_mcp_common import (
    INJECTED,
    MCP_ANNOTATION_PACKAGES,
    SPRING_AI,
    SPRING_TOOL_FQN,
    in_mcp_server_module,
    kotlin_aliases,
    kotlin_string,
    kotlin_type_name,
    walk,
)
from ..mcp_common import (
    PROMPT,
    RESOURCE,
    guarded,
    mcp_emit,
    param,
    tool_emit,
    uri_template_params,
)
from ..treesitter import node_text

_MAX_DEFAULT = 200


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
                out[kotlin_type_name(type_, source, aliases)] = a
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
) -> list[Parameter]:
    params = next(
        (c for c in function.named_children if c.type == "function_value_parameters"), None
    )
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
    out: list[Parameter] = []
    for p, p_mods, default in entries:
        assert p is not None
        name_node = next((c for c in p.named_children if c.type == "simple_identifier"), None)
        type_node = next((c for c in p.named_children if c.type != "simple_identifier"), None)
        if name_node is None or type_node is None:
            continue
        type_text = node_text(type_node, source)
        base = type_node.named_children[0] if type_node.type == "nullable_type" else type_node
        if kotlin_type_name(base, source, aliases) in INJECTED:
            continue
        ann = _annotations(p_mods, source, aliases).get(param_annotation)
        description = required = None
        if ann is not None:
            description = kotlin_string(
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


#: ``@Tool`` is Spring AI's only when imported from its package.
_SPRING_TOOL_IMPORT = re.compile(
    rb"import\s+org\.springframework\.ai\.tool\.annotation\.(?:Tool\b(?!\s+as\b)|\*)"
)


def _spring_tool(ann: Node, source: bytes, imported: bool, aliases: dict[str, str]) -> bool:
    type_, _ = _ann_parts(ann)
    text = node_text(type_, source) if type_ is not None else ""
    if text in aliases:  # import org.springframework.ai.tool.annotation.Tool as AiTool
        return aliases[text] == SPRING_TOOL_FQN
    return text == SPRING_TOOL_FQN or (imported and text == "Tool")


#: Parameter types the annotations library does not list as prompt arguments; any ``…Map``
#: (``MutableMap``, ``TreeMap``, ``ConcurrentHashMap``, …) is skipped too: it receives the
#: arguments map.
_PROMPT_SKIPPED = INJECTED | {"GetPromptRequest"}


def _prompt_skipped(name: str) -> bool:
    simple = name.rsplit(".", 1)[-1]
    return simple in _PROMPT_SKIPPED or simple.endswith("Map")


def _prompt_params(
    function: Node, source: bytes, aliases: dict[str, str]
) -> list[Parameter] | None:
    """The prompt's arguments; None when an ``@McpArg`` ``name`` / ``required`` is not a
    literal (a constant): the argument list is then unknown."""
    params = next(
        (c for c in function.named_children if c.type == "function_value_parameters"), None
    )
    out: list[Parameter] = []
    mods: Node | None = None
    for c in params.named_children if params is not None else []:
        if c.type == "parameter_modifiers":
            mods = c
            continue
        if c.type != "parameter":
            continue
        p_mods, mods = mods, None
        name_node = next((x for x in c.named_children if x.type == "simple_identifier"), None)
        type_node = next((x for x in c.named_children if x.type != "simple_identifier"), None)
        if name_node is None or type_node is None:
            continue
        base = type_node.named_children[0] if type_node.type == "nullable_type" else type_node
        if _prompt_skipped(kotlin_type_name(base, source, aliases)):
            continue
        name = node_text(name_node, source)
        ann = _annotations(p_mods, source, aliases).get("McpArg")
        description, required = None, False
        if ann is not None:
            named = _element(ann, "name", source)
            if named is not None:
                literal = kotlin_string(named, source)
                if literal is None:
                    return None  # @McpArg(name = ARG): which name, unknown
                name = literal or name  # name = "" → the parameter's name
            description = kotlin_string(_element(ann, "description", source), source)
            req = _element(ann, "required", source)
            req_text = node_text(req, source) if req is not None else "false"
            if req_text not in ("true", "false"):
                return None  # required = FLAG
            required = req_text == "true"
        out.append(param(name, None, required=required, description=description))
    return out


def _annotation_import(simple: str) -> re.Pattern[bytes]:
    packages = b"|".join(re.escape(p.encode()) for p in MCP_ANNOTATION_PACKAGES)
    return re.compile(
        rb"^[ \t]*import[ \t]+(?:"
        + packages
        + rb")\.(?:"
        + simple.encode()
        + rb"\b(?![ \t]+as\b)|\*)",
        re.MULTILINE,
    )


_ENTRY_IMPORTS = {key: _annotation_import(key) for key in ("McpResource", "McpPrompt")}


def _library_annotation(
    ann: Node, key: str, source: bytes, imported: bool, aliases: dict[str, str]
) -> bool:
    """``ann`` is the annotations library's ``key``: imported from its package (by name, ``*`` or
    an alias), or written fully qualified."""
    type_, _ = _ann_parts(ann)
    text = node_text(type_, source) if type_ is not None else ""
    fqns = {f"{p}.{key}" for p in MCP_ANNOTATION_PACKAGES}
    if text in aliases:  # import org.springaicommunity.mcp.annotation.McpPrompt as Prompt
        return aliases[text] in fqns
    return (imported and text == key) or text in fqns


def _annotated_entries(dc: DetectContext) -> list[Emit]:
    """Spring AI ``@McpResource`` / ``@McpPrompt`` functions."""
    source = dc.source
    if b"McpResource" not in source and b"McpPrompt" not in source:
        return []
    aliases = kotlin_aliases(source)
    imported = {key: bool(rx.search(source)) for key, rx in _ENTRY_IMPORTS.items()}
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found: list[Emit] = []
    for function in walk(dc.root):
        if function.type != "function_declaration":
            continue
        mods = next((c for c in function.named_children if c.type == "modifiers"), None)
        anns = _annotations(mods, source, aliases)
        name_node = next(
            (c for c in function.named_children if c.type == "simple_identifier"), None
        )
        if name_node is None:
            continue
        function_name = node_text(name_node, source)
        line = function.start_point[0] + 1
        owner = functions.get((function_name, line))
        for key, kind in (("McpResource", RESOURCE), ("McpPrompt", PROMPT)):
            ann = anns.get(key)
            if ann is None or not _library_annotation(ann, key, source, imported[key], aliases):
                continue
            if kind == RESOURCE:
                endpoint = kotlin_string(_element(ann, "uri", source), source)
                params = uri_template_params(endpoint) if endpoint else None
            else:
                named = _element(ann, "name", source)
                endpoint = kotlin_string(named, source) if named is not None else function_name
                params = _prompt_params(function, source, aliases)
            if not endpoint:
                continue
            found.append(
                mcp_emit(
                    ann,
                    source,
                    kind=kind,
                    endpoint=endpoint,
                    framework=SPRING_AI,
                    handler=function_name,
                    handler_line=line,
                    description=kotlin_string(_element(ann, "description", source), source),
                    params=params,
                    parent=owner.id if owner is not None else None,
                )
            )
    return found


def _annotated_tools(dc: DetectContext) -> list[Emit]:
    """Spring AI ``@McpTool`` / ``@Tool`` functions."""
    source = dc.source
    if b"Tool" not in source:
        return []
    mcp_annotations = b"McpTool" in source
    plain = b"springframework.ai" in source and in_mcp_server_module(
        dc.record.path, dc.ctx.resolution_index
    )
    if not mcp_annotations and not plain:
        return []
    imported = bool(_SPRING_TOOL_IMPORT.search(source))
    aliases = kotlin_aliases(source)
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found: list[Emit] = []
    for function in walk(dc.root):
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
        name_node = next(
            (c for c in function.named_children if c.type == "simple_identifier"), None
        )
        if name_node is None:
            continue
        function_name = node_text(name_node, source)
        named = _element(ann, "name", source)
        endpoint = kotlin_string(named, source) if named is not None else function_name
        if not endpoint:
            continue
        line = function.start_point[0] + 1
        owner = functions.get((function_name, line))
        found.append(
            tool_emit(
                ann,
                source,
                endpoint=endpoint,
                framework=SPRING_AI,
                handler=function_name,
                handler_line=line,
                description=kotlin_string(_element(ann, "description", source), source),
                params=_params(function, source, param_ann, aliases),
                parent=owner.id if owner is not None else None,
            )
        )
    return found


def _detect(dc: DetectContext) -> Detection | None:
    # A failing resource / prompt reader never drops the file's tools.
    found = _annotated_tools(dc) + guarded(dc.path, _annotated_entries, dc)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=SPRING_AI)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="kotlin-springai",
        # `Tool`, `McpResource` or `McpPrompt`: each reader checks its own marker.
        gate=Gate("kotlin", order=30, skip_fixtures=True),
        detect=_detect,
        frameworks=(SPRING_AI,),
    )
)
