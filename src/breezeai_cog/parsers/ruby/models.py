"""ActiveRecord model discovery — the positive evidence behind Ruby data-access hints.

A Ruby data-access call is only distinguishable from ordinary code by *what the receiver
is*: ``User.find(1)`` is a database read, ``Tempfile.create`` is not, and nothing in the
call's own syntax separates them. Capitalization cannot decide it (every Ruby class name is
capitalized) and a denylist of known non-models cannot either — it has to enumerate every
gem and stdlib constant in existence to stay correct.

So this module answers the question from the declarations instead: a constant is a model
when its class **transitively extends** ``ActiveRecord::Base`` (directly, or through the
conventional ``ApplicationRecord`` intermediate). That answer lives in the model file, which
is a *different* file from the controller that queries it, so it is computed once per
repository in ``ruby.index.build_ruby_index`` (the ``build_index`` pre-pass) and handed to the
shared classifier as ``typed_db_ids`` — the same positive-evidence gate TypeScript uses for
``Repository<T>``-typed fields.

:func:`model_receivers` then adds the per-scope half: local variables and instance variables
assigned from a model constant (``user = User.find(id)``), parameter type hints
(``def deactivate(user: User)`` / Sorbet / YARD), association readers (``account.save`` in
a model with ``belongs_to :account``), association parameters, block arguments, and
explicit ``self`` in model instance methods, so instance writes (``user.save``) have
genuine evidence too.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Self

from tree_sitter import Node

from ..index_common import parallel_map
from ..treesitter import node_text, parse_source

#: Roots of the ActiveRecord hierarchy. ``ApplicationRecord`` is the Rails-generated
#: intermediate; it is seeded as a root so a repo that never declares it (engines, or a
#: model file parsed on its own) still resolves its subclasses.
_AR_ROOTS = frozenset({"ActiveRecord::Base", "ApplicationRecord"})

#: Call names that produce a model *instance* (or relation) from a model constant, so the
#: assigned variable carries the model's identity: ``user = User.find(id)``.
_MODEL_PRODUCERS = frozenset({
    "find", "find_by", "find_by!", "where", "new", "create", "create!", "first", "last",
    "build", "find_or_create_by", "find_or_initialize_by", "take", "all",
})

_ASSOC_KINDS = frozenset({"has_many", "belongs_to", "has_one", "has_and_belongs_to_many"})

_COLLECTION_ITERATORS = frozenset({
    "each", "find_each", "each_with_index", "filter", "select", "find_all", "map", "collect",
})


class RubyModelIndex(frozenset[str]):
    """The ActiveRecord model constants, carrying proven associations."""

    associations: dict[str, dict[str, tuple[str, str]]]

    def __new__(
        cls,
        models: set[str] | frozenset[str],
        associations: dict[str, dict[str, tuple[str, str]]] | None = None,
    ) -> Self:
        instance = super().__new__(cls, models)
        instance.associations = associations or {}
        return instance

    def __reduce__(self) -> tuple[type, tuple[set[str], dict[str, dict[str, tuple[str, str]]]]]:
        return (RubyModelIndex, (set(self), self.associations))


def _singularize(name: str) -> str:
    """Best-effort inflector singularization for conventional Rails association names."""
    if name.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    if name.endswith("sses"):
        return name[:-2]
    if name.endswith(("ches", "shes", "xes", "zes")):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def _camelize(name: str) -> str:
    """Convert snake_case to CamelCase: user_session -> UserSession."""
    return "".join(part.capitalize() for part in name.split("_"))


def _class_name(node: Node, source: bytes) -> str | None:
    """Declared name of a ``class`` node — ``User`` or ``Admin::User``."""
    name = node.child_by_field_name("name")
    if name is None or name.type not in {"constant", "scope_resolution"}:
        return None
    return node_text(name, source)


def _superclass_name(node: Node, source: bytes) -> str | None:
    """Base-class name of a ``class`` node, with the grammar's ``<`` token stripped.

    The ``superclass`` field text includes the ``<`` (``"< ApplicationRecord"``), so it is
    stripped here rather than at each call site.
    """
    superclass = node.child_by_field_name("superclass")
    if superclass is None:
        return None
    text = node_text(superclass, source).lstrip("<").strip()
    return text or None


def _iter_classes(node: Node) -> Iterator[Node]:
    if node.type == "class":
        yield node
    for child in node.named_children:
        yield from _iter_classes(child)


def _qualified_name(node: Node, source: bytes) -> str | None:
    """Declared name prefixed by every enclosing class/module scope.

    A nested class must not be recorded under its simple name: mastodon declares an
    ``ActiveModelSerializers`` class ``MediaAttachment`` inside ``Translation`` *and* an
    ActiveRecord model ``MediaAttachment`` at the top level. Recording both as
    ``MediaAttachment`` makes the name look ambiguous and drops the real model. Mirrors the
    nested-type qualification every other language parser applies to node ids.
    """
    name = _class_name(node, source)
    if name is None:
        return None
    scopes: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in {"class", "module"}:
            outer = _class_name(current, source)
            if outer is not None:
                scopes.append(outer)
        current = current.parent
    return "::".join([*reversed(scopes), name])


def declared_class_names(root: Node, source: bytes) -> list[str]:
    """Qualified names for all class declarations, including classes without a base."""
    return [
        name
        for node in _iter_classes(root)
        if (name := _qualified_name(node, source)) is not None
    ]


def declared_parents(root: Node, source: bytes) -> list[tuple[str, str]]:
    """``(qualified_name, superclass_name)`` for every class that names a base."""
    pairs: list[tuple[str, str]] = []
    for node in _iter_classes(root):
        name = _qualified_name(node, source)
        parent = _superclass_name(node, source)
        if name is not None and parent is not None:
            pairs.append((name, parent))
    return pairs


def _resolve_superclass(
    class_name: str, superclass: str, declarations: set[str]
) -> str:
    """Resolve a superclass reference against its Ruby lexical namespace."""
    if superclass.startswith("::"):
        return superclass[2:]

    namespace = class_name.split("::")[:-1]
    for depth in range(len(namespace), 0, -1):
        candidate = "::".join([*namespace[:depth], superclass])
        if candidate in declarations or candidate in _AR_ROOTS:
            return candidate

    if superclass in declarations or superclass in _AR_ROOTS:
        return superclass
    return superclass


def resolve_model_names(
    parent_pairs: list[tuple[str, str]], class_names: list[str]
) -> frozenset[str]:
    """Return only class identities proven to inherit from an ActiveRecord root.

    Relative bases are resolved lexically before the graph is walked. A declaration with
    conflicting inheritance evidence, or a chain through such a declaration, is omitted.
    """
    declarations = set(class_names)
    parents: dict[str, set[str]] = {}
    for class_name, raw_base in parent_pairs:
        base = _resolve_superclass(class_name, raw_base, declarations)
        parents.setdefault(class_name, set()).add(base)

    def status(name: str, visiting: frozenset[str] = frozenset()) -> bool | None:
        if name in _AR_ROOTS:
            return True
        if name in visiting:
            return None
        bases = parents.get(name)
        if not bases:
            return False
        outcomes = {
            status(base, visiting | {name}) if base in parents or base in _AR_ROOTS else False
            for base in bases
        }
        if outcomes == {True}:
            return True
        if outcomes == {False}:
            return False
        return None

    return frozenset(name for name in declarations if status(name) is True)


def model_receiver_ids(
    model_names: set[str] | frozenset[str], class_names: list[str]
) -> frozenset[str]:
    """Include only unambiguous short aliases, retaining qualified model identities."""
    declarations_by_terminal: dict[str, set[str]] = {}
    for name in class_names:
        declarations_by_terminal.setdefault(name.rsplit("::", 1)[-1], set()).add(name)

    ids: set[str] = set()
    for name in model_names:
        terminal = name.rsplit("::", 1)[-1]
        if "::" not in name and len(declarations_by_terminal.get(terminal, ())) > 1:
            # Keep the top-level model addressable only by an explicit absolute constant;
            # an unqualified terminal is ambiguous with the other declaration(s).
            ids.add(f"::{name}")
        else:
            ids.add(name)
        if len(declarations_by_terminal.get(terminal, ())) == 1:
            ids.add(terminal)
        if "::" in name:
            ids.add(f"::{name}")
    return frozenset(ids)


def model_class_identity(
    class_name: str, class_names: list[str]
) -> str:
    """Return the class receiver identity used for model-scope evidence."""
    same_terminal = {
        name for name in class_names if name.rsplit("::", 1)[-1] == class_name.rsplit("::", 1)[-1]
    }
    if "::" not in class_name and (
        f"::{class_name}" in class_names or len(same_terminal) > 1
    ):
        return f"::{class_name}"
    return class_name


def declared_associations(root: Node, source: bytes) -> list[tuple[str, str, str, str]]:
    """``(class_qualified_name, assoc_name, kind, target_model)`` for association calls."""
    out: list[tuple[str, str, str, str]] = []
    for node in _iter_classes(root):
        cls_name = _qualified_name(node, source)
        if cls_name is None:
            continue
        body = node.child_by_field_name("body")
        if body is None:
            continue
        for child in body.named_children:
            if child.type != "call":
                continue
            meth = child.child_by_field_name("method")
            if meth is None:
                continue
            kind = node_text(meth, source)
            if kind not in _ASSOC_KINDS:
                continue
            args = child.child_by_field_name("arguments")
            if args is None or not args.named_children:
                continue
            arg0 = args.named_children[0]
            assoc_name = node_text(arg0, source).lstrip(":").strip("\"'")
            target: str | None = None
            for arg in args.named_children[1:]:
                if arg.type == "pair":
                    k = arg.child_by_field_name("key")
                    v = arg.child_by_field_name("value")
                    if k is not None and v is not None and node_text(k, source).rstrip(":") == "class_name":
                        target = node_text(v, source).strip("\"'").lstrip(":")
            if not target:
                if kind in {"has_many", "has_and_belongs_to_many"}:
                    target = _camelize(_singularize(assoc_name))
                else:
                    target = _camelize(assoc_name)
            if target:
                out.append((cls_name, assoc_name, kind, target))
    return out


def _file_info(
    args: tuple[Path, Path, int]
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str]], list[str]]:
    """Picklable per-file worker for :func:`build_model_index` (one parse per file).

    A file that cannot be read or parsed contributes nothing — a missing model is a known
    gap, whereas failing the pre-pass would block the whole repository.

    O2 fix: Uses the caller-supplied ``timeout_micros`` so pathological files are
    time-bounded rather than running with no timeout (``timeout=0``).
    O5 fix: Catches the broader ``Exception`` class (not just ``ValueError``) so large or
    malformed files that trigger ``MemoryError`` or ``RecursionError`` inside the parser do
    not crash the pool worker. ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) is
    intentionally *not* caught so cancellation propagates correctly.
    """
    _repo_root, abs_path, timeout_micros = args
    try:
        source = abs_path.read_bytes()
    except OSError:
        return [], [], []
    if b"class" not in source:
        return [], [], []
    try:
        root = parse_source("ruby", source, timeout_micros).root_node
    except Exception:  # noqa: BLE001 — O5: robustness, not swallowing programming errors
        return [], [], []
    return (
        declared_parents(root, source),
        declared_associations(root, source),
        declared_class_names(root, source),
    )


def _file_parents(args: tuple[Path, Path]) -> list[tuple[str, str]]:
    """Legacy helper preserved for backward compatibility."""
    parents, _, _ = _file_info((*args, 0))
    return parents


def resolve_models(parents: dict[str, str]) -> frozenset[str]:
    """Return ActiveRecord model names from an already-resolved inheritance graph."""
    pairs = list(parents.items())
    return resolve_model_names(pairs, list(parents))


def build_model_index(
    repo_root: Path, files: Sequence[Path], jobs: int = 1
) -> RubyModelIndex:
    """Repo-level pre-pass: every constant and association that names an ActiveRecord model.

    Runs through the shared :func:`parallel_map` so it honours ``--jobs`` (serial at
    ``jobs<=1``) like the parse stage, and reduces order-independently — the result is the
    same set regardless of how the workers interleave.
    """
    root = Path(repo_root)
    # Import lazily to avoid the parser -> models -> parser import cycle. Use the parser's
    # source extensions and exact filename entries so indexing follows discovery support.
    from .parser import RubyParser

    ruby_extensions = RubyParser.extensions
    ruby_files = [
        f for f in files if f.suffix in ruby_extensions or f.name in ruby_extensions
    ]
    known_files = set(ruby_files)
    ruby_parser = RubyParser()
    for path in files:
        if path in known_files or path.suffix:
            continue
        try:
            with path.open("rb") as source_file:
                prefix = source_file.readline(4096)
        except OSError:
            continue
        if ruby_parser.matches_source(path, prefix):
            ruby_files.append(path)
    if not ruby_files:
        return RubyModelIndex(frozenset(), {})
    per_file = parallel_map(
        [(root, f, 0) for f in ruby_files], _file_info, jobs
    )
    parent_pairs: list[tuple[str, str]] = []
    class_names: list[str] = []
    all_assocs: list[tuple[str, str, str, str]] = []
    for pairs, assocs, names in per_file:
        parent_pairs.extend(pairs)
        all_assocs.extend(assocs)
        class_names.extend(names)
    models = resolve_model_names(parent_pairs, class_names)
    all_models = model_receiver_ids(models, class_names)

    by_model_assoc: dict[str, dict[str, tuple[str, str]]] = {}
    for cls_name, assoc_name, kind, target_model in all_assocs:
        if cls_name in all_models or cls_name.rsplit("::", 1)[-1] in all_models:
            resolved_target: str | None = None
            if "::" in cls_name:
                ns = cls_name.rsplit("::", 1)[0]
                if f"{ns}::{target_model}" in all_models:
                    resolved_target = f"{ns}::{target_model}"
            if not resolved_target:
                if target_model in all_models:
                    resolved_target = target_model
                elif target_model.rsplit("::", 1)[-1] in all_models:
                    resolved_target = target_model.rsplit("::", 1)[-1]
            if resolved_target:
                by_model_assoc.setdefault(cls_name, {})[assoc_name] = (kind, resolved_target)
                simple_cls = cls_name.rsplit("::", 1)[-1]
                if simple_cls not in by_model_assoc:
                    by_model_assoc[simple_cls] = by_model_assoc[cls_name]

    return RubyModelIndex(all_models, by_model_assoc)


def extract_param_types(method_node: Node, source: bytes) -> dict[str, str]:
    """Extract declared parameter type hints from keywords, Sorbet sigs, YARD, or RBS."""
    types: dict[str, str] = {}
    parent = method_node.parent
    if parent is not None:
        children = parent.named_children
        if method_node in children:
            idx = children.index(method_node)
            preceding = list(children[:idx])
            if parent.type == "body_statement" and parent.parent is not None:
                scope_children = parent.parent.named_children
                if parent in scope_children:
                    preceding = [
                        *scope_children[:scope_children.index(parent)],
                        *preceding,
                    ]
            for sib in reversed(preceding):
                if sib.type == "comment":
                    t = node_text(sib, source)
                    m_yard = re.search(
                        r"@param\s+(?:\[([A-Za-z0-9_:]+)\]\s+([a-z0-9_]+)|([a-z0-9_]+)\s+\[([A-Za-z0-9_:]+)\])",
                        t,
                    )
                    if m_yard:
                        typ = m_yard.group(1) or m_yard.group(4)
                        nm = m_yard.group(2) or m_yard.group(3)
                        types[nm] = typ
                    m_rbs = re.search(r"#:\s*\((?:([a-z0-9_]+):\s*)?([A-Z][A-Za-z0-9_:]*)", t)
                    if m_rbs:
                        nm = m_rbs.group(1)
                        typ = m_rbs.group(2)
                        if nm:
                            types[nm] = typ
                elif sib.type == "call":
                    m_node = sib.child_by_field_name("method")
                    if m_node is not None and node_text(m_node, source) == "sig":
                        def find_params(n: Node) -> None:
                            if n.type == "call":
                                m = n.child_by_field_name("method")
                                if m is not None and node_text(m, source) == "params":
                                    args = n.child_by_field_name("arguments")
                                    if args is not None:
                                        for pair in args.named_children:
                                            if pair.type == "pair":
                                                k = pair.child_by_field_name("key")
                                                v = pair.child_by_field_name("value")
                                                if k is not None and v is not None:
                                                    types[node_text(k, source).rstrip(":")] = node_text(v, source)
                            for c in n.named_children:
                                find_params(c)

                        find_params(sib)
                else:
                    break

    params_node = method_node.child_by_field_name("parameters")
    return types
def models_from_parents(per_file: Sequence[list[tuple[str, str]]]) -> frozenset[str]:
    """Reduce per-file ``(class, superclass)`` pairs to the model constants (order-independent)."""
    by_name: dict[str, set[str]] = {}
    for pairs in per_file:
        for name, base in pairs:
            by_name.setdefault(name, set()).add(base)
    flat = {name: next(iter(bases)) for name, bases in by_name.items()}
    verdicts: dict[str, set[bool]] = {}
    for name, bases in by_name.items():
        for base in bases:
            probe = dict(flat)
            probe[name] = base
            verdicts.setdefault(name, set()).add(name in resolve_models(probe))
    models = {n for n, v in verdicts.items() if v == {True}}
    # A model's terminal segment is a valid receiver too (`Admin::User` is written `User`
    # inside `module Admin`) — but never when that bare name is itself a declared non-model.
    non_models = {n for n, v in verdicts.items() if True not in v}
    terminals = {m.rsplit("::", 1)[-1] for m in models} - non_models
    return frozenset(models | terminals)


def model_receivers(
    root: Node,
    source: bytes,
    models: frozenset[str] | set[str],
    associations: dict[str, dict[str, tuple[str, str]]] | None = None,
    enclosing_class: str | None = None,
    is_instance_method: bool = False,
) -> frozenset[str]:
    """Receivers in one scope that carry a proven ActiveRecord model's identity.

    Resolves receivers from:
    1. The model constants themselves.
    2. Enclosing model scope (explicit ``self`` in model instance methods, plus reader methods
       for proven ``belongs_to`` / ``has_one`` associations).
    3. Method parameters with type hints (keyword params, Sorbet sigs, YARD @param, RBS).
    4. Method parameters matching enclosing model associations.
    5. Block arguments iterating over model associations (``sessions.each do |session|``).
    6. Local variables assigned from model producers (``user = User.find(id)``) or associations
       (``account = user.account``, ``session = sessions.build``).
    """
    if not models:
        return frozenset()
    names: set[str] = set(models)
    var_models: dict[str, str] = {}
    assocs = associations or {}

    is_model = enclosing_class is not None and (
        enclosing_class in models or enclosing_class.rsplit("::", 1)[-1] in models
    )
    class_assocs: dict[str, tuple[str, str]] = {}
    if is_model and enclosing_class is not None:
        model_name = enclosing_class if enclosing_class in models else enclosing_class.rsplit("::", 1)[-1]
        if is_instance_method:
            names.add("self")
            var_models["self"] = model_name
        class_assocs = assocs.get(enclosing_class) or assocs.get(enclosing_class.rsplit("::", 1)[-1]) or {}
        for assoc_name, (kind, target) in class_assocs.items():
            if (target in models or target.rsplit("::", 1)[-1] in models) and kind in {"belongs_to", "has_one"}:
                names.add(assoc_name)
                var_models[assoc_name] = target
                names.add(f"self.{assoc_name}")

    if root.type in {"method", "singleton_method"}:
        param_types = extract_param_types(root, source)
        for p_name, p_type in param_types.items():
            if p_type in models or p_type.rsplit("::", 1)[-1] in models:
                names.add(p_name)
                var_models[p_name] = p_type
        if is_model and class_assocs:
            params_node = root.child_by_field_name("parameters")
            if params_node is not None:
                for child in params_node.named_children:
                    p_name = None
                    if child.type == "identifier":
                        p_name = node_text(child, source)
                    elif child.type in {"optional_parameter", "keyword_parameter"}:
                        nm = child.child_by_field_name("name")
                        if nm is not None:
                            p_name = node_text(nm, source)
                    if p_name and p_name not in var_models:
                        if p_name in class_assocs:
                            _kind, target = class_assocs[p_name]
                            if target in models or target.rsplit("::", 1)[-1] in models:
                                names.add(p_name)
                                var_models[p_name] = target
                        else:
                            for assoc_name, (kind, target) in class_assocs.items():
                                if (
                                    kind in {"has_many", "has_and_belongs_to_many"}
                                    and p_name == _singularize(assoc_name)
                                    and (target in models or target.rsplit("::", 1)[-1] in models)
                                ):
                                    names.add(p_name)
                                    var_models[p_name] = target
                                    break

    def _add_model_var(var_name: str, model_type: str) -> None:
        names.add(var_name)
        var_models[var_name] = model_type
        v_assocs = assocs.get(model_type) or assocs.get(model_type.rsplit("::", 1)[-1]) or {}
        for a_name, (_k, tgt) in v_assocs.items():
            if tgt in models or tgt.rsplit("::", 1)[-1] in models:
                names.add(f"{var_name}.{a_name}")

    for v_name, m_type in list(var_models.items()):
        _add_model_var(v_name, m_type)

    def visit(node: Node) -> None:
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if (
                left is not None
                and right is not None
                and left.type in {"identifier", "instance_variable", "class_variable"}
                and right.type == "call"
            ):
                rec = right.child_by_field_name("receiver")
                verb_node = right.child_by_field_name("method")
                if rec is not None and verb_node is not None:
                    verb = node_text(verb_node, source)
                    left_name = node_text(left, source)
                    if rec.type in {"constant", "scope_resolution"}:
                        base = node_text(rec, source).lstrip(":")
                        if (base in models or base.rsplit("::", 1)[-1] in models) and verb in _MODEL_PRODUCERS:
                            _add_model_var(left_name, base)
                    elif rec.type == "identifier":
                        base = node_text(rec, source)
                        if base in var_models:
                            rec_model = var_models[base]
                            rec_assocs = assocs.get(rec_model) or assocs.get(rec_model.rsplit("::", 1)[-1]) or {}
                            if verb in rec_assocs:
                                _kind, target = rec_assocs[verb]
                                if target in models or target.rsplit("::", 1)[-1] in models:
                                    _add_model_var(left_name, target)
                    elif rec.type == "call":
                        inner_rec = rec.child_by_field_name("receiver")
                        inner_meth = rec.child_by_field_name("method")
                        if inner_rec is not None and inner_meth is not None:
                            inner_base = node_text(inner_rec, source)
                            inner_assoc = node_text(inner_meth, source)
                            rec_model = var_models.get(inner_base) or (
                                enclosing_class if inner_base == "self" else None
                            )
                            if rec_model:
                                rec_assocs = assocs.get(rec_model) or assocs.get(rec_model.rsplit("::", 1)[-1]) or {}
                                if inner_assoc in rec_assocs and verb in _MODEL_PRODUCERS:
                                    _kind, target = rec_assocs[inner_assoc]
                                    if target in models or target.rsplit("::", 1)[-1] in models:
                                        _add_model_var(left_name, target)
        elif node.type in {"block", "do_block"}:
            call = node.parent
            if call is not None and call.type == "call":
                call_rec = call.child_by_field_name("receiver")
                call_meth = call.child_by_field_name("method")
                if call_meth is not None and node_text(call_meth, source) in _COLLECTION_ITERATORS:
                    target_model: str | None = None
                    if call_rec is not None:
                        if call_rec.type == "identifier":
                            rec_name = node_text(call_rec, source)
                            if enclosing_class:
                                c_assocs = assocs.get(enclosing_class) or assocs.get(enclosing_class.rsplit("::", 1)[-1]) or {}
                                if rec_name in c_assocs:
                                    _kind, target = c_assocs[rec_name]
                                    if target in models or target.rsplit("::", 1)[-1] in models:
                                        target_model = target
                        elif call_rec.type == "call":
                            rec_obj = call_rec.child_by_field_name("receiver")
                            rec_assoc = call_rec.child_by_field_name("method")
                            if rec_obj is not None and rec_assoc is not None:
                                obj_name = node_text(rec_obj, source)
                                assoc_name = node_text(rec_assoc, source)
                                obj_model = var_models.get(obj_name) or (
                                    enclosing_class if obj_name == "self" else None
                                )
                                if obj_model:
                                    rec_assocs = assocs.get(obj_model) or assocs.get(obj_model.rsplit("::", 1)[-1]) or {}
                                    if assoc_name in rec_assocs:
                                        _kind, target = rec_assocs[assoc_name]
                                        if target in models or target.rsplit("::", 1)[-1] in models:
                                            target_model = target
                    if target_model:
                        params = node.child_by_field_name("parameters")
                        if params is not None:
                            for p in params.named_children:
                                if p.type == "identifier":
                                    arg_name = node_text(p, source)
                                    _add_model_var(arg_name, target_model)
        for child in node.named_children:
            visit(child)

    visit(root)
    return frozenset(names)
