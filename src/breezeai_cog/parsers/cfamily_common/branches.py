"""Functions whose braces a preprocessor conditional splits.

cog doesn't run the preprocessor, so the grammar reads every branch of an ``#if`` at once. When
the branches open and close braces separately —

    } else {          ← inside one #ifdef USE_DECNUM block
    …
    }                 ← inside a later #ifdef USE_DECNUM block

— the compiler sees both or neither, but the grammar can't match them: the function's body runs
on into the functions after it (which are lost, their calls credited to it) or stops short.

Such a file is parsed a second time with every conditional resolved one consistent way: the
directive lines, the branches after the first and ``#if 0`` blocks blanked to spaces, so lines
and byte offsets don't move. That parse is only a second opinion: it drops every ``#else``-only
definition, so it never replaces the normal one. It is used only where the normal parse lost
something:

* where the two disagree about where a function ends — then the function is read from it, and
  so are the functions the normal parse swallowed;
* inside an error at file scope that holds a conditional — an ``else if`` written inside an
  ``#ifndef``, or a split brace in a function the error swallowed, can turn the rest of the file
  into one error the parser can't read. What the second parse finds there is read from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from ..treesitter import parse_source
from .mappings import TRANSPARENT_SCOPES

_DIRECTIVE = re.compile(rb"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
_DIRECTIVE_IN = re.compile(rb"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|else|endif)\b", re.M)
_NOT_SPACE = re.compile(rb"\S")

#: What the second parse may supply: definitions and declarations. Anything else it finds at
#: file scope (a bare ``while`` from a macro's continuation lines) is its own misreading.
_SUPPLIED = frozenset({
    "function_definition", "declaration", "type_definition", "alias_declaration",
    "class_specifier", "struct_specifier", "union_specifier", "enum_specifier",
    "template_declaration", "preproc_def", "preproc_function_def", "concept_definition",
})


def blank_conditionals(source: bytes) -> bytes:
    """``source`` with each conditional resolved to its first branch: directive lines, the lines
    of later branches and of ``#if 0`` blocks become spaces. Lines and offsets are kept."""
    out: list[bytes] = []
    stack: list[list[bool]] = []  # per open conditional: [this branch live, a branch taken]
    in_directive = False
    for line in source.split(b"\n"):
        m = None if in_directive else _DIRECTIVE.match(line)
        if m is not None:
            keyword, rest = m.group(1), m.group(2).strip()
            if keyword in (b"if", b"ifdef", b"ifndef"):
                live = not (keyword == b"if" and re.match(rb"0\b", rest) is not None)
                stack.append([live, live])
            elif keyword in (b"elif", b"else") and stack:
                stack[-1][0] = not stack[-1][1]
                stack[-1][1] = True
            elif keyword == b"endif" and stack:
                stack.pop()
        blank = in_directive or m is not None or not all(live for live, _ in stack)
        out.append(_NOT_SPACE.sub(b" ", line) if blank else line)
        in_directive = (in_directive or m is not None) and line.endswith(b"\\")
    return b"\n".join(out)


def _top_level(scope: Node) -> list[Node]:
    """The file- and namespace-scope nodes the parser walks, in order."""
    found: list[Node] = []
    stack = [scope]
    while stack:
        node = stack.pop()
        for c in reversed(node.named_children):
            if c.type == "template_declaration" or c.type in TRANSPARENT_SCOPES:
                stack.append(c)
            elif c.type == "namespace_definition":
                body = c.child_by_field_name("body")
                if body is not None:
                    stack.append(body)
                found.append(c)
            else:
                found.append(c)
    return sorted(found, key=lambda n: n.start_byte)


def _split_body(fn: Node, source: bytes) -> bool:
    """Whether a function's body has a parse error and a conditional directive in it — the
    directive may be a node or, inside an error, bare tokens."""
    body = fn.child_by_field_name("body")
    return (
        body is not None and body.has_error
        and _DIRECTIVE_IN.search(source, body.start_byte, body.end_byte) is not None
    )


def _trustworthy(node: Node, source: bytes) -> bool:
    """Whether a node the second parse found can be taken as read. Not when it misread too:
    its last token is a ``}`` the grammar inserted to close a body it lost track of (the node
    runs on to wherever it gave up), or an error outside a function body swallowed a brace (a
    function's head read as a declaration, its ``{`` lost in the error)."""
    last = node
    while last.children:
        last = last.children[-1]
    if last.is_missing:
        return False
    body = node.child_by_field_name("body") if node.type == "function_definition" else None
    stack = [c for c in node.children if c != body]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" and (b"{" in source[n.start_byte:n.end_byte]
                                  or b"}" in source[n.start_byte:n.end_byte]):
            return False
        stack.extend(n.children)
    return True


@dataclass
class BranchFix:
    """How to read a file whose function braces a conditional split. ``replace`` maps a
    function's start byte to its node in the second parse; ``extra`` are the file- and
    namespace-scope nodes (functions, classes, declarations…) the second parse finds in the span
    a function wrongly covered; ``skip`` are start bytes of nodes the normal parse put inside a
    corrected function's real span."""

    replace: dict[int, Node] = field(default_factory=dict)
    extra: list[Node] = field(default_factory=list)
    skip: set[int] = field(default_factory=set)
    #: The normal parse's root was an error and the second parse read the file whole: what the
    #: second parse supplies is class members inside their classes, never spilled ones.
    whole: bool = False


def branch_fix(root: Node, source: bytes, grammar: str, timeout_micros: int) -> BranchFix | None:
    """The corrections for a file, or ``None`` when it needs none (the usual case: no function
    body and no file-scope error holds a broken conditional, or both parses agree)."""
    if not root.has_error or b"#if" not in source:
        return None
    if root.type == "ERROR":
        return _root_error_fix(root, source, grammar, timeout_micros)
    first = _top_level(root)
    lost = [  # file-scope errors with a conditional in them: everything in them is lost
        (n.start_byte, n.end_byte) for n in first
        if n.type == "ERROR" and _DIRECTIVE_IN.search(source, n.start_byte, n.end_byte) is not None
    ]
    if not lost and not any(
        n.type == "function_definition" and _split_body(n, source) for n in first
    ):
        return None
    second = _top_level(parse_source(grammar, blank_conditionals(source), timeout_micros).root_node)
    second_fns = {n.start_byte: n for n in second if n.type == "function_definition"}
    fix = BranchFix()
    spans: list[tuple[int, int]] = list(lost)  # where the normal parse is wrong or lost
    for n in first:
        other = second_fns.get(n.start_byte)
        if (
            n.type == "function_definition" and other is not None
            and other.end_byte != n.end_byte and _trustworthy(other, source)
        ):
            fix.replace[n.start_byte] = other
            spans.append((min(n.end_byte, other.end_byte), max(n.end_byte, other.end_byte)))
    if not spans:
        return None
    first_starts = {n.start_byte for n in first if n.type != "ERROR"}
    for start, end in spans:
        fix.skip.update(s for s in first_starts if start <= s < end)
        fix.extra.extend(
            n for n in second
            if start <= n.start_byte < end and n.start_byte not in first_starts
            and n.start_byte not in fix.replace
            and n.type in _SUPPLIED  # a namespace's members are listed themselves
            and _trustworthy(n, source)
        )
    fix.extra = sorted({n.start_byte: n for n in fix.extra}.values(), key=lambda n: n.start_byte)
    for n in fix.extra:  # one that runs past the lost span covers what the normal parse had there
        fix.skip.update(s for s in first_starts if n.start_byte <= s < n.end_byte)
    return fix


def _root_error_fix(
    root: Node, source: bytes, grammar: str, timeout_micros: int,
) -> BranchFix | None:
    """The corrections for a file whose whole parse is one error. A conditional can do that —
    zlib's ``inflate.c`` splits an ``if (`` across ``#ifdef GUNZIP`` / ``#else`` — and then the
    statements and locals of the broken function read as file-scope nodes. When the second parse
    (conditionals resolved to their first branch) reads the file without a root error, each
    file- or namespace-scope node it finds that the normal parse doesn't have as such is read
    from it, and the normal parse's nodes inside it are skipped. The normal parse's nodes outside
    every such node (an ``#else``-only definition) stay."""
    second_root = parse_source(grammar, blank_conditionals(source), timeout_micros).root_node
    if second_root.type == "ERROR":
        return None
    first = {n.start_byte: n for n in _top_level(root) if n.type != "ERROR"}
    fix = BranchFix(whole=True)
    for n in _top_level(second_root):
        if n.type not in _SUPPLIED or not _trustworthy(n, source):
            continue
        same = first.get(n.start_byte)
        if same is not None and same.type == n.type and same.end_byte == n.end_byte:
            continue  # both parses read it the same way
        if same is not None and same.type == n.type == "function_definition":
            fix.replace[n.start_byte] = n
            fix.skip.update(s for s in first if n.start_byte < s < n.end_byte)
        else:
            fix.extra.append(n)
            fix.skip.update(s for s in first if n.start_byte <= s < n.end_byte)
    return fix if fix.replace or fix.extra else None


def enclosing_scope(node: Node, source: bytes) -> tuple[str, bool]:
    """The ``::``-joined namespace path a top-level node sits in, and whether it is inside
    ``extern "C"`` — for a function only the second parse found."""
    names: list[str] = []
    c_linkage = False
    parent = node.parent
    while parent is not None:
        if parent.type == "namespace_definition":
            nm = parent.child_by_field_name("name")
            if nm is not None:
                names.append(source[nm.start_byte:nm.end_byte].decode("utf-8", "replace"))
        elif parent.type == "linkage_specification":
            value = parent.child_by_field_name("value")
            if value is not None and source[value.start_byte:value.end_byte].strip(b'"') == b"C":
                c_linkage = True
        parent = parent.parent
    return "::".join(reversed(names)), c_linkage
