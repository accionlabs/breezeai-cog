"""Compile-time String constant folding, shared across language parsers.

Faithful to the language's own constant-expression rules: only a ``static final String``
field whose initializer is a string literal — or a ``+`` concatenation of literals and other
such constants — has a value the language guarantees. Anything else (a non-final field, a
method call, a runtime variable) has no compile-time value, so it stays **unresolved**
(honest-null) rather than being guessed. Client-agnostic: this matches syntax only, never any
project's naming.

An initializer is captured as a flat **token list** — picklable, so a repo-wide
``build_index`` can carry it across worker processes. Each token is either a literal string or
a reference to another constant by name. :func:`resolve_tokens` folds one list against a
``name → value`` map; :func:`resolve_all` runs the small fixpoint that lets one constant
reference another (e.g. ``BUS_NAME = APP_ID + "/x"``).
"""

from __future__ import annotations

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
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        if frag is not None:
            return [("lit", node_text(frag, source))]
        text = node_text(node, source)
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
    return None


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
