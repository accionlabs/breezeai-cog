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

#: One initializer fragment: ``("lit", text)`` (a string literal) or ``("ref", name)`` (a
#: reference to another constant, by simple name or ``Class.field``).
Token = tuple[str, str]


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


def resolve_all(raw: dict[str, list[Token] | None]) -> dict[str, str]:
    """Fold a ``name → tokens`` map to ``name → value`` via a bounded fixpoint, so a constant
    may reference another (chains are short in practice). ``None`` tokens (an ambiguous name)
    and anything still unresolved after the passes are dropped — honest-null."""
    values: dict[str, str] = {}
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
