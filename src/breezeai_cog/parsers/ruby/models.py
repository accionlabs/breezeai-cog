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
repository in :func:`build_model_index` (the ``build_index`` pre-pass) and handed to the
shared classifier as ``typed_db_ids`` — the same positive-evidence gate TypeScript uses for
``Repository<T>``-typed fields.

:func:`model_receivers` then adds the per-file half: local variables and instance variables
assigned from a model constant (``user = User.find(id)``), so instance writes
(``user.save``) have evidence too.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Sequence

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
    text = node_text(superclass, source).lstrip("<").strip().lstrip(":")
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


def declared_parents(root: Node, source: bytes) -> list[tuple[str, str]]:
    """``(qualified_name, superclass_name)`` for every class in one file that names a base."""
    pairs: list[tuple[str, str]] = []
    for node in _iter_classes(root):
        name = _qualified_name(node, source)
        parent = _superclass_name(node, source)
        if name is not None and parent is not None:
            pairs.append((name, parent))
    return pairs


def _file_parents(args: tuple[Path, Path]) -> list[tuple[str, str]]:
    """Picklable per-file worker for :func:`build_model_index` (one parse per file).

    A file that cannot be read or parsed contributes nothing — a missing model is a known
    gap, whereas failing the pre-pass would block the whole repository.
    """
    repo_root, abs_path = args
    try:
        source = abs_path.read_bytes()
    except OSError:
        return []
    # Guard on `class`, not on an ActiveRecord marker: a file declaring an unrelated class
    # of the same name is what makes a name ambiguous, so it has to be seen even though it
    # names no AR base. Files with no class at all (scripts, config, bare modules) are the
    # only ones skipped.
    if b"class" not in source:
        return []
    try:
        root = parse_source("ruby", source).root_node
    except ValueError:  # bounded-parse failure — skip this file only
        return []
    return declared_parents(root, source)


def resolve_models(parents: dict[str, str]) -> frozenset[str]:
    """Constants whose class transitively reaches an ActiveRecord root.

    ``parents`` maps class name → base-class name. Walks each chain with a visited set so a
    cyclic or self-referential declaration terminates instead of recursing forever.
    """
    models: set[str] = set()
    for name in parents:
        seen: set[str] = set()
        current: str | None = name
        while current is not None and current not in seen:
            seen.add(current)
            base = parents.get(current)
            if base in _AR_ROOTS or current in _AR_ROOTS:
                models.add(name)
                break
            current = base
    # A model's terminal segment is also a valid receiver (`Admin::User` is written `User`
    # inside `module Admin`), so both forms are accepted.
    return frozenset(models | {m.rsplit("::", 1)[-1] for m in models})


def build_model_index(
    repo_root: Path, files: Sequence[Path], jobs: int = 1
) -> frozenset[str]:
    """Repo-level pre-pass: every constant that names an ActiveRecord model.

    Runs through the shared :func:`parallel_map` so it honours ``--jobs`` (serial at
    ``jobs<=1``) like the parse stage, and reduces order-independently — the result is the
    same set regardless of how the workers interleave.
    """
    root = Path(repo_root)
    ruby_files = [f for f in files if f.suffix == ".rb"]
    if not ruby_files:
        return frozenset()
    per_file = parallel_map(
        [(root, f) for f in ruby_files], _file_parents, jobs
    )
    # A name can be declared more than once: a model re-opened inside a migration or a CLI
    # task (routine in Rails), or a genuinely different class sharing the name. Conflicting
    # on the *base name* would drop the first kind, so the test is whether the declarations
    # disagree about **model-ness**: all-model is kept, all-not-model is excluded, and a
    # genuine disagreement is dropped (absent beats wrong). Order-independent — the verdict
    # does not depend on which worker finished first.
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
    root: Node, source: bytes, models: frozenset[str]
) -> frozenset[str]:
    """Receivers in one file that carry a model's identity.

    The model constants themselves, plus any local or instance variable assigned from one
    (``user = User.find(id)`` → ``user``), so an instance write (``user.save``) has the same
    positive evidence a class-level query does. An assignment from anything else — a gem, a
    literal, a method call on ``self`` — contributes nothing.
    """
    if not models:
        return frozenset()
    names: set[str] = set(models)

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
                receiver = right.child_by_field_name("receiver")
                method = right.child_by_field_name("method")
                if receiver is not None and method is not None:
                    base = node_text(receiver, source).lstrip(":")
                    verb = node_text(method, source)
                    if (
                        base in models or base.rsplit("::", 1)[-1] in models
                    ) and verb in _MODEL_PRODUCERS:
                        names.add(node_text(left, source))
        for child in node.named_children:
            visit(child)

    visit(root)
    return frozenset(names)
