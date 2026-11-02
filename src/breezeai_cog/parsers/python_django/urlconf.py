"""Where each urlconf is served, and the settings every view runs under: the ``django.urlconf``
fact on the Python index.

A pattern's address starts in another file: the root urlconf named by ``ROOT_URLCONF`` in a
settings module, then the ``include()``s that lead from it to the pattern's module::

    # config/settings.py      ROOT_URLCONF = "config.urls"
    # config/urls.py          path("api/", include("shop.urls"))
    # shop/urls.py            path("orders/", views.orders)        → /api/orders/

So each file contributes its includes, its ``ROOT_URLCONF`` and its settings
(:func:`file_urlconf`), and they are composed once per repository:

* a ``ROOT_URLCONF`` module is served from ``/``;
* an included module is served at its includer's address + the include's pattern;
* a module reached from no root, from two places (two includes, or a root that is also
  included), or through a pattern that is not a literal, has an unknown address (``None``);
* ``MIDDLEWARE`` holds only when every settings module that sets it agrees and none changes it
  afterwards (``MIDDLEWARE += …``, ``.append``, …): which settings module runs is chosen by the
  environment, so a value the modules disagree on is not certain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..python_common import Origins, field_text, import_refs, literal, string_list, top_statements
from ..treesitter import node_text
from .patterns import Part
from .urlpatterns import module_target, urlconf

FACT = "django.urlconf"

#: What an include points at: ``("module", file)`` or ``("router", file, variable)``.
Target = tuple[str, ...]
#: The settings read: name → value, or None when it is set but not a literal list.
SETTINGS = ("MIDDLEWARE",)


@dataclass
class FileUrlconf:
    """What one file contributes. Plain data (picklable)."""

    has_patterns: bool = False
    #: (the include's address within this module — None when unknown, what it includes).
    includes: list[tuple[tuple[Part, ...] | None, Target]] = field(default_factory=list)
    #: The file ``ROOT_URLCONF`` names, when this is a settings module.
    root: str | None = None
    #: Settings set here: name → value (None: not a literal, or changed after it is set).
    settings: dict[str, tuple[str, ...] | None] = field(default_factory=dict)


@dataclass(frozen=True)
class DjangoUrlconf:
    #: Urlconf module → the patterns above its own (root: ``()``); None when unknown.
    served: dict[str, tuple[Part, ...] | None] = field(default_factory=dict)
    #: Settings every settings module agrees on: name → value.
    settings: dict[str, tuple[str, ...]] = field(default_factory=dict)


def django_urlconf(index: Any | None) -> DjangoUrlconf | None:
    found: DjangoUrlconf | None = index_fact(index, FACT)
    return found


def include_target(target: tuple[str, object], source: bytes, rel: str, repo_root: Path | None,
                   imports: dict[str, tuple[str, ...]]) -> Target | None:
    """The repo module an include names (``"shop.urls"``, an imported ``shop_urls``), or None."""
    kind, value = target
    if kind == "module" and isinstance(value, str):
        file = module_target(value, rel, repo_root)
        return ("module", file) if file is not None else None
    if isinstance(value, Node):
        if value.type == "identifier":
            ref = imports.get(node_text(value, source))
            if ref is not None and ref[0] == "module":
                return ("module", ref[1])
        elif value.type == "attribute":  # shop.urls with `import shop` / `from shop import urls`
            obj = value.child_by_field_name("object")
            ref = imports.get(node_text(obj, source)) if obj is not None and obj.type == "identifier" else None
            if ref is not None and ref[0] == "module":
                attr = field_text(value, "attribute", source)
                sub = Path(ref[1])
                base = sub.parent if sub.name == "__init__.py" else None
                if base is not None and repo_root is not None and (repo_root / base / f"{attr}.py").is_file():
                    return ("module", (base / f"{attr}.py").as_posix())
    return None


_MUTATORS = frozenset({"append", "insert", "extend", "remove", "pop", "clear"})


def _settings(root: Node, source: bytes) -> tuple[str | None, dict[str, tuple[str, ...] | None]]:
    """(``ROOT_URLCONF`` as written, the settings this module sets). A setting assigned anywhere
    but at module level (``if DEBUG: MIDDLEWARE = …``), added to (``+=``) or changed in place
    (``MIDDLEWARE.append(…)``) is not certain: None."""
    root_urlconf: str | None = None
    values: dict[str, tuple[str, ...] | None] = {}
    top = set(top_statements(root))

    def walk(node: Node) -> None:
        nonlocal root_urlconf
        for child in node.named_children:
            if child.type in ("assignment", "augmented_assignment"):
                left = child.child_by_field_name("left")
                name = node_text(left, source) if left is not None and left.type == "identifier" else None
                if name == "ROOT_URLCONF" and child.type == "assignment" and child in top:
                    root_urlconf = literal(child.child_by_field_name("right"), source)
                elif name in SETTINGS:
                    items = string_list(child.child_by_field_name("right"), source) \
                        if child.type == "assignment" and child in top and name not in values else None
                    values[name] = tuple(items) if items is not None else None
            elif child.type == "call":
                fn = child.child_by_field_name("function")
                obj = fn.child_by_field_name("object") if fn is not None and fn.type == "attribute" else None
                attr = fn.child_by_field_name("attribute") if fn is not None and fn.type == "attribute" else None
                if obj is not None and obj.type == "identifier" and node_text(obj, source) in SETTINGS \
                        and attr is not None and node_text(attr, source) in _MUTATORS:
                    values[node_text(obj, source)] = None
            walk(child)

    walk(root)
    return root_urlconf, values


def file_urlconf(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileUrlconf:
    """The urlconf wiring and settings of one file (see :class:`FileUrlconf`)."""
    out = FileUrlconf()
    root_urlconf, out.settings = _settings(root, source)
    if root_urlconf is not None and repo_root is not None:
        out.root = module_target(root_urlconf, rel, repo_root)
    found = urlconf(root, source, Origins(root, source)) if b"urlpatterns" in source else None
    if found is not None:
        out.has_patterns = True
        imports = import_refs(root, source, repo_root, rel) if repo_root is not None else {}
        for inc in found[1]:
            target = include_target(inc.target, source, rel, repo_root, imports)
            if target is not None:
                out.includes.append((inc.prefix, target))
    return out


def resolve_urlconf(files: dict[str, FileUrlconf]) -> DjangoUrlconf:
    roots = {f.root for f in files.values() if f.root is not None}
    sites: dict[Target, list[tuple[str, tuple[Part, ...] | None]]] = {}
    for rel, f in files.items():
        for parts, target in f.includes:
            sites.setdefault(target, []).append((rel, parts))
    memo: dict[str, tuple[Part, ...] | None] = {}

    def served(module: str, seen: frozenset[str]) -> tuple[Part, ...] | None:
        if module in memo:
            return memo[module]
        at = sites.get(("module", module), [])
        result: tuple[Part, ...] | None
        if len(at) + (module in roots) != 1 or module in seen:
            result = None
        elif module in roots:
            result = ()
        else:
            parent, parts = at[0]
            base = served(parent, seen | {module})
            result = base + parts if base is not None and parts is not None else None
        if not seen:
            memo[module] = result
        return result

    modules = sorted({rel for rel, f in files.items() if f.has_patterns}
                     | {t[1] for t in sites if t[0] == "module"} | roots)
    settings: dict[str, tuple[str, ...]] = {}
    for name in SETTINGS:
        values = {f.settings[name] for f in files.values() if name in f.settings}
        if len(values) == 1 and None not in values:
            (value,) = values
            assert value is not None
            settings[name] = value
    return DjangoUrlconf(served={m: served(m, frozenset()) for m in modules}, settings=settings)


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> tuple[str, FileUrlconf] | None:
    facts = file_urlconf(root, source, rel, repo_root)
    if not (facts.has_patterns or facts.root or facts.settings):
        return None
    return rel, facts


def _resolve(values: list[tuple[str, FileUrlconf]], index: Any) -> DjangoUrlconf | None:
    return resolve_urlconf(dict(values)) if values else None


register_fact(FactSpec(
    name=FACT, language="python", gate=(b"urlpatterns", b"ROOT_URLCONF", b"MIDDLEWARE"),
    collect=_collect, resolve=_resolve,
))
