"""The Ruby repo index (``RubyParser.build_index``): facts declared in one file and needed in
another — ActiveRecord model constants (:mod:`.models`), Rails controller callbacks
(:mod:`.controllers`) and the registered facts of the Ruby files (``register_fact``). One parse
per file feeds them all."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from ..additive import (
    collect_additive, collect_fact_sources, facts_for, resolve_additive, split_fact_sources,
)
from ..index_common import parallel_map, record_distinct
from ..treesitter import parse_source
from .controllers import Callback, controller_callbacks
from .models import declared_parents, models_from_parents

_FileFacts = tuple[list[tuple[str, str]], list[tuple[str, list[Callback]]], dict[str, Any]]


@dataclass(frozen=True)
class RubyIndex:
    #: Constants naming an ActiveRecord model (see ``models.py``).
    models: frozenset[str] = frozenset()
    #: Controller (qualified name) → its ``before_action`` callbacks. A controller declared in
    #: more than one file with different callbacks is dropped (ambiguous → no guards).
    controller_callbacks: dict[str, list[Callback]] = field(default_factory=dict)
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)


def _file_facts(args: tuple[Path, Path]) -> _FileFacts:
    """Picklable per-file worker: class → superclass pairs, controller callbacks and what
    registered facts collect. A file that cannot be read or parsed contributes nothing."""
    repo_root, abs_path = args
    try:
        source = abs_path.read_bytes()
    except OSError:
        return [], [], {}
    has_class = b"class" in source
    if not has_class and not any(g in source for spec in facts_for("ruby") for g in spec.gate):
        return [], [], {}
    try:
        root = parse_source("ruby", source).root_node
    except ValueError:  # bounded-parse failure — skip this file only
        return [], [], {}
    facts = collect_additive("ruby", root, source, abs_path.relative_to(repo_root).as_posix(),
                             repo_root)
    if not has_class:
        return [], [], facts
    return declared_parents(root, source), controller_callbacks(root, source), facts


def build_ruby_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> RubyIndex:
    root = Path(repo_root)
    files, fact_files = split_fact_sources("ruby", list(files))
    ruby_files = [f for f in files if f.suffix == ".rb"]
    # registered fact name → per-file values: fact-source files (another parser's), then our own
    collected: dict[str, list[Any]] = collect_fact_sources("ruby", fact_files, root, jobs)
    if not ruby_files:
        return replace(RubyIndex(), facts=resolve_additive("ruby", collected, RubyIndex()))
    per_file = parallel_map([(root, f) for f in ruby_files], _file_facts, jobs)
    callbacks: dict[str, list[Callback] | None] = {}
    for _, controllers, facts in per_file:
        for name, value in facts.items():
            collected.setdefault(name, []).append(value)
        for name, entries in controllers:
            record_distinct(callbacks, name, entries)
    index = RubyIndex(
        models=models_from_parents([pairs for pairs, _, _ in per_file]),
        controller_callbacks={k: v for k, v in callbacks.items() if v is not None},
    )
    # Registered facts may need the whole index, so they resolve after it is built.
    return replace(index, facts=resolve_additive("ruby", collected, index))
