"""Structured logging — mirrors ``breezeai-ai-backend``'s ``app/core/logging.py``.

``structlog`` over stdlib ``logging``: two renderers (``plaintext`` / ``json``)
chosen by ``Settings.log_format``, ``contextvars`` for per-run / per-file context,
and a daily-dated + size-capped file handler (20 MB, 30-day retention). Standard
stdlib levels — no custom TRACE.

Differences from the sibling (a single async web process): ``setup_logging`` takes
an injected :class:`~breezeai_cog.config.Settings` (no module-level singleton), and
there is no JWT-identity / HTTP-body handling. Cross-process worker log funneling
(``QueueHandler`` → ``QueueListener``) is wired by the executor, not here.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from structlog.stdlib import LoggerFactory

if TYPE_CHECKING:
    from .config import Settings

HOSTNAME = socket.gethostname()
APP_NAME = "breezeai-cog"
APP_LOGGER = "breezeai_cog"
#: File-only sub-logger for high-volume, low-signal detail (e.g. per-file skips). It carries
#: the file handler but never a console handler, so its records land in the log file without
#: polluting the terminal summary. Silent when file logging is off.
DETAIL_LOGGER = "breezeai_cog.detail"

_LOG_MAX_BYTES = 20 * 1024 * 1024
_LOG_BACKUP_DAYS = 30

_QUOTE_CHARS = set(' "\\\t\n\r=')


def safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"), ensure_ascii=False)


def _format_log_value(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (dict, list)):
        return safe_json_dumps(v)
    if isinstance(v, str):
        if v == "" or any(c in _QUOTE_CHARS for c in v):
            return safe_json_dumps(v)
        return v
    return safe_json_dumps(v)


def _render_plaintext(_, __, event_dict):
    timestamp = event_dict.pop("timestamp", "")
    level = event_dict.pop("level", "info").upper()
    request_id = event_dict.pop("request_id", None)
    message = event_dict.pop("event", "")
    exception = event_dict.pop("exception", None)

    parts = [timestamp, level, f"hostname={HOSTNAME}"]
    if request_id:
        parts.append(f"request_id={request_id}")
    parts.append(f"message={_format_log_value(message)}")
    for k, v in event_dict.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={_format_log_value(v)}")
    line = " ".join(p for p in parts if p != "")

    if exception:
        line += "\n" + "\n".join("\t" + ln for ln in exception.splitlines())
    return line


def _render_json(_, __, event_dict):
    out: dict[str, Any] = {
        "timestamp": event_dict.pop("timestamp", ""),
        "level": str(event_dict.pop("level", "info")).lower(),
        "hostname": HOSTNAME,
    }
    request_id = event_dict.pop("request_id", None)
    if request_id:
        out["request_id"] = request_id
    out["message"] = event_dict.pop("event", "")
    exception = event_dict.pop("exception", None)
    if exception:
        out["stack"] = exception
    for k, v in event_dict.items():
        out[k] = v
    return safe_json_dumps(out)


class _DailyDatedFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Rolls daily and on size cap. Current file is ``<dir>/<prefix>-YYYY-MM-DD.log``;
    when the size cap is hit mid-day the current file is renamed to
    ``<prefix>-YYYY-MM-DD.<n>.log``. Files older than ``backup_count`` days are pruned.
    """

    def __init__(self, dir_path: Path, prefix: str, backup_count: int, max_bytes: int) -> None:
        self._dir = Path(dir_path)
        self._prefix = prefix
        self._dir.mkdir(parents=True, exist_ok=True)
        super().__init__(
            filename=self._build_name(),
            when="midnight",
            backupCount=backup_count,
            encoding="utf-8",
        )
        self.maxBytes = max_bytes

    def _build_name(self) -> str:
        return str(self._dir / f"{self._prefix}-{_date.today().isoformat()}.log")

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if super().shouldRollover(record):
            return 1
        if self.maxBytes > 0 and self.stream is not None:
            self.stream.seek(0, 2)
            if self.stream.tell() + len(self.format(record)) >= self.maxBytes:
                return 1
        return 0

    def doRollover(self) -> None:
        now = int(time.time())
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        if now >= self.rolloverAt:
            self.baseFilename = self._build_name()
            self.rolloverAt = self.computeRollover(now)
        else:
            self._rotate_indexed()
        self._cleanup_old()
        if not self.delay:
            self.stream = self._open()

    def _rotate_indexed(self) -> None:
        base = Path(self.baseFilename)
        stem, ext = base.stem, base.suffix
        i = 1
        while (self._dir / f"{stem}.{i}{ext}").exists():
            i += 1
        try:
            os.rename(self.baseFilename, str(self._dir / f"{stem}.{i}{ext}"))
        except OSError:
            pass

    def _cleanup_old(self) -> None:
        if self.backupCount <= 0:
            return
        cutoff = _date.today() - timedelta(days=self.backupCount)
        pattern = re.compile(
            rf"^{re.escape(self._prefix)}-(\d{{4}}-\d{{2}}-\d{{2}})(?:\.\d+)?\.log$"
        )
        for path in self._dir.iterdir():
            m = pattern.match(path.name)
            if not m:
                continue
            try:
                if _date.fromisoformat(m.group(1)) < cutoff:
                    path.unlink()
            except (ValueError, OSError):
                pass


def setup_logging(settings: "Settings") -> None:
    """Configure stdlib logging + structlog from an injected ``Settings``."""
    formatter = logging.Formatter("%(message)s")
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=settings.log_level, force=True
    )

    logger = logging.getLogger(APP_LOGGER)
    logger.handlers.clear()
    logger.setLevel(settings.log_level)
    logger.propagate = False

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    # File-only detail sink: same file, no console handler, no propagation — so its records
    # are captured on disk but never echoed to the terminal summary. Reset each setup.
    detail_logger = logging.getLogger(DETAIL_LOGGER)
    detail_logger.handlers.clear()
    detail_logger.setLevel(settings.log_level)
    detail_logger.propagate = False

    # httpx/httpcore log every request at INFO ("HTTP Request: POST ... 201 Created"). That
    # noise would shred the upload progress bar and leak backend URLs to the console. Keep it
    # OFF the console (propagate=False → never reaches the root stdout handler) but still route
    # it to the log file below, so the request trail is retained for debugging.
    http_loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    for lg in http_loggers:
        lg.handlers.clear()
        lg.setLevel(settings.log_level)
        lg.propagate = False

    if settings.log_to_file:
        file_handler = _DailyDatedFileHandler(
            dir_path=Path(settings.log_location or "./logs"),
            prefix=APP_NAME,
            backup_count=_LOG_BACKUP_DAYS,
            max_bytes=_LOG_MAX_BYTES,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        detail_logger.addHandler(file_handler)  # detail goes ONLY to the file
        for lg in http_loggers:
            lg.addHandler(file_handler)  # httpx request trail → file only, never the console

    _configure_structlog(settings.log_format)


def _configure_structlog(log_format: str) -> None:
    renderer: Any = _render_json if log_format == "json" else _render_plaintext
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%dT%H:%M:%S", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        context_class=dict,
        logger_factory=LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def setup_worker_logging(queue: object, log_format: str, log_level: str) -> None:
    """Configure a process-pool worker: route the app logger to a ``QueueHandler``
    so records flow to the main process's ``QueueListener``."""
    logger = logging.getLogger(APP_LOGGER)
    logger.handlers.clear()
    logger.setLevel(log_level)
    logger.propagate = False
    logger.addHandler(logging.handlers.QueueHandler(queue))  # type: ignore[arg-type]
    _configure_structlog(log_format)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name or APP_LOGGER)


def quiet_console(*, level: int = logging.WARNING) -> None:
    """Raise the app logger's **console** (stdout) handler threshold to ``level`` without
    touching file handlers. Lets the CLI keep INFO summaries flowing to the log file while
    the terminal shows only the Rich table (plus WARNING/ERROR). Idempotent; a no-op once
    the console has been routed through a live display."""
    logger = logging.getLogger(APP_LOGGER)
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            handler.setLevel(level)


#: Log-level → rich style for console-routed records (INFO stays default/uncolored).
_LEVEL_STYLE = {
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold red",
}


class _ConsoleLogHandler(logging.Handler):
    """Emit each record through a shared rich ``Console``. When that console has an active
    ``Live``/``Progress`` display, rich prints the line **above** the pinned bar (on its own
    line) instead of clobbering it — and colours it by level."""

    def __init__(self, console: Any) -> None:
        super().__init__()
        self._console = console
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._console.print(
                self.format(record),
                style=_LEVEL_STYLE.get(record.levelno),
                markup=False,      # log text is literal — never interpret `[...]` as markup
                highlight=False,   # no auto-highlighting of numbers/paths
                soft_wrap=False,
            )
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


@contextmanager
def route_logs_through_console(console: Any) -> Iterator[None]:
    """Temporarily route the app logger through ``console`` so records render above a live
    ``Progress`` bar — on their own lines, coloured by level — rather than tearing it apart.

    Must wrap the analysis run (before the process-pool ``QueueListener`` snapshots the app
    logger's handlers), so funnelled worker warnings render through the console too. Restores
    the original handlers on exit."""
    logger = logging.getLogger(APP_LOGGER)
    saved = logger.handlers[:]
    handler = _ConsoleLogHandler(console)
    # Inherit the threshold of the stream handler we're replacing so a prior quiet_console()
    # (INFO summary suppressed on-screen when a table is shown) carries over to the bar too.
    stream_levels = [
        h.level for h in saved
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    handler.setLevel(min(stream_levels) if stream_levels else logger.level)
    # Replace only the console/stream output with the rich-routed one; keep file handlers
    # (``--log-to-file``) writing as usual.
    kept = [h for h in saved if isinstance(h, logging.FileHandler)]
    logger.handlers = kept + [handler]
    try:
        yield
    finally:
        logger.handlers = saved


# ── Context helpers (per-run / per-file) — thin wrappers over structlog.contextvars ──

def bind_context(**values: Any) -> None:
    """Bind context fields (e.g. run_id, repo, path, parser, language) to all logs."""
    structlog.contextvars.bind_contextvars(**values)


def unbind_context(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
