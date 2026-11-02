"""Syntax helpers shared by the Python framework parsers (FastAPI, Flask, Django / DRF).

Reading a literal, a keyword argument or a module-level constant, and following an import to the
repository file it names, are the same for every Python framework. They live here so no
framework package imports another's (architecture §15).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

from .treesitter import node_text


def literal(node: Node | None, source: bytes) -> str | None:
    """A plain string literal's value (no f-string interpolation), else None."""
    if node is None or node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.named_children):
        return None
    content = next((c for c in node.named_children if c.type == "string_content"), None)
    return node_text(content, source) if content is not None else ""


def string_list(node: Node | None, source: bytes) -> list[str] | None:
    """The values of a list / tuple / set of plain string literals; None when the node is not
    one, or when any element is not a plain literal (the value is then not known)."""
    if node is None or node.type not in ("list", "tuple", "set"):
        return None
    out: list[str] = []
    for el in node.named_children:
        value = literal(el, source)
        if value is None:
            return None
        out.append(value)
    return out


def keyword(call: Node, source: bytes, name: str) -> Node | None:
    """The value node of keyword argument ``name`` of ``call``, or None when absent."""
    args = call.child_by_field_name("arguments")
    for arg in args.named_children if args is not None else []:
        key = arg.child_by_field_name("name") if arg.type == "keyword_argument" else None
        if key is not None and node_text(key, source) == name:
            return arg.child_by_field_name("value")
    return None


def positional(call: Node) -> list[Node]:
    """The positional arguments of ``call`` (keyword arguments, ``*a`` and ``**k`` left out)."""
    args = call.child_by_field_name("arguments")
    if args is None or args.type != "argument_list":
        return []
    return [a for a in args.named_children
            if a.type not in ("keyword_argument", "list_splat", "dictionary_splat", "comment")]


def argument(call: Node, source: bytes, index: int, name: str) -> Node | None:
    """The argument passed as keyword ``name``, else the positional one at ``index``."""
    value = keyword(call, source, name)
    if value is not None:
        return value
    args = positional(call)
    return args[index] if index < len(args) else None


def calls(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "call":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def top_statements(root: Node) -> Iterator[Node]:
    """The module's top-level statements, an ``expression_statement`` unwrapped to its
    expression (``x = 1`` is an ``assignment`` either way)."""
    for stmt in root.named_children:
        if stmt.type == "expression_statement" and stmt.named_children:
            yield stmt.named_children[0]
        else:
            yield stmt


def module_constants(root: Node, source: bytes) -> dict[str, str]:
    """``NAME = "literal"`` at module level. A name assigned more than once is left out."""
    out: dict[str, str] = {}
    twice: set[str] = set()
    for node in top_statements(root):
        if node.type != "assignment":
            continue
        left = node.child_by_field_name("left")
        if left is None or left.type != "identifier":
            continue
        name = node_text(left, source)
        value = literal(node.child_by_field_name("right"), source)
        if name in out or name in twice or value is None:
            out.pop(name, None)
            twice.add(name)
            continue
        out[name] = value
    return out


def module_file(repo_root: Path, importer: str, module: str, level: int) -> str | None:
    """The repo file a module name refers to (``pkg/mod.py`` or ``pkg/mod/__init__.py``), from
    the importing file ``importer`` (repo-relative). A relative import (``level`` dots) starts at
    the importer's package; an absolute one is tried from the importer's directory up to the repo
    root (nearest first), so ``from app.x import y`` works in a repo that holds the project in a
    subdirectory. Not a repo file (a library) → None."""
    parts = [p for p in module.split(".") if p]
    here = Path(importer).parent
    if level > 0:
        bases = [here.parents[level - 2] if level > 1 else here] if level - 1 <= len(here.parents) else []
    else:
        bases = [here, *here.parents]
    for base in bases:
        target = repo_root / base / Path(*parts) if parts else repo_root / base
        for cand in (target.with_suffix(".py") if parts else None, target / "__init__.py"):
            if cand is not None and cand.is_file():
                return cand.relative_to(repo_root).as_posix()
    return None


def import_refs(root: Node, source: bytes, repo_root: Path, rel: str) -> dict[str, tuple[str, ...]]:
    """Local name → what it refers to in the repo: ``("module", file)`` or
    ``("symbol", file, name)``. Library imports are left out."""
    out: dict[str, tuple[str, ...]] = {}
    for node in root.named_children:
        if node.type == "import_from_statement":
            mod = node.child_by_field_name("module_name")
            if mod is None:
                continue
            if mod.type == "relative_import":
                prefix = next((c for c in mod.named_children if c.type == "import_prefix"), None)
                level = len(node_text(prefix, source)) if prefix is not None else 0
                dotted = next((c for c in mod.named_children if c.type == "dotted_name"), None)
                module = node_text(dotted, source) if dotted is not None else ""
            else:
                level, module = 0, node_text(mod, source)
            for name_node in node.named_children:
                if name_node is mod or name_node.type not in ("dotted_name", "aliased_import"):
                    continue
                orig = name_node.child_by_field_name("name") if name_node.type == "aliased_import" else name_node
                alias = name_node.child_by_field_name("alias") if name_node.type == "aliased_import" else name_node
                if orig is None or alias is None:
                    continue
                symbol, local = node_text(orig, source), node_text(alias, source)
                sub = module_file(repo_root, rel, f"{module}.{symbol}" if module else symbol, level)
                if sub is not None:  # from pkg import mod — a submodule
                    out[local] = ("module", sub)
                    continue
                owner = module_file(repo_root, rel, module, level)
                if owner is not None:  # from pkg.mod import router — a name in that module
                    out[local] = ("symbol", owner, symbol)
        elif node.type == "import_statement":
            for name_node in node.named_children:
                if name_node.type != "aliased_import":
                    continue  # `import pkg.mod` binds `pkg`, not the module — not followed
                orig, alias = name_node.child_by_field_name("name"), name_node.child_by_field_name("alias")
                target = module_file(repo_root, rel, node_text(orig, source), 0) if orig else None
                if target is not None and alias is not None:
                    out[node_text(alias, source)] = ("module", target)
    return out


class Origins:
    """Where each imported name of one file comes from, as a dotted path — library and repo
    imports alike — so a name can be checked against the module that defines it::

        from django.contrib.auth.decorators import login_required as lr   # lr → django.contrib.auth.decorators.login_required
        from rest_framework import permissions                            # permissions → rest_framework.permissions
        import flask_login                                                # flask_login → flask_login

    A relative import's origin starts with ``.`` so it never matches a library name.
    ``star`` holds the modules imported with ``from m import *``."""

    def __init__(self, root: Node, source: bytes) -> None:
        self.names: dict[str, str] = {}
        self.star: set[str] = set()
        for node in root.named_children:
            if node.type == "import_from_statement":
                mod = node.child_by_field_name("module_name")
                if mod is None:
                    continue
                module = node_text(mod, source)
                if any(c.type == "wildcard_import" for c in node.named_children):
                    self.star.add(module)
                for name_node in node.named_children:
                    if name_node is mod or name_node.type not in ("dotted_name", "aliased_import"):
                        continue
                    orig = name_node.child_by_field_name("name") if name_node.type == "aliased_import" else name_node
                    alias = name_node.child_by_field_name("alias") if name_node.type == "aliased_import" else name_node
                    if orig is not None and alias is not None:
                        sep = "" if module.endswith(".") else "."
                        self.names[node_text(alias, source)] = f"{module}{sep}{node_text(orig, source)}"
            elif node.type == "import_statement":
                for name_node in node.named_children:
                    if name_node.type == "aliased_import":
                        orig, alias = name_node.child_by_field_name("name"), name_node.child_by_field_name("alias")
                        if orig is not None and alias is not None:
                            self.names[node_text(alias, source)] = node_text(orig, source)
                    elif name_node.type == "dotted_name":
                        top = node_text(name_node, source).split(".", 1)[0]
                        self.names.setdefault(top, top)

    def qualified(self, written: str) -> str | None:
        """The dotted origin of a name as written (``permissions.IsAuthenticated`` →
        ``rest_framework.permissions.IsAuthenticated``); None when its first part is not imported
        (a name of this file, or unknown)."""
        head, _, rest = written.partition(".")
        origin = self.names.get(head)
        if origin is None:
            return None
        return f"{origin}.{rest}" if rest else origin

    def imports_package(self, package: str) -> bool:
        """Whether the file imports ``package`` or one of its modules."""
        return any(o == package or o.startswith(package + ".")
                   for o in (*self.names.values(), *self.star))
