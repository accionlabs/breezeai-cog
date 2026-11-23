"""Compile-time String constant folding, shared across language parsers.

Faithful to the language's own constant-expression rules: only a ``static final String``
field whose initializer is a string literal — or a ``+`` concatenation of literals and other
such constants — has a value the language guarantees. Anything else (a non-final field, a
method call, a runtime variable) has no compile-time value, so it stays **unresolved**
(honest-null) rather than being guessed. Client-agnostic: this matches syntax only, never any
project's naming.

One exception, read from syntax alone: Java ``T.class.getSimpleName()`` on a class literal
folds to ``T``'s simple name (``Outer.Inner.class`` → ``Inner``). The compiler does not fold
it, but its value is the class's name as written in the source, fixed by the declaration
itself. A class reached any other way (``obj.getClass()``, Groovy's ``x.class``), an array
class (``int[].class``) and the other name methods (``getName()``, ``getCanonicalName()``)
stay unresolved.

An initializer is captured as a flat **token list** — picklable, so a repo-wide
``build_index`` can carry it across worker processes. Each token is either a literal string or
a reference to another constant by name. :func:`resolve_tokens` folds one list against a
``name → value`` map; :func:`resolve_all` runs the small fixpoint that lets one constant
reference another (e.g. ``BUS_NAME = APP_ID + "/x"``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from tree_sitter import Node

from .treesitter import node_text

#: One initializer fragment: ``("lit", text)`` (a string literal) or ``("ref", name)`` (a
#: reference to another constant, by simple name or ``Class.field``).
Token = tuple[str, str]


def init_tokens(node: Node, source: bytes) -> list[Token] | None:
    """A constant-initializer / argument expression → fold tokens, or ``None`` if it is not a
    plain string literal, a ``+`` concatenation of literals, or a reference to another
    constant. Shared across languages (Java/Groovy use the same node types); handles both
    quote styles and skips interpolated (GString) literals."""
    t = node.type
    if t == "string_literal":
        # a Groovy GString (any `*interpolation*` child) has no compile-time value
        if any("interpolation" in c.type for c in node.named_children):
            return None
        if any(c.type == "escape_sequence" for c in node.named_children):
            value = java_literal_value(node, source)  # Java splits a literal at each escape
            return [("lit", value)] if value is not None else None
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        if frag is not None:
            return [("lit", node_text(frag, source))]
        text = node_text(node, source)
        if text[:3] in ('"""', "'''"):  # a text block / multi-line string: not read
            return None
        return [("lit", text[1:-1] if len(text) >= 2 and text[0] in "\"'" else text)]
    if t == "binary_expression":  # only string `+` concatenation folds
        parts: list[Token] = []
        for child in node.named_children:
            sub = init_tokens(child, source)
            if sub is None:
                return None
            parts += sub
        return parts
    if t == "parenthesized_expression":
        inner = next(iter(node.named_children), None)
        return init_tokens(inner, source) if inner is not None else None
    if t in ("identifier", "field_access"):  # reference to another constant (NAME / Class.FIELD)
        return [("ref", node_text(node, source))]
    if t == "method_invocation":
        simple = _class_simple_name(node, source)
        return [("lit", simple)] if simple is not None else None
    return None


#: Java's single-character escapes (JLS 3.10.7).
_JAVA_ESCAPES = {
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    "s": " ",
    '"': '"',
    "'": "'",
    "\\": "\\",
}
_OCTAL_ESCAPE = re.compile(r"[0-3][0-7]{0,2}|[4-7][0-7]?")
_UNICODE_ESCAPE = re.compile(r"u([0-9A-Fa-f]{4})")


def _java_escape(sequence: str) -> str | None:
    """One Java ``escape_sequence`` (``\\"``, ``\\t``, ``\\101``, ``\\u00e9``) → its character,
    or None when it is not a Java escape."""
    body = sequence[1:]
    if body in _JAVA_ESCAPES:
        return _JAVA_ESCAPES[body]
    if _OCTAL_ESCAPE.fullmatch(body):
        return chr(int(body, 8))
    hit = _UNICODE_ESCAPE.fullmatch(body)
    return chr(int(hit.group(1), 16)) if hit else None


def java_literal_value(node: Node, source: bytes) -> str | None:
    """A Java one-line ``string_literal`` → its value, escapes decoded: the grammar splits the
    literal into ``string_fragment`` and ``escape_sequence`` children, all of which make the value.
    None for a text block (``\"\"\"``), an escape that is not Java's, or a lone surrogate."""
    if node_text(node, source).startswith('"""'):
        return None
    parts: list[str] = []
    for child in node.named_children:
        if child.type == "string_fragment":
            parts.append(node_text(child, source))
        elif child.type == "escape_sequence":
            char = _java_escape(node_text(child, source))
            if char is None:
                return None
            parts.append(char)
        else:
            return None
    try:  # `😀` is one character written as a UTF-16 surrogate pair
        return "".join(parts).encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return None


def _class_simple_name(call: Node, source: bytes) -> str | None:
    """Java ``T.class.getSimpleName()`` → ``T``'s simple name, else ``None``.

    Only a Java ``class_literal`` over a plain or dotted type name qualifies — never an array
    type. Groovy is left out on purpose: there ``x.class`` on a *variable* means
    ``x.getClass()``, and syntax alone cannot tell a type name from a variable."""
    name = call.child_by_field_name("name")
    receiver = call.child_by_field_name("object")
    args = call.child_by_field_name("arguments")
    if (
        name is None
        or receiver is None
        or args is None
        or args.named_child_count
        or node_text(name, source) != "getSimpleName"
        or receiver.type != "class_literal"
    ):
        return None
    type_node = receiver.named_children[0] if receiver.named_children else None
    if type_node is None or type_node.type not in ("type_identifier", "scoped_type_identifier"):
        return None
    return node_text(type_node, source).rsplit(".", 1)[-1]


def resolve_tokens(tokens: list[Token], values: dict[str, str]) -> str | None:
    """Fold a token list to a string, resolving ``ref`` tokens via ``values``. Returns
    ``None`` if any reference is unresolved — an address is all-or-nothing, never partial."""
    out: list[str] = []
    for kind, text in tokens:
        if kind == "lit":
            out.append(text)
        else:
            hit = values.get(text)
            if hit is None:  # absent, ambiguous, or not-yet-resolved → honest-null
                return None
            out.append(hit)
    return "".join(out)


def resolve_all(
    raw: Mapping[str, list[Token] | None], base: dict[str, str] | None = None
) -> dict[str, str]:
    """Fold a ``name → tokens`` map to ``name → value`` via a bounded fixpoint, so a constant
    may reference another (chains are short in practice). ``base`` seeds already-resolved
    values (e.g. a repo-wide index), so a local constant can reference a cross-file one.
    ``None`` tokens (an ambiguous name) and anything still unresolved are dropped — honest-null."""
    values: dict[str, str] = dict(base or {})
    pending = {k: v for k, v in raw.items() if v is not None}
    for _ in range(len(pending) if pending else 0):  # worst case: a chain of every constant
        progressed = False
        for name, tokens in list(pending.items()):
            val = resolve_tokens(tokens, values)
            if val is not None:
                values[name] = val
                del pending[name]
                progressed = True
        if not progressed:
            break
    return values
