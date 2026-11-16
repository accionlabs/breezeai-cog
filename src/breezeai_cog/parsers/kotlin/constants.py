"""Kotlin string constants: the values a path argument can be folded from.

``string_constants`` / ``lookup_constant`` read one file's ``val`` / ``const val`` strings;
``importable_constants`` collects the ones another file can name (top-level, or a member of an
``object`` / ``companion object``) by fully-qualified name for the repo index
(``KotlinIndex.consts``); ``ConstantScope`` resolves a written reference in one file: its own
constants first, then its explicit imports (an alias too), then its own package. Star imports
are not followed and a constant built from another file's constant is not stored: either leaves
the value unknown (honest-null).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping

from tree_sitter import Node

from ..treesitter import node_text

#: Node types a path / constant value can be written as.
PATH_EXPRS = frozenset({
    "string_literal", "simple_identifier", "navigation_expression", "additive_expression",
})


_CONTAINERS = frozenset({"object_declaration", "class_declaration", "companion_object"})


class QualifiedNames:
    """The declared qualified names of a file (``Routes.Admin.ITEMS``), indexed so a written one
    finds its matches (:meth:`matches`) in the time of its own segments, not of the file's
    constants: a lookup per route stays linear in file size."""

    def __init__(self, declared: Iterable[str]) -> None:
        self.names = frozenset(k for k in declared if "." in k)
        #: every qualified (2+ segment) suffix of a declared name → the names ending with it
        self.by_suffix: dict[str, list[str]] = {}
        for k in self.names:
            parts = k.split(".")
            for i in range(len(parts) - 1):
                self.by_suffix.setdefault(".".join(parts[i:]), []).append(k)

    def matches(self, ref: str) -> list[str]:
        """The declared names a written one (``Admin.ITEMS``) can be: one chain ends with the
        other — ``Routes.Admin.ITEMS`` for ``Admin.ITEMS`` (the written chain starts lower),
        ``Admin.ITEMS`` for ``com.x.Admin.ITEMS`` (a package in front)."""
        found = dict.fromkeys(self.by_suffix.get(ref, ()))
        parts = ref.split(".")
        for i in range(1, len(parts) - 1):
            suffix = ".".join(parts[i:])
            if suffix in self.names:
                found[suffix] = None
        return list(found)


def string_constants(root: Node, source: bytes) -> dict[str, str | None]:
    """``val`` / ``const val`` string properties of this file → their value, by simple name
    (``USERS``) and, for one inside an ``object`` / class / ``companion object``, also by its
    container chain (``object Paths { const val USERS = "/users" }`` → ``Paths.USERS``). A value
    may interpolate another constant. A name declared twice with different values, or with a
    value that does not resolve, maps to None. Look names up with :func:`lookup_constant`."""
    raw: dict[str, list[Node]] = {}

    def walk(n: Node, chain: tuple[str, ...]) -> None:
        if n.type == "property_declaration":
            var = next((c for c in n.named_children if c.type == "variable_declaration"), None)
            name = var.named_children[0] if var is not None and var.named_children else None
            value = next((c for c in n.named_children if c.type in PATH_EXPRS), None)
            if name is not None and name.type == "simple_identifier" and value is not None:
                simple = node_text(name, source)
                raw.setdefault(simple, []).append(value)
                if chain:
                    raw.setdefault(".".join((*chain, simple)), []).append(value)
            return  # a property's initializer declares no constants of the container
        if n.type in _CONTAINERS:
            nm = next((c for c in n.named_children if c.type == "type_identifier"), None)
            if n.type != "companion_object":  # a companion's members are the class's
                if nm is None:
                    return
                chain = (*chain, node_text(nm, source))
        elif n.type == "infix_expression" and [c.type for c in n.named_children] == [
                "object_literal", "simple_identifier", "lambda_literal"]:
            # the grammar's parse of a one-line ``object Paths { … }``
            chain = (*chain, node_text(n.named_children[1], source))
        for c in n.named_children:
            walk(c, chain)

    walk(root, ())
    out: dict[str, str | None] = {}
    qualified = QualifiedNames(raw)

    def value_of(name: str, depth: int) -> str | None:
        if name in out:
            return out[name]
        if "." in name and name not in raw:
            values = {value_of(k, depth + 1) for k in qualified.matches(name)}
            return values.pop() if len(values) == 1 else None
        nodes = raw.get(name)
        if not nodes or depth > 8:
            return None
        values = {resolve_expr(v, source, lambda n: value_of(n, depth + 1)) for v in nodes}
        out[name] = values.pop() if len(values) == 1 else None
        return out[name]

    for name in raw:
        value_of(name, 0)
    return out


def lookup_constant(consts: dict[str, str | None], ref: str,
                    qualified: QualifiedNames | None = None) -> str | None:
    """A written constant reference's value: a bare name by its simple name; a qualified one
    (``Admin.ITEMS``) only through a declared container chain it matches
    (:meth:`QualifiedNames.matches`) — never another container's constant of the same simple
    name. None when unknown or when the matches disagree. Pass ``qualified`` (built once from
    ``consts``) when looking up many references."""
    if "." not in ref:
        return consts.get(ref)
    names = qualified if qualified is not None else QualifiedNames(consts)
    values = {consts.get(k) for k in names.matches(ref)}
    return values.pop() if len(values) == 1 else None


def resolve_expr(node: Node, source: bytes, lookup: Callable[[str], str | None]) -> str | None:
    """A path expression → its literal string, or None when any part is not a known constant.
    String content is kept verbatim (Ktor path parameters are written ``{id}`` inside it)."""
    if node.type == "string_literal":
        parts: list[str] = []
        for child in node.named_children:
            if child.type == "string_content":
                parts.append(node_text(child, source))
            elif child.type in ("interpolated_identifier", "interpolated_expression"):
                expr = child if child.type == "interpolated_identifier" else (
                    child.named_children[0] if child.named_children else None)
                value = lookup(node_text(expr, source)) if expr is not None and expr.type in (
                    "interpolated_identifier", "simple_identifier") else None
                if value is None:
                    return None
                parts.append(value)
        return "".join(parts)
    if node.type == "simple_identifier":
        return lookup(node_text(node, source))
    if node.type == "navigation_expression":  # Paths.USERS — by its chain (lookup_constant)
        ref = "".join(node_text(node, source).split())
        return lookup(ref) if re.fullmatch(r"\w+(\.\w+)+", ref) else None
    if node.type == "additive_expression" and len(node.named_children) == 2:
        left, right = (resolve_expr(c, source, lookup) for c in node.named_children)
        return left + right if left is not None and right is not None else None
    return None


def _is_val(prop: Node, source: bytes) -> bool:
    """``val`` / ``const val`` (never ``var``: its value can change)."""
    kind = next((c for c in prop.named_children if c.type == "binding_pattern_kind"), None)
    return kind is not None and node_text(kind, source) == "val"


def _infix_object(n: Node, source: bytes) -> tuple[str, Node] | None:
    """The grammar's parse of a top-level ``object Name { … }``: ``(Name, its lambda body)``."""
    kids = n.named_children
    if n.type == "infix_expression" and [c.type for c in kids] == [
            "object_literal", "simple_identifier", "lambda_literal"]:
        return node_text(kids[1], source), kids[2]
    return None


def importable_constants(root: Node, source: bytes, package: str) -> dict[str, str]:
    """This file's string constants another file can reference, by fully-qualified name: a
    top-level ``val`` (``com.acme.API``) and a ``val`` member of an ``object`` or a
    ``companion object`` (``com.acme.Paths.USERS``, ``com.acme.K.C``). A ``var``, an instance
    property of a class, or a local is never one. Only values that resolve within this file
    are kept (a constant built from another file's constant is unknown here)."""
    local = string_constants(root, source)
    qualified = QualifiedNames(local)
    found: dict[str, str] = {}

    def lookup(ref: str) -> str | None:
        return lookup_constant(local, ref, qualified)

    def walk(n: Node, chain: tuple[str, ...], reachable: bool) -> None:
        for c in n.named_children:
            if c.type == "property_declaration":
                if not reachable or not _is_val(c, source):
                    continue
                var = next((v for v in c.named_children if v.type == "variable_declaration"), None)
                name = var.named_children[0] if var is not None and var.named_children else None
                value = next((v for v in c.named_children if v.type in PATH_EXPRS), None)
                if name is None or name.type != "simple_identifier" or value is None:
                    continue
                resolved = resolve_expr(value, source, lookup)
                if resolved is not None:
                    key = ".".join((package, *chain, node_text(name, source)) if package
                                   else (*chain, node_text(name, source)))
                    found[key] = resolved
            elif (infix := _infix_object(c, source)) is not None:
                body = next((b for b in infix[1].named_children if b.type == "statements"), None)
                if body is not None:
                    walk(body, (*chain, infix[0]), True)
            elif c.type in ("object_declaration", "class_declaration"):
                nm = next((t for t in c.named_children if t.type == "type_identifier"), None)
                body = next((b for b in c.named_children if b.type == "class_body"), None)
                if nm is not None and body is not None:
                    # an object's members are static; a class's are per instance
                    walk(body, (*chain, node_text(nm, source)), c.type == "object_declaration")
            elif c.type == "companion_object":
                body = next((b for b in c.named_children if b.type == "class_body"), None)
                if body is not None:
                    walk(body, chain, True)  # a companion's members are the class's

    walk(root, (), True)
    return found


class ConstantScope:
    """Resolves a constant reference written in one file, as Kotlin does: a name the file
    declares itself (even one it cannot resolve: it shadows the rest), else an explicit import
    (``import com.acme.Paths`` / ``…Paths.USERS`` / ``…Paths as P``), else the file's own package,
    else the reference as a fully-qualified name. Star imports are not followed."""

    def __init__(self, root: Node, source: bytes, local: dict[str, str | None],
                 repo: Mapping[str, str] | None) -> None:
        self.local = local
        self.qualified = QualifiedNames(local)
        self.repo = repo or {}
        self.package = ""
        self.imports: dict[str, str] = {}
        for node in root.named_children:
            if node.type == "package_header":
                pkg = next((c for c in node.named_children if c.type == "identifier"), None)
                self.package = node_text(pkg, source) if pkg is not None else ""
            elif node.type == "import_list" and self.repo:
                for imp in node.named_children:
                    ident = next((c for c in imp.named_children if c.type == "identifier"), None)
                    if ident is None or any(c.type == "wildcard_import" for c in imp.children):
                        continue
                    fqn = node_text(ident, source)
                    alias = next((c for c in imp.named_children if c.type == "import_alias"), None)
                    alias_name = next((c for c in alias.named_children
                                       if c.type in ("type_identifier", "simple_identifier")),
                                      None) if alias is not None else None
                    name = node_text(alias_name, source) if alias_name is not None \
                        else fqn.rsplit(".", 1)[-1]
                    self.imports[name] = fqn

    def _declared_here(self, ref: str) -> bool:
        if "." not in ref:
            return ref in self.local
        return bool(self.qualified.matches(ref))

    def __call__(self, ref: str) -> str | None:
        if self._declared_here(ref) or not self.repo:
            return lookup_constant(self.local, ref, self.qualified)
        head, _, rest = ref.partition(".")
        if head in self.imports:  # an explicit import names it: nothing else is consulted
            return self.repo.get(self.imports[head] + ("." + rest if rest else ""))
        if self.package and (value := self.repo.get(f"{self.package}.{ref}")) is not None:
            return value
        return self.repo.get(ref) if "." in ref else None
