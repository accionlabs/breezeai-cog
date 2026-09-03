"""Batch-upload orchestration: resumable state + a bounded thread pool that uploads
several repos concurrently.

This module is deliberately **console-free** — it reports progress through a
:class:`UploadTracker` (a thread-safe snapshot source) and logs raw backend responses to
the file logger, so it can be unit-tested without a TTY. The Rich rendering layer lives in
``cli.py`` and reads ``tracker`` snapshots.

Resume: :class:`UploadState` persists the set of repos that have reached the backend's
terminal ``active`` status to ``<workspace>/.cog/batch-upload-state.json``. An interrupted
run leaves the file behind; a re-run skips the repos already in it. The caller deletes the
file once every task in the run has succeeded.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..errors import UploadError
from ..logging import get_logger
from ..utils.paths import cog_dir
from .upload import extract_ontology_id, poll_ontology_status, upload_ontology

_STATE_FILENAME = "batch-upload-state.json"


@dataclass(frozen=True, slots=True)
class UploadTask:
    """One unit of upload work: a repo name and the artifact to POST."""

    repository_name: str
    out_path: Path


# ── Resume state ──────────────────────────────────────────────────────────────

class UploadState:
    """Tracks which repos have reached the backend's terminal ``active`` status, persisted
    for resume. Instances are safe to share across upload threads."""

    def __init__(self, path: Path, completed: set[str]) -> None:
        self.path = path
        self.completed = completed
        self._lock = threading.Lock()

    @classmethod
    def load(cls, workspace: str | Path) -> "UploadState":
        """Load (or start fresh) the state file for ``workspace``. A missing or corrupt
        file yields an empty completed set rather than an error."""
        path = cog_dir(workspace) / _STATE_FILENAME
        completed: set[str] = set()
        if path.is_file():
            try:
                data = json.loads(path.read_text("utf-8"))
                names = data.get("completed", []) if isinstance(data, dict) else []
                completed = {str(n) for n in names} if isinstance(names, list) else set()
            except (OSError, ValueError):
                completed = set()  # corrupt / unreadable → start over
        return cls(path=path, completed=completed)

    def is_done(self, name: str) -> bool:
        with self._lock:
            return name in self.completed

    def mark_done(self, name: str, *, total: int | None = None) -> None:
        """Record ``name`` as completed and persist immediately (so an interruption keeps
        the progress)."""
        with self._lock:
            self.completed.add(name)
            self._save_locked(total)

    def _save_locked(self, total: int | None) -> None:
        payload: dict[str, object] = {
            "workspace": str(self.path.parent.parent),
            "completed": sorted(self.completed),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        if total is not None:
            payload["total"] = total
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            tmp.replace(self.path)  # atomic on POSIX
        except OSError:
            pass  # a failed checkpoint must never abort an in-flight upload

    def clear(self) -> None:
        """Delete the state file — called once every task in the run has succeeded."""
        with self._lock:
            try:
                self.path.unlink()
            except OSError:
                pass


# ── Progress tracking (thread-safe snapshot source) ────────────────────────────

@dataclass(frozen=True, slots=True)
class ActiveView:
    name: str
    attempt: int
    elapsed: float  # seconds since this upload started


@dataclass(frozen=True, slots=True)
class TrackerSnapshot:
    total: int
    completed: int
    failed: tuple[str, ...]
    active: tuple[ActiveView, ...]
    total_elapsed: float  # seconds since the upload phase began


@dataclass
class _Active:
    start: float
    attempt: int = 1


class UploadTracker:
    """Thread-safe progress state for the upload phase. Worker threads report lifecycle
    (:meth:`start` / :meth:`set_attempt` / :meth:`finish`); the renderer reads
    :meth:`snapshot`."""

    def __init__(self, total: int) -> None:
        self._total = total
        self._completed = 0
        self._failed: list[str] = []
        self._errors: dict[str, str] = {}
        self._active: dict[str, _Active] = {}
        self._lock = threading.Lock()
        self._phase_start = time.monotonic()

    def start(self, name: str) -> None:
        with self._lock:
            self._active[name] = _Active(start=time.monotonic())

    def set_attempt(self, name: str, attempt: int) -> None:
        with self._lock:
            entry = self._active.get(name)
            if entry is not None:
                entry.attempt = attempt

    def finish(self, name: str, *, ok: bool, error: str | None = None) -> None:
        with self._lock:
            self._active.pop(name, None)
            if ok:
                self._completed += 1
            else:
                self._failed.append(name)
                if error:
                    self._errors[name] = error

    @property
    def failed(self) -> list[str]:
        with self._lock:
            return list(self._failed)

    @property
    def errors(self) -> dict[str, str]:
        """Failed repo → reason (for surfacing on the console after the bar tears down)."""
        with self._lock:
            return dict(self._errors)

    def snapshot(self) -> TrackerSnapshot:
        now = time.monotonic()
        with self._lock:
            active = tuple(
                ActiveView(name=n, attempt=a.attempt, elapsed=now - a.start)
                for n, a in self._active.items()
            )
            return TrackerSnapshot(
                total=self._total,
                completed=self._completed,
                failed=tuple(self._failed),
                active=active,
                total_elapsed=now - self._phase_start,
            )


# ── Orchestrator ───────────────────────────────────────────────────────────────

def run_batch_uploads(
    tasks: list[UploadTask],
    settings: Settings,
    tracker: UploadTracker,
    *,
    state: UploadState | None = None,
) -> list[str]:
    """Upload every task (up to ``settings.upload_parallelism`` concurrently), polling each
    for backend processing. A repo is marked done in ``state`` only after its poll returns
    ``active``. Raw upload/poll responses are logged to the file logger, never the console.

    Returns the list of repo names that failed (empty when all succeeded).
    """
    log = get_logger()
    total = len(tasks)

    def _worker(task: UploadTask) -> None:
        name = task.repository_name
        tracker.start(name)
        try:
            resp = upload_ontology(
                settings,
                task.out_path,
                repository_name=name,
                on_attempt=lambda n: tracker.set_attempt(name, n),
            )
            log.info("upload.response", repo=name, response=resp)

            ontology_id = extract_ontology_id(resp)
            if not ontology_id:
                log.warning("upload.no_id", repo=name, response=resp)
                tracker.finish(name, ok=False, error="upload response missing '_id' — cannot confirm backend status")
                return

            status = poll_ontology_status(
                settings,
                ontology_id,
                overall_timeout=settings.upload_timeout,  # bound the processing wait, not just the POST
                on_response=lambda p: log.info("upload.poll", repo=name, response=p),
                on_waiting=lambda s: log.info(
                    "upload.poll.waiting", repo=name, status=s or "pending"
                ),
            )
            if status == "active":
                if state is not None:
                    state.mark_done(name, total=total)
                tracker.finish(name, ok=True)
            else:
                log.warning("upload.not_active", repo=name, status=status)
                tracker.finish(name, ok=False, error=f"backend reported status '{status}' (expected 'active')")
        except UploadError as exc:
            log.error("upload.failed", repo=name, error=str(exc))
            tracker.finish(name, ok=False, error=str(exc))
        except Exception as exc:  # a stray error must not wedge the render loop
            log.error("upload.error", repo=name, error=str(exc))
            tracker.finish(name, ok=False, error=str(exc))

    max_workers = max(1, min(settings.upload_parallelism, total or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_worker, t) for t in tasks]
        for fut in as_completed(futures):
            fut.result()  # workers swallow their own errors; this just surfaces the rare escape

    return tracker.failed
