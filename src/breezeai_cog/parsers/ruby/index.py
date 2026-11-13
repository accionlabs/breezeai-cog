"""The Ruby repo index (``RubyParser.build_index``): facts declared in one file and needed in
another — ActiveRecord model constants (:mod:`.models`) and the registered facts of the Ruby
files (``register_fact``, e.g. Rails controller callbacks). One parse per file feeds both.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..additive import (
    collect_additive,
    collect_fact_sources,
    facts_for,
    resolve_additive,
    split_fact_sources,
)
from ..index_common import parallel_map
from ..treesitter import parse_source
from .models import (
    RUBY_INDEX_PARSE_TIMEOUT_MICROS,
    _model_candidate_files,
    _model_index_from_info,
    declared_associations,
    declared_class_names,
    declared_parents,
)

_FileFacts = tuple[list[tuple[str, str]], dict[str, Any]]
_ModelInfo = tuple[list[tuple[str, str]], list[tuple[str, str, str, str]], list[str]]
_FileIndexData = tuple[_ModelInfo, _FileFacts]
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RubyIndex:
    #: Constants naming an ActiveRecord model (see ``models.py``).
    models: frozenset[str] = frozenset()
    #: Proven model associations, indexed by class and association name.
    associations: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)

    def __contains__(self, name: object) -> bool:
        return name in self.models

    def __iter__(self) -> Iterator[str]:
        return iter(self.models)


def _file_index_data(
    args: tuple[Path, Path, bool, bool, tuple[bytes, ...]],
) -> _FileIndexData:
    """Read and parse one file for whichever Ruby index consumers need it.

    Model indexing isolates all per-file exceptions. Facts indexing skips parse ``ValueError``
    but propagates other exceptions. This worker preserves that distinction while sharing a
    syntax tree whenever both consumers need the file.
    """
    repo_root, abs_path, model_candidate, facts_candidate, fact_gates = args
    empty: _FileIndexData = (([], [], []), ([], {}))
    try:
        source = abs_path.read_bytes()
    except OSError as exc:
        _logger.warning("Ruby index could not read %s: %s", abs_path, exc)
        return empty

    has_class = b"class" in source
    needs_model = model_candidate and has_class
    needs_facts = facts_candidate and (has_class or any(gate in source for gate in fact_gates))
    if not needs_model and not needs_facts:
        return empty

    try:
        root = parse_source("ruby", source, RUBY_INDEX_PARSE_TIMEOUT_MICROS).root_node
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Ruby index parse failed for %s: %s", abs_path, exc)
        return empty

    parent_pairs: list[tuple[str, str]] = []
    if has_class and (needs_model or needs_facts):
        try:
            parent_pairs = declared_parents(root, source)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Ruby index extraction failed for %s: %s", abs_path, exc)
            return empty

    model_info: _ModelInfo = ([], [], [])
    if needs_model:
        try:
            model_info = (
                parent_pairs,
                declared_associations(root, source),
                declared_class_names(root, source),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Ruby model indexing failed for %s: %s", abs_path, exc)
            model_info = ([], [], [])

    fact_info: _FileFacts = ([], {})
    if needs_facts:
        try:
            facts = collect_additive(
                "ruby",
                root,
                source,
                abs_path.relative_to(repo_root).as_posix(),
                repo_root,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Ruby fact indexing failed for %s: %s", abs_path, exc)
            facts = {}
        fact_info = (parent_pairs if has_class else [], facts)
    return model_info, fact_info


def build_ruby_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> RubyIndex:
    root = Path(repo_root)
    source_files = list(files)
    model_files = _model_candidate_files(source_files)
    own_files, fact_files = split_fact_sources("ruby", source_files)
    # Keep the original fact-index restriction: only exact lowercase .rb suffixes are own files.
    ruby_files = [path for path in own_files if path.suffix == ".rb"]
    # registered fact name → per-file values: fact-source files (another parser's), then our own
    collected: dict[str, list[Any]] = collect_fact_sources("ruby", fact_files, root, jobs)

    # Preserve the input ordering for each consumer while parsing their union only once.
    work_files = list(dict.fromkeys([*model_files, *ruby_files]))
    model_candidates = set(model_files)
    facts_candidates = set(ruby_files)
    fact_gates = tuple(gate for spec in facts_for("ruby") for gate in spec.gate)
    work = [
        (root, path, path in model_candidates, path in facts_candidates, fact_gates)
        for path in work_files
    ]
    per_file = parallel_map(work, _file_index_data, jobs)
    by_file = dict(zip(work_files, per_file))

    model_index = _model_index_from_info([by_file[path][0] for path in model_files])
    for _, facts in (by_file[path][1] for path in ruby_files):
        for name, value in facts.items():
            collected.setdefault(name, []).append(value)

    index = RubyIndex(
        models=frozenset(model_index),
        associations=model_index.associations,
    )
    # Registered facts may need the whole index, so they resolve after it is built.
    return replace(index, facts=resolve_additive("ruby", collected, index))
