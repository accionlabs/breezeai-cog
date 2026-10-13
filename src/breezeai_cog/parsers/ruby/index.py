"""The Ruby repo index (``RubyParser.build_index``): facts declared in one file and needed in
another — ActiveRecord model constants (:mod:`.models`) and Rails controller callbacks
(:mod:`.controllers`). One parse per file feeds both."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..index_common import parallel_map, record_distinct
from ..treesitter import parse_source
from .controllers import Callback, controller_callbacks
from .models import declared_parents, models_from_parents

_FileFacts = tuple[list[tuple[str, str]], list[tuple[str, list[Callback]]]]


@dataclass(frozen=True)
class RubyIndex:
    #: Constants naming an ActiveRecord model (see ``models.py``).
    models: frozenset[str] = frozenset()
    #: Controller (qualified name) → its ``before_action`` callbacks. A controller declared in
    #: more than one file with different callbacks is dropped (ambiguous → no guards).
    controller_callbacks: dict[str, list[Callback]] = field(default_factory=dict)


def _file_facts(args: tuple[Path, Path]) -> _FileFacts:
    """Picklable per-file worker: class → superclass pairs and controller callbacks. A file that
    cannot be read or parsed contributes nothing."""
    _, abs_path = args
    try:
        source = abs_path.read_bytes()
    except OSError:
        return [], []
    if b"class" not in source:
        return [], []
    try:
        root = parse_source("ruby", source).root_node
    except ValueError:  # bounded-parse failure — skip this file only
        return [], []
    return declared_parents(root, source), controller_callbacks(root, source)


def build_ruby_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> RubyIndex:
    root = Path(repo_root)
    ruby_files = [f for f in files if f.suffix == ".rb"]
    if not ruby_files:
        return RubyIndex()
    per_file = parallel_map([(root, f) for f in ruby_files], _file_facts, jobs)
    callbacks: dict[str, list[Callback] | None] = {}
    for _, controllers in per_file:
        for name, entries in controllers:
            record_distinct(callbacks, name, entries)
    return RubyIndex(
        models=models_from_parents([pairs for pairs, _ in per_file]),
        controller_callbacks={k: v for k, v in callbacks.items() if v is not None},
    )
