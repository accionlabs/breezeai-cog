"""Parallel parse fan-out.

Spawn-safe (Linux/macOS/Windows): module-level worker functions, a ``Manager().Queue``
for log funneling, and picklable ``initargs``. The repo indexes reach the workers through a
private temp file, not ``initargs`` (see :func:`_worker_options`). Each worker loads grammars lazily
(cached per process) after ``discover_builtin`` registers parsers in the pool
initializer; files are submitted in batches and collected in completion order.
Per-file failures (incl. the tree-sitter timeout ``ValueError``) are isolated.
"""

from __future__ import annotations

import logging
import math
import multiprocessing as mp
import os
import pickle
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from logging.handlers import QueueListener
from pathlib import Path
from typing import Any, Iterator

from ..logging import get_logger
from ..schemas import FileRecord
from .scanner import ScanEntry

# Force spawn: cross-platform (matches macOS/Windows) and avoids the
# fork()-in-a-multithreaded-process deprecation/deadlock risk on Linux 3.13+.
_SPAWN = mp.get_context("spawn")

_WORKER: dict[str, object] = {}


def _options(settings) -> dict:
    from .registry import discover_builtin, template_extensions

    # The extensions the scanner is dropping this run — empty when --capture-templates is on.
    # Needed here (not just in the scanner) to prune edges that would otherwise point at a
    # File node that was never emitted; see _prune_template_edges.
    if settings.capture_templates:
        dropped: frozenset[str] = frozenset()
    else:
        discover_builtin()  # the main process may not have registered yet
        dropped = template_extensions()
    return {
        "capture_statements": settings.capture_statements,
        "template_extensions": dropped,
        "statement_text_limit": settings.statement_text_limit,
        "parse_timeout_micros": int(settings.parse_timeout * 1_000_000),
        "max_concat_depth": settings.max_concat_depth,
        "log_format": settings.log_format,
        "log_level": settings.log_level,
    }


def _prune_template_edges(record: FileRecord, dropped: frozenset[str]) -> None:
    """Drop cross-file edges pointing at a template the scanner skipped.

    A skipped template is still on disk, so a resolver that probes the filesystem happily
    binds to it — ``parsers/typescript/imports.py`` deliberately resolves ``./Avatar.vue``
    because "the Vue parser emits a real File node for it". With the template gate on that
    node is never emitted, so the edge would dangle. Pruning here (the single post-parse
    chokepoint shared by the worker pool, ``run_inprocess`` and ``iter_records``) covers
    every parser and any markup type added later, rather than patching each resolver.

    ``calls[].path`` is set to ``None`` rather than dropping the call — the call really did
    happen, only its target file is out of scope (honest-null, as elsewhere).
    """
    if not dropped:
        return
    def is_template(path: str) -> bool:
        return os.path.splitext(path)[1].lower() in dropped

    record.importFiles = [p for p in record.importFiles if not is_template(p)]
    for holder in (*record.functions, *record.classes):
        for call in getattr(holder, "calls", ()):
            if call.path is not None and is_template(call.path):
                call.path = None


def _parse_entry(path: str, repo_root: str, options: dict) -> FileRecord | None:
    from ..parsers.base import ParseContext
    from .registry import base_parser_for, select

    abs_path = os.path.join(repo_root, path)
    try:
        with open(abs_path, "rb") as fh:
            source = fh.read()
    except OSError:
        return None

    parser = select(
        path, source, options["parse_timeout_micros"]
    )  # exactly one parser per file (claims + priority)
    if parser is None:  # pragma: no cover - classify already filtered
        return None
    base = base_parser_for(path)
    index = options.get("indexes", {}).get(base.name if base is not None else "")
    from ..parsers.statements_common import (
        begin_concat_tracking,
        set_concat_depth,
        summarize_skipped_concats,
    )

    set_concat_depth(options.get("max_concat_depth"))  # apply configured cap in this process
    begin_concat_tracking()  # collect concats the fold cap skips, for one per-file summary
    try:
        ctx = ParseContext(
            path=path,
            abs_path=Path(abs_path),
            source=source,
            repo_root=Path(repo_root),
            capture_statements=options["capture_statements"],
            statement_text_limit=options["statement_text_limit"],
            parse_timeout_micros=options["parse_timeout_micros"],
            resolution_index=index,
        )
        record = parser.parse_file(ctx)
        parser.link_handlers(record, ctx)
    except Exception as exc:  # per-file isolation (incl. parse timeout)
        get_logger("breezeai_cog.worker").warning(
            "parse.file.failed", path=path, parser=parser.name, error=str(exc)
        )
        return None
    _prune_template_edges(record, options.get("template_extensions") or frozenset())
    project_index = options.get("nx_projects")
    if project_index is not None:
        project = project_index.project_for(record.path)
        if project is not None:
            record.project = project.name
            record.projectRoot = project.root
            if (record.metadata and record.metadata.get("kind") == "project.json"
                    and isinstance(record.metadata.get("projectInfo"), dict)):
                record.metadata["projectInfo"].setdefault("name", project.name)
                record.metadata["projectInfo"].setdefault("projectRoot", project.root)
    summary = summarize_skipped_concats(path)  # one clean, human-readable line if any
    if summary:
        get_logger("breezeai_cog.worker").warning(summary)
    return record


def _init_worker(log_queue: object, repo_root: str, options: dict) -> None:
    from .registry import discover_builtin

    discover_builtin()
    path = options.pop("indexes_path", None)
    if path is not None:
        with open(path, "rb") as fh:
            options["indexes"] = pickle.load(fh)
    _WORKER["repo_root"] = repo_root
    _WORKER["options"] = options
    if log_queue is not None:
        from ..logging import setup_worker_logging

        setup_worker_logging(log_queue, options["log_format"], options["log_level"])


def _parse_batch(batch: list[ScanEntry]) -> list[tuple[str, FileRecord]]:
    repo_root = _WORKER["repo_root"]  # type: ignore[assignment]
    options = _WORKER["options"]  # type: ignore[assignment]
    out: list[tuple[str, FileRecord]] = []
    for entry in batch:
        record = _parse_entry(entry.path, repo_root, options)  # type: ignore[arg-type]
        if record is not None:
            out.append((record.language, record))  # the base language, not the parser name
    return out


def _chunk(items: list[ScanEntry], jobs: int) -> list[list[ScanEntry]]:
    size = max(1, math.ceil(len(items) / (jobs * 4)))  # ~4 batches/worker for balance
    return [items[i:i + size] for i in range(0, len(items), size)]


@contextmanager
def _worker_options(options: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The ``options`` for the pool's ``initargs``, with the indexes moved to a temp file.

    A spawned worker gets its ``initargs`` through a pipe; once they outgrow the pipe buffer
    (~64 KB), the parent blocks on each worker until that worker has started Python and
    imported cog to read them, so the workers start one after another instead of together.
    The indexes are most of that size, so the parent writes them once to a file in a private
    temp directory and each worker loads them in ``_init_worker``. If the file can't be written
    (no writable temp dir), they stay in ``options``: slower to start, same result. The
    directory is removed once the pool has shut down."""
    if not options.get("indexes"):
        yield options
        return
    try:
        tmp = tempfile.mkdtemp(prefix="breezeai-cog-")  # owner-only (0700)
    except OSError:
        yield options
        return
    try:
        path = os.path.join(tmp, "indexes.pickle")
        try:
            with open(path, "wb") as fh:
                pickle.dump(options["indexes"], fh, protocol=pickle.HIGHEST_PROTOCOL)
        except OSError:
            yield options
            return
        yield {**options, "indexes": {}, "indexes_path": path}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _start_listener() -> tuple[object, tuple]:
    manager = _SPAWN.Manager()
    queue = manager.Queue()
    handlers = logging.getLogger("breezeai_cog").handlers
    listener = QueueListener(queue, *handlers, respect_handler_level=True)
    listener.start()
    return queue, (manager, listener)


def _stop_listener(state: tuple) -> None:
    manager, listener = state
    listener.stop()
    manager.shutdown()


def parse_entries(
    entries: list[ScanEntry], repo_root: str | Path, settings, indexes: dict | None = None,
    nx_projects=None,
) -> Iterator[tuple[str, FileRecord]]:
    """Yield ``(language, FileRecord)`` — parallel across processes, sequential when
    ``jobs == 1`` or there's nothing to parallelize. ``indexes`` maps parser-name →
    repo-level ``build_index`` result (threaded into each ParseContext)."""
    repo_root = str(Path(repo_root))
    options = _options(settings)
    options["indexes"] = indexes or {}
    options["nx_projects"] = nx_projects
    jobs = settings.jobs if settings.jobs and settings.jobs > 0 else (os.cpu_count() or 1)

    if jobs <= 1 or len(entries) <= 1:
        for entry in entries:
            record = _parse_entry(entry.path, repo_root, options)
            if record is not None:
                yield record.language, record  # the base language, not the parser name
        return

    queue, state = _start_listener()
    try:
        with _worker_options(options) as worker_options, ProcessPoolExecutor(
            max_workers=jobs, mp_context=_SPAWN,
            initializer=_init_worker, initargs=(queue, repo_root, worker_options),
        ) as pool:
            futures = [pool.submit(_parse_batch, batch) for batch in _chunk(entries, jobs)]
            for future in as_completed(futures):
                yield from future.result()
    finally:
        _stop_listener(state)
