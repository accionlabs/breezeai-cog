"""The C# ``collect`` of the ``webforms.page-routes`` fact: ``routes.MapPageRoute(name, url,
"~/physical.aspx")`` calls read from the C# tree (the fact itself is
:mod:`~..webforms_common.page_routes`)."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ..additive import FactSpec, register_fact
from ..csharp.imports import _called_name, _positional_args, _string_literal
from ..webforms_common.page_routes import FACT, add_page_route, app_root_of, resolve_page_routes


def _collect(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> dict[str, list[str]] | None:
    """``MapPageRoute`` mappings from the AST (not raw text — so matches in comments/strings are
    excluded). Only string-literal ``url``/``physical`` args are recorded; ``~/`` resolves
    against the file's app root (the shallowest ancestor with a ``web.config``), so the key
    matches the page's repo path. Iterative walk (no recursion — deep ASTs can't blow the
    stack)."""
    app_root = app_root_of(rel, repo_root)
    found: dict[str, list[str]] = {}
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "invocation_expression" and _called_name(n, source) == "MapPageRoute":
            args = _positional_args(n)
            if len(args) >= 3:
                add_page_route(found, _string_literal(args[1], source),
                               _string_literal(args[2], source), app_root)
        stack.extend(n.named_children)
    return found or None


register_fact(FactSpec(
    name=FACT, language="csharp", gate=(b"MapPageRoute",), collect=_collect,
    resolve=resolve_page_routes,
))
