"""Which FastAPI dependencies certainly reject an anonymous call — the only guards that assert
``authRequired``.

A dependency runs before the handler, but most are not authentication: a DB session, paging, a
rate limit. Only FastAPI's own security schemes reject a request without credentials before the
handler runs (HTTP 401/403), and only when ``auto_error`` is left on::

    oauth2 = OAuth2PasswordBearer(tokenUrl="token")              # certain
    optional = HTTPBearer(auto_error=False)                       # not: a missing token → None
    def current_user(token: str = Depends(oauth2)): …             # certain: oauth2 runs first
    @router.get("/me", dependencies=[Depends(current_user)])      # → authRequired true

Read from this file only: a scheme assigned at module level from ``fastapi.security``, and a
top-level function with a parameter that depends on a certain scheme or function (followed
through the file). A name defined elsewhere, or a scheme whose ``auto_error`` is not a literal,
asserts nothing — the guard is still listed.
"""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text
from .file_facts import DEPENDS, keyword

#: fastapi.security classes that reject a call without credentials when ``auto_error`` is on.
SCHEMES = frozenset({
    "OAuth2", "OAuth2PasswordBearer", "OAuth2AuthorizationCodeBearer", "OpenIdConnect",
    "HTTPBasic", "HTTPBearer", "HTTPDigest", "APIKeyHeader", "APIKeyQuery", "APIKeyCookie",
})
_MODULE = "fastapi.security"


def _scheme_names(root: Node, source: bytes) -> dict[str, str]:
    """Local name → scheme class, for names imported from ``fastapi.security``."""
    out: dict[str, str] = {}
    for stmt in root.named_children:
        mod = stmt.child_by_field_name("module_name") if stmt.type == "import_from_statement" else None
        if mod is None or node_text(mod, source) != _MODULE:
            continue
        for name_node in stmt.named_children:
            if name_node is mod:
                continue
            orig = name_node.child_by_field_name("name") if name_node.type == "aliased_import" else name_node
            alias = name_node.child_by_field_name("alias") if name_node.type == "aliased_import" else name_node
            if orig is not None and alias is not None and node_text(orig, source) in SCHEMES:
                out[node_text(alias, source)] = node_text(orig, source)
    return out


def _is_scheme_call(call: Node, source: bytes, imported: dict[str, str]) -> bool:
    fn = call.child_by_field_name("function")
    text = node_text(fn, source) if fn is not None else ""
    if not (text in imported or (text.startswith(_MODULE + ".") and text.rsplit(".", 1)[-1] in SCHEMES)):
        return False
    auto_error = keyword(call, source, "auto_error")
    return auto_error is None or node_text(auto_error, source) == "True"


def _depends_on(fdef: Node, source: bytes) -> set[str]:
    """Names a function's parameters depend on (``x = Depends(name)`` / ``Security(name)``)."""
    out: set[str] = set()
    params = fdef.child_by_field_name("parameters")
    for param in params.named_children if params is not None else []:
        value = param.child_by_field_name("value")
        if value is None or value.type != "call":
            continue
        fn = value.child_by_field_name("function")
        args = value.child_by_field_name("arguments")
        first = next((a for a in args.named_children if a.type != "keyword_argument"), None) \
            if args is not None else None
        if fn is not None and node_text(fn, source) in DEPENDS and first is not None \
                and first.type == "identifier":
            out.add(node_text(first, source))
    return out


def certain_auth(root: Node, source: bytes) -> frozenset[str]:
    """The dependency names of this file that certainly reject an anonymous call."""
    if b"fastapi.security" not in source:
        return frozenset()
    imported = _scheme_names(root, source)
    certain: set[str] = set()
    functions: dict[str, set[str]] = {}
    for stmt in root.named_children:
        node = stmt.named_children[0] if stmt.type == "expression_statement" and stmt.named_children else stmt
        if node.type == "decorated_definition":
            node = node.child_by_field_name("definition") or node
        if node.type == "assignment":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and left.type == "identifier" and right is not None \
                    and right.type == "call" and _is_scheme_call(right, source, imported):
                certain.add(node_text(left, source))
        elif node.type == "function_definition":
            fname = node.child_by_field_name("name")
            if fname is not None:
                functions[node_text(fname, source)] = _depends_on(node, source)
    changed = True
    while changed:  # a function depending on a certain one is certain: its dependency runs first
        changed = False
        for name, deps in functions.items():
            if name not in certain and deps & certain:
                certain.add(name)
                changed = True
    return frozenset(certain)
