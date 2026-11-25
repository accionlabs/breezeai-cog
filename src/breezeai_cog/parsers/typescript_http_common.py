"""Helpers shared by the TypeScript / JavaScript HTTP server detectors (``typescript_express``,
``typescript_fastify``): what names a middleware or handler argument, and which package each local
name of a file is imported from. One framework's package never imports another's (architecture
§15), so what both read lives here."""

from __future__ import annotations

from tree_sitter import Node

from .treesitter import node_text
from .typescript.imports import _module_of, _require_module


def mw_name(node: Node, source: bytes) -> str | None:
    """A middleware/handler arg's callable name — ``requireAuth`` from ``requireAuth()``,
    ``Auth.check`` from the member call, ``rateLimit`` from a bare reference. An inline
    (arrow/function) middleware has no name → None (honest-null, no fabricated name)."""
    n = node.child_by_field_name("function") if node.type == "call_expression" else node
    if n is not None and n.type in ("identifier", "member_expression"):
        return node_text(n, source)
    return None


def package_locals(root: Node, source: bytes) -> dict[str, tuple[str, str]]:
    """Local name → (package, the export it binds; ``*`` for the default / namespace) for the
    file's package imports and top-level ``require``s: ``import swaggerUi from
    'swagger-ui-express'`` → ``swaggerUi: ('swagger-ui-express', '*')``; ``const { graphqlHTTP }
    = require('express-graphql')`` → ``graphqlHTTP: ('express-graphql', 'graphqlHTTP')``.
    Relative imports are left out (they are the repo's own code)."""
    out: dict[str, tuple[str, str]] = {}
    for node in root.named_children:
        if node.type == "import_statement":
            module = _module_of(node, source)
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            if module is None or module.startswith(".") or clause is None:
                continue
            for c in clause.named_children:
                if c.type == "identifier":
                    out[node_text(c, source)] = (module, "*")
                elif c.type == "namespace_import":
                    ident = next((x for x in c.named_children if x.type == "identifier"), None)
                    if ident is not None:
                        out[node_text(ident, source)] = (module, "*")
                elif c.type == "named_imports":
                    for spec in c.named_children:
                        idents = [x for x in spec.named_children if x.type == "identifier"]
                        if spec.type == "import_specifier" and idents:
                            out[node_text(idents[-1], source)] = (module, node_text(idents[0], source))
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for d in node.named_children:
                name = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                value = d.child_by_field_name("value")
                module = _require_module(value, source) \
                    if name is not None and value is not None and value.type == "call_expression" \
                    else None
                if name is None or module is None or module.startswith("."):
                    continue
                if name.type == "identifier":
                    out[node_text(name, source)] = (module, "*")
                elif name.type == "object_pattern":
                    for prop in name.named_children:
                        if prop.type == "shorthand_property_identifier_pattern":
                            out[node_text(prop, source)] = (module, node_text(prop, source))
    return out


#: Function values a handler may be written as inline.
INLINE_FUNCTIONS = ("arrow_function", "function_expression", "function", "generator_function")


def inline_handler(node: Node, source: bytes) -> tuple[str | None, int]:
    """(handler, handlerLine) of a handler written inline (entry-point contract §5): its own
    name when it has one (``app.post('/users', function createUser(req, res) {…})`` →
    ``createUser``, the Function cog records for it), else none; the line where it starts."""
    name = node.child_by_field_name("name") if node.type != "arrow_function" else None
    return (node_text(name, source) if name is not None else None), node.start_point[0] + 1
