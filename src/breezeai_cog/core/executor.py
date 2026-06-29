"""Parallel parse fan-out (ARCHITECTURE.md §5).

Spawn-safe (Linux/macOS/Windows): module-level worker functions, a ``Manager().Queue``
for log funneling, and picklable ``initargs``. Each worker loads grammars lazily
(cached per process) after ``discover_builtin`` registers parsers in the pool
initializer; files are submitted in batches and collected in completion order.
Per-file failures (incl. the tree-sitter timeout ``ValueError``) are isolated.
"""

from __future__ import annotations

import logging
import math
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from logging.handlers import QueueListener
from pathlib import Path
from typing import Iterator

from ..logging import get_logger
from ..schemas import FileRecord
from .scanner import ScanEntry

# Force spawn: cross-platform (matches macOS/Windows) and avoids the
# fork()-in-a-multithreaded-process deprecation/deadlock risk on Linux 3.13+.
_SPAWN = mp.get_context("spawn")

_WORKER: dict[str, object] = {}


def _options(settings) -> dict:
    return {
        "capture_statements": settings.capture_statements,
        "text_truncation_limit": settings.text_truncation_limit,
        "parse_timeout_micros": int(settings.parse_timeout * 1_000_000),
        "log_format": settings.log_format,
        "log_level": settings.log_level,
    }


def _parse_entry(path: str, repo_root: str, options: dict) -> FileRecord | None:
    from ..parsers.base import ParseContext
    from .registry import parser_for

    parser = parser_for(path)
    if parser is None:  # pragma: no cover - classify already filtered
        return None
    abs_path = os.path.join(repo_root, path)
    try:
        with open(abs_path, "rb") as fh:
            source = fh.read()
        ctx = ParseContext(
            path=path,
            abs_path=Path(abs_path),
            source=source,
            repo_root=Path(repo_root),
            capture_statements=options["capture_statements"],
            text_truncation_limit=options["text_truncation_limit"],
            parse_timeout_micros=options["parse_timeout_micros"],
            resolution_index=options.get("indexes", {}).get(parser.name),
        )
        return parser.parse_file(ctx)
    except Exception as exc:  # per-file isolation (incl. parse timeout)
        get_logger("breezeai_cog.worker").warning(
            "parse.file.failed", path=path, parser=getattr(parser, "name", None), error=str(exc)
        )
        return None


def _init_worker(log_queue: object, repo_root: str, options: dict) -> None:
    from .registry import discover_builtin

    discover_builtin()
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
    entries: list[ScanEntry], repo_root: str | Path, settings, indexes: dict | None = None
) -> Iterator[tuple[str, FileRecord]]:
    """Yield ``(language, FileRecord)`` — parallel across processes, sequential when
    ``jobs == 1`` or there's nothing to parallelize. ``indexes`` maps parser-name →
    repo-level ``build_index`` result (threaded into each ParseContext)."""
    repo_root = str(Path(repo_root))
    options = _options(settings)
    options["indexes"] = indexes or {}
    jobs = settings.jobs if settings.jobs and settings.jobs > 0 else (os.cpu_count() or 1)

    if jobs <= 1 or len(entries) <= 1:
        for entry in entries:
            record = _parse_entry(entry.path, repo_root, options)
            if record is not None:
                yield record.language, record  # the base language, not the parser name
        return

    queue, state = _start_listener()
    try:
        with ProcessPoolExecutor(
            max_workers=jobs, mp_context=_SPAWN,
            initializer=_init_worker, initargs=(queue, repo_root, options),
        ) as pool:
            futures = [pool.submit(_parse_batch, batch) for batch in _chunk(entries, jobs)]
            for future in as_completed(futures):
                yield from future.result()
    finally:
        _stop_listener(state)
