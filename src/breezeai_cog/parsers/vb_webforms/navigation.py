"""The VB half of Web Forms navigation: literal ``Response.Redirect`` / ``Server.Transfer`` calls
in a code-behind, read from the VB tree (``vb/scopes.py``). The records are built by
:func:`~..webforms_common.navigation.detect_navigation`, as for C#::

    Response.Redirect("~/Confirm.aspx?id=1")          → navigation  endpoint /Confirm.aspx
    HttpContext.Current.Server.Transfer("Done.aspx")  → navigation  endpoint /<dir>/Done.aspx

VB names are case-insensitive, so the method and the receiver are matched without case. The
target must be a string built only from literals; a variable or a named argument gives none.
"""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text
from ..vb.scopes import iter_calls, string_value
from ..webforms_common.navigation import REDIRECT_METHODS

_RECEIVERS = {m.lower(): r.lower() for m, r in REDIRECT_METHODS.items()}


def codebehind_redirects(root: Node, source: bytes) -> list[tuple[str, Node]]:
    """(target, call node) for each ``Response.Redirect`` / ``Server.Transfer`` whose first
    argument is a literal string."""
    lowered = source.lower()
    if b"redirect" not in lowered and b"transfer" not in lowered:  # cheap gate before the walk
        return []
    out: list[tuple[str, Node]] = []
    for call in iter_calls(root, source):
        want = _RECEIVERS.get(call.method.lower())
        if want is None or not call.args:
            continue
        if node_text(call.receiver, source).rsplit(".", 1)[-1].strip().lower() != want:
            continue
        target = string_value(call.args[0], source)
        if target is not None:
            out.append((target, call.node))
    return out
