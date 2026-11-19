"""VB.NET method (Sub/Function) / constructor + parameter + attribute + call extraction.

VB nests modifiers under a ``modifiers`` wrapper, types under an ``as_clause``, and
attributes under ``attribute_block`` nodes; calls use ``invocation`` / ``member_access``.

The grammar's type argument list is ``Of T`` without its parentheses, so in ``List(Of Customer)``
the ``(`` is an ``ERROR`` inside the type and the ``)`` an ``ERROR`` right after it: a type's text
is read by :func:`type_text`, which takes those closing parentheses back from the source.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .source import generic_group_end, shadow
from .statements import extract_statements

#: A type argument list right after a type node (``(Of …)``, or a further ``.Inner(Of …)``
#: segment of a qualified type); the ``(`` is group 1.
_BLANKED_GROUP = re.compile(rb"[ \t]*(?:\.[\w\[\]]+[ \t]*)?(\()(?=[ \t]*Of\b)", re.IGNORECASE)

_VISIBILITY = {"Public": "public", "Private": "private", "Protected": "protected", "Friend": "internal"}


def flags(node: Node, source: bytes) -> tuple[str, bool]:
    """Visibility + is_shared from the ``modifiers`` wrapper (default ``public`` —
    VB members are Public unless stated). ``Shared`` == C#'s static."""
    visibility, is_static = "public", False
    modifiers = node.child_by_field_name("modifiers") or next(
        (c for c in node.named_children if c.type == "modifiers"), None)
    if modifiers is not None:
        for child in modifiers.children:
            m = node_text(child, source)
            if m in _VISIBILITY:
                visibility = _VISIBILITY[m]
            elif m == "Shared":
                is_static = True
    return visibility, is_static


def _attribute(node: Node, source: bytes) -> Decorator:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source).rsplit(".", 1)[-1] if name_node is not None else ""
    args: list[str] = []
    arglist = next((c for c in node.named_children if c.type == "argument_list"), None)
    if arglist is not None:
        for arg in arglist.named_children:
            if arg.type == "argument":
                args.append(_attribute_arg(arg, source))
    return Decorator(name=name, args=args)


#: A whole VB string literal (``""`` is an escaped quote).
_VB_STRING = re.compile(r'^"((?:[^"]|"")*)"$', re.DOTALL)


def _attribute_arg(arg: Node, source: bytes) -> str:
    """One attribute argument as the C# reader keeps it: a named argument as written
    (``Name:="GetOrder"``), a positional string literal unquoted (``{id}``), anything else
    (``"a" & "b"``, ``Order.Last``) as written."""
    text = node_text(arg, source).strip()
    first = arg.named_children[0] if arg.named_children else None
    if first is not None and first.type == "identifier":  # ``Key:=value``
        return text
    m = _VB_STRING.match(text)
    return m.group(1).replace('""', '"') if m else text


def attributes_from_blocks(blocks: list[Node], source: bytes) -> list[Decorator]:
    out: list[Decorator] = []
    for block in blocks:
        out.extend(_attribute(a, source) for a in block.named_children if a.type == "attribute")
    return out


def extract_attributes(node: Node, source: bytes) -> list[Decorator]:
    """Attributes on a declaration — its ``attributes`` (attribute_block) field/children."""
    blocks = [c for c in node.named_children if c.type == "attribute_block"]
    return attributes_from_blocks(blocks, source)


def _is_sub(node: Node, source: bytes) -> bool:
    """Whether a method is a ``Sub``, which returns nothing — whatever the grammar put in its
    ``return_type`` when a parameter list misparses (a type after a generic parameter)."""
    name = node.child_by_field_name("name")
    head = source[node.start_byte:name.start_byte] if name is not None else b""
    return re.search(rb"(?i)\bSub[ \t]+$", head) is not None


def type_text(node: Node, source: bytes) -> str | None:
    """A type node's text with the closing parentheses its generic arguments lost to the grammar
    (``Task(Of List(Of Customer``, then ``))`` in the source → ``Task(Of List(Of Customer))``);
    only ``)`` directly after the node (spaces between) are taken, as many as are missing. A type
    argument list the shadow blanked (``Dictionary``, then ``(Of String, Integer)`` in the source;
    see :func:`~.source.blank_generic_arguments`) is taken back whole. None when they are not
    there: a partial type is not a type."""
    end = node.end_byte
    missing = node_text(node, source).count("(") - node_text(node, source).count(")")
    while missing > 0:
        j = end
        while j < len(source) and source[j] in b" \t":
            j += 1
        if source[j:j + 1] != b")":
            return None
        end, missing = j + 1, missing - 1
    if missing != 0:
        return None
    while (m := _BLANKED_GROUP.match(source, end)) is not None:
        group_end = generic_group_end(source, m.start(1))
        if group_end is None:
            return None
        end = group_end
    return source[node.start_byte:end].decode("utf-8", "replace")


def as_clause_type(container: Node, source: bytes) -> str | None:
    """The declared type of a node's ``As <type>`` clause (param/field), else None."""
    as_clause = next((c for c in container.named_children if c.type == "as_clause"), None)
    if as_clause is not None:
        tnode = as_clause.child_by_field_name("type") or next(
            (c for c in as_clause.named_children if c.type in ("type", "primitive_type")), None)
        if tnode is not None:
            return type_text(tnode, source)
    return None


#: The text between two parameters (one ``,``) or around them (none): whitespace, `` _``
#: continuations, comments, and an attribute block the grammar did not attach to its parameter
#: (``<[In], Out> ByVal a() As T`` on the line after ``(``: the parameter is read, only its
#: attributes are not).
_PARAM_GAP = re.compile(rb"""(?:[ \t\r\n_]|'[^\n]*|<(?:[^<>"\n]|"(?:[^"]|"")*")*>)*""")


#: What separates a parameter list's ``)`` from its return type: `` As `` (continued lines too).
_RETURN_AS = re.compile(rb"(?:[ \t\r\n]|_[ \t]*\r?\n)*As(?:[ \t\r\n]|_[ \t]*\r?\n)+", re.IGNORECASE)


def _directly_after(ret: Node, params_node: Node, source: bytes) -> bool:
    """Whether the return type ``ret`` follows the parameter list's ``)`` with only `` As ``
    between, as the grammar saw it."""
    return _RETURN_AS.fullmatch(shadow(source)[params_node.end_byte:ret.start_byte]) is not None


def params_broken(params_node: Node | None, source: bytes) -> bool:
    """Whether the grammar lost text between a parameter list's parameters — anything but the
    separators (``<FromQuery> toY As String`` reads as the keyword ``To`` and falls into an
    ``ERROR`` between them). Its parameters are then not read: none rather than some. An error
    inside one parameter (``<[In], Out> ByVal a() As T``) loses no parameter and is not counted."""
    if params_node is None:
        return False
    source = shadow(source)  # the gaps as the grammar saw them (a blanked ``(Of …)`` is not lost)
    params = [c for c in params_node.named_children if c.type == "parameter"]
    kids = params_node.children
    pos = kids[0].end_byte if kids and kids[0].type == "(" else params_node.start_byte
    end = kids[-1].start_byte if kids and kids[-1].type == ")" else params_node.end_byte
    gaps = [source[pos:params[0].start_byte]] if params else [source[pos:end]]
    gaps += [source[a.end_byte:b.start_byte] for a, b in zip(params, params[1:])]
    if params:
        gaps.append(source[params[-1].end_byte:end])
    for i, gap in enumerate(gaps):
        inner = 0 < i < len(gaps) - 1
        rest = gap.replace(b",", b"", 1) if inner else gap
        if (inner and b"," not in gap) or _PARAM_GAP.fullmatch(rest) is None:
            return True
    return False


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    out: list[Parameter] = []
    if params_node is None or params_broken(params_node, source):
        return out
    for p in params_node.named_children:
        if p.type != "parameter":
            continue
        nnode = p.child_by_field_name("name")
        out.append(Parameter(
            name=node_text(nnode, source) if nnode is not None else "",
            type=as_clause_type(p, source) or "",
            decorators=extract_attributes(p, source),
        ))
    return out


def _calls(node: Node, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(n: Node) -> None:
        # Descend into every scope, including inline lambdas — their calls belong to
        # the nearest named enclosing function (see build_function).
        for child in n.named_children:
            if child.type == "invocation":
                target = child.child_by_field_name("target")
                name = receiver = None
                if target is not None and target.type == "member_access":
                    member = target.child_by_field_name("member")
                    obj = target.child_by_field_name("object")
                    name = node_text(member, source) if member is not None else None
                    receiver = node_text(obj, source) if obj is not None else None
                elif target is not None:
                    name = node_text(target, source)
                if name and name not in seen:
                    seen.add(name)
                    calls.append(Call(name=name, path=resolve(name, receiver)))
            visit(child)

    visit(node)
    return calls


#: ``Function Name`` / ``Sub Name`` in a declaration header.
_HEADER_NAME = re.compile(rb"\b(?:Function|Sub)[ \t]+(?:\[([^\]\n]+)\]|([A-Za-z_]\w*))", re.IGNORECASE)


#: ``Operator`` right before a method's name: an operator declaration the shadow reads as a
#: function (:func:`~.source.blank_operators`), named ``Operator +``, ``Operator CType``.
_OPERATOR_BEFORE = re.compile(rb"\bOperator[ \t]+\Z", re.IGNORECASE)


def method_name(node: Node, source: bytes) -> tuple[str | None, bool]:
    """``(name, header_broken)`` of a method or constructor. When the grammar fails inside the
    header (an ``ERROR`` before the ``name`` field), that field points into the body — e.g. at
    ``IsNullOrWhiteSpace`` from the first ``If`` — so the name is read from the header text
    instead, and ``header_broken`` tells the caller not to trust the parsed parameters."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None, False
    if not any(c.type == "ERROR" and c.start_byte < name_node.start_byte for c in node.named_children):
        name = node_text(name_node, source)
        if _OPERATOR_BEFORE.search(source, node.start_byte, name_node.start_byte):
            name = f"Operator {name}"  # read as a function from the shadow, named as C# names it
        return name, False
    m = _HEADER_NAME.search(source, node.start_byte, name_node.start_byte)
    return (next(g for g in m.groups() if g).decode("utf-8", "replace") if m else None), True


def defined_names(root: Node, source: bytes) -> set[str]:
    names: set[str] = set()
    types = {
        "method_declaration", "constructor_declaration", "class_block",
        "interface_block", "enum_block", "struct_block", "structure_block", "module_block",
    }

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in ("method_declaration", "constructor_declaration"):
                name, _ = method_name(c, source)
                if name is not None:
                    names.add(name)
            elif c.type in types:
                nm = c.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))
            walk(c)

    walk(root)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str]:
    """Variable name → declared type (fields win) for same-file receiver resolution."""
    types: dict[str, str] = {}

    def add(name: str | None, typ: str | None, *, override: bool) -> None:
        if name and typ and (override or name not in types):
            types[name] = typ

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "field_declaration":
                typ = as_clause_type(c, source)
                for d in c.named_children:
                    if d.type == "variable_declarator" and d.named_children:
                        add(node_text(d.named_children[0], source), typ or as_clause_type(d, source),
                            override=True)
            elif c.type == "parameter":
                nnode = c.child_by_field_name("name")
                add(node_text(nnode, source) if nnode is not None else None,
                    as_clause_type(c, source), override=False)
            walk(c)

    walk(root)
    return types


def build_method(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    class_name: str | None,
    id_owner: str | None = None,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[Function, list[Statement]]:
    is_ctor = node.type == "constructor_declaration"
    declared, header_broken = method_name(node, source)
    name = declared or ("New" if is_ctor else "<anonymous>")
    start, end = line_span(node)
    # A nested type's methods carry its dotted owner (``Outer.Inner``) in their id.
    fid = disambiguate(function_id(path, name, start, class_name=id_owner or class_name), seen_ids)
    visibility, is_static = flags(node, source)
    # A broken header's parameter list and return type are read from the wrong text: empty.
    params_node = None if header_broken else node.child_by_field_name("parameters")
    ret = None if header_broken or _is_sub(node, source) else node.child_by_field_name("return_type")
    if params_node is not None and params_broken(params_node, source):
        if ret is not None and not _directly_after(ret, params_node, source):
            ret = None  # the list closed early: the "return type" may be one of its parameters
        params_node = None
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="constructor" if is_ctor else "method",
        visibility=visibility,
        isStatic=is_static,
        params=extract_params(params_node, source),
        decorators=extract_attributes(node, source),
        returnType=type_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(node, source, resolve),
    )
    statements = extract_statements(
        node, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,  # walk inline lambdas — attribute their statements here
    )
    return fn, statements
