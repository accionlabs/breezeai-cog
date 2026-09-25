"""Tests for the batch-upload orchestration: UploadState resume tracking,
run_batch_uploads (marks done only on backend 'active', records failures, honors
parallelism) and raw-response log routing. upload_ontology / poll_ontology_status are
faked — no network."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager

import breezeai_cog.services.batch_upload as bu
from breezeai_cog.config import Settings
from breezeai_cog.errors import UploadError
from breezeai_cog.logging import APP_LOGGER, DETAIL_LOGGER, _configure_structlog
from breezeai_cog.services.batch_upload import (
    UploadState,
    UploadTask,
    UploadTracker,
    run_batch_uploads,
)
from breezeai_cog.utils.paths import cog_dir


def _settings(**kwargs) -> Settings:
    base = dict(baseurl="https://api.example.com", uuid="proj-uuid", user_api_key="secret-key")
    base.update(kwargs)
    return Settings(_env_file=None, upload=True, **base)


def _tasks(names, tmp_path):
    tasks = []
    for n in names:
        p = tmp_path / f"{n}.ndjson.gz"
        p.write_bytes(b"x")
        tasks.append(UploadTask(n, p))
    return tasks


# ── UploadState ────────────────────────────────────────────────────────────────

def test_state_load_fresh_when_missing(tmp_path):
    state = UploadState.load(tmp_path)
    assert state.completed == set()
    assert not state.path.exists()


def test_state_mark_done_persists_and_clear_deletes(tmp_path):
    state = UploadState.load(tmp_path)
    state.mark_done("proj-a")
    state.mark_done("proj-b", total=3)

    reloaded = UploadState.load(tmp_path)
    assert reloaded.completed == {"proj-a", "proj-b"}
    assert reloaded.is_done("proj-a")

    on_disk = json.loads((cog_dir(tmp_path) / "batch-upload-state.json").read_text())
    assert sorted(on_disk["completed"]) == ["proj-a", "proj-b"]
    assert on_disk["total"] == 3

    state.clear()
    assert not state.path.exists()
    # clear is idempotent
    state.clear()


def test_state_corrupt_file_starts_fresh(tmp_path):
    path = cog_dir(tmp_path) / "batch-upload-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    state = UploadState.load(tmp_path)
    assert state.completed == set()


# ── run_batch_uploads ────────────────────────────────────────────────────────────

def test_run_marks_done_only_on_active(tmp_path, monkeypatch):
    monkeypatch.setattr(bu, "upload_ontology",
                        lambda s, p, *, repository_name, on_attempt=None: {"_id": f"id-{repository_name}"})

    # proj-a → active (done), proj-b → error (not done)
    def fake_poll(s, oid, *, overall_timeout=None, on_response=None, on_waiting=None):
        return "active" if oid == "id-proj-a" else "error"

    monkeypatch.setattr(bu, "poll_ontology_status", fake_poll)

    tasks = _tasks(["proj-a", "proj-b"], tmp_path)
    state = UploadState.load(tmp_path)
    tracker = UploadTracker(total=len(tasks))

    failed = run_batch_uploads(tasks, _settings(), tracker, state=state)

    assert failed == ["proj-b"]
    assert state.is_done("proj-a")
    assert not state.is_done("proj-b")
    snap = tracker.snapshot()
    assert snap.completed == 1 and snap.failed == ("proj-b",)
    # the reason is captured for console surfacing
    assert "proj-b" in tracker.errors and "active" in tracker.errors["proj-b"]


def test_run_records_upload_error_as_failure(tmp_path, monkeypatch):
    def fake_upload(s, p, *, repository_name, on_attempt=None):
        if repository_name == "bad":
            raise UploadError("boom")
        return {"_id": f"id-{repository_name}"}

    monkeypatch.setattr(bu, "upload_ontology", fake_upload)
    monkeypatch.setattr(bu, "poll_ontology_status", lambda s, oid, **k: "active")

    tasks = _tasks(["good", "bad"], tmp_path)
    state = UploadState.load(tmp_path)
    failed = run_batch_uploads(tasks, _settings(), UploadTracker(len(tasks)), state=state)

    assert failed == ["bad"]
    assert state.is_done("good") and not state.is_done("bad")


def test_run_missing_id_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bu, "upload_ontology", lambda s, p, *, repository_name, on_attempt=None: {"no": "id"})
    monkeypatch.setattr(bu, "poll_ontology_status",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not poll")))

    tasks = _tasks(["x"], tmp_path)
    failed = run_batch_uploads(tasks, _settings(), UploadTracker(1), state=None)
    assert failed == ["x"]


def test_raw_responses_logged_on_file_only_logger(tmp_path, monkeypatch):
    # DEF-998-01: the raw backend JSON (`upload.response` / `upload.poll`) must be logged on the
    # file-only detail logger, never the app logger (whose console handler would leak it to the
    # terminal in piped/CI mode). Warnings still go to the app logger so they surface.
    from breezeai_cog.logging import APP_LOGGER, DETAIL_LOGGER

    events: list[tuple[str, str]] = []  # (logger_name, event)

    class _Rec:
        def __init__(self, name: str) -> None:
            self.name = name

        def _record(self, event: str, **_: object) -> None:
            events.append((self.name, event))

        info = warning = error = _record

    monkeypatch.setattr(bu, "get_logger", lambda name=None: _Rec(name or APP_LOGGER))
    monkeypatch.setattr(bu, "upload_ontology",
                        lambda s, p, *, repository_name, on_attempt=None: {"_id": f"id-{repository_name}"})

    def fake_poll(s, oid, *, overall_timeout=None, on_response=None, on_waiting=None):
        if on_response:
            on_response({"status": "active", "raw": "..."})
        return "active"

    monkeypatch.setattr(bu, "poll_ontology_status", fake_poll)

    run_batch_uploads(_tasks(["proj-a"], tmp_path), _settings(), UploadTracker(1), state=None)

    raw = {"upload.response", "upload.poll"}
    detail_events = {e for (n, e) in events if n == DETAIL_LOGGER}
    app_events = {e for (n, e) in events if n == APP_LOGGER}
    assert raw <= detail_events, f"raw responses must use the detail logger; saw {events}"
    assert not (raw & app_events), f"raw responses leaked to the app/console logger: {events}"


def test_run_respects_parallelism(tmp_path, monkeypatch):
    import threading
    import time

    lock = threading.Lock()
    concurrent = {"cur": 0, "max": 0}

    def fake_upload(s, p, *, repository_name, on_attempt=None):
        with lock:
            concurrent["cur"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["cur"])
        time.sleep(0.02)
        with lock:
            concurrent["cur"] -= 1
        return {"_id": f"id-{repository_name}"}

    monkeypatch.setattr(bu, "upload_ontology", fake_upload)
    monkeypatch.setattr(bu, "poll_ontology_status", lambda s, oid, **k: "active")

    tasks = _tasks([f"p{i}" for i in range(6)], tmp_path)
    failed = run_batch_uploads(tasks, _settings(upload_parallelism=3), UploadTracker(len(tasks)), state=None)
    assert failed == []
    assert concurrent["max"] <= 3
    assert concurrent["max"] >= 2  # genuinely ran in parallel


# ── Raw-response log routing (AC2 / DEF-998-01) ─────────────────────────────────

@contextmanager
def _captured_logs(settings: Settings):
    """Capture the records each of the two app loggers actually emits.

    Deliberately *not* ``setup_logging(settings)``: that calls ``logging.basicConfig(
    force=True)``, which closes pytest's own root handlers and breaks unrelated tests later
    in the session. This mirrors only the parts under test — the plaintext renderer plus the
    two loggers, both ``propagate=False`` (which is also why ``caplog`` cannot see them).

    Which logger a record lands on is the whole point: ``setup_logging`` gives APP_LOGGER a
    stdout handler and gives DETAIL_LOGGER the file handler *only* (see ``logging.py``), so
    "payload on DETAIL_LOGGER" == "payload never printed".
    """
    import structlog

    names = (APP_LOGGER, DETAIL_LOGGER)
    loggers = [logging.getLogger(n) for n in names]
    saved = [(lg.handlers[:], lg.level, lg.propagate) for lg in loggers]
    try:
        _configure_structlog(settings.log_format)
        sinks: dict[str, list[logging.LogRecord]] = {}
        for name, lg in zip(names, loggers):
            records: list[logging.LogRecord] = []
            handler = logging.Handler()
            handler.emit = records.append  # type: ignore[method-assign]
            lg.handlers = [handler]
            lg.setLevel(settings.log_level)
            lg.propagate = False
            sinks[name] = records
        yield sinks
    finally:
        for lg, (handlers, level, propagate) in zip(loggers, saved):
            lg.handlers = handlers
            lg.setLevel(level)
            lg.propagate = propagate
        structlog.reset_defaults()


def _messages(sinks: dict[str, list[logging.LogRecord]], name: str) -> list[str]:
    return [r.getMessage() for r in sinks[name]]


def _fake_backend(monkeypatch, *, response: dict, status: str = "active") -> None:
    """Fake the upload + poll pair, handing ``on_response`` a raw poll payload the way the
    real poller does."""
    monkeypatch.setattr(bu, "upload_ontology",
                        lambda s, p, *, repository_name, on_attempt=None: response)

    def fake_poll(s, oid, *, overall_timeout=None, on_response=None, on_waiting=None):
        if on_response is not None:
            on_response({"success": True, "data": {"fileGraphStatus": status, "raw": "payload"}})
        return status

    monkeypatch.setattr(bu, "poll_ontology_status", fake_poll)


def test_raw_responses_stay_off_the_app_logger(tmp_path, monkeypatch):
    """DEF-998-01: a piped / CI run has no Rich display, so nothing quiets the app logger's
    console handler — a payload logged there would print to stdout. Payloads must go to the
    file-only detail logger instead."""
    _fake_backend(monkeypatch, response={"success": True, "data": {"_id": "id-1", "raw": "payload"}})
    settings = _settings(log_to_file=False)

    with _captured_logs(settings) as sinks:
        failed = run_batch_uploads(_tasks(["repo-alpha"], tmp_path), settings, UploadTracker(1), state=None)

    assert failed == []
    app = _messages(sinks, APP_LOGGER)
    detail = _messages(sinks, DETAIL_LOGGER)
    assert not [m for m in app if "response=" in m], app
    assert any("message=upload.response" in m and "response=" in m for m in detail), detail
    assert any("message=upload.poll " in m and "response=" in m for m in detail), detail


def test_verbose_puts_raw_responses_back_on_the_app_logger(tmp_path, monkeypatch):
    """``--verbose`` (log_level=DEBUG) is the escape hatch: payloads go to the app logger so
    they show on the terminal, rather than only in .cog/logs."""
    _fake_backend(monkeypatch, response={"success": True, "data": {"_id": "id-1", "raw": "payload"}})
    settings = _settings(log_to_file=False, log_level="DEBUG")

    with _captured_logs(settings) as sinks:
        failed = run_batch_uploads(_tasks(["repo-alpha"], tmp_path), settings, UploadTracker(1), state=None)

    assert failed == []
    app = _messages(sinks, APP_LOGGER)
    assert any("message=upload.response" in m and "response=" in m for m in app), app
    assert _messages(sinks, DETAIL_LOGGER) == []


def test_no_id_warning_carries_no_payload(tmp_path, monkeypatch):
    """The missing-``_id`` record is a WARNING, so it survives every console threshold
    (``quiet_console`` only raises it *to* WARNING) — it must not embed the raw response."""
    _fake_backend(monkeypatch, response={"success": True, "data": {"no_id_here": True, "raw": "payload"}})
    settings = _settings(log_to_file=False)

    with _captured_logs(settings) as sinks:
        failed = run_batch_uploads(_tasks(["repo-alpha"], tmp_path), settings, UploadTracker(1), state=None)

    assert failed == ["repo-alpha"]
    warnings = [r.getMessage() for r in sinks[APP_LOGGER] if r.levelno >= logging.WARNING]
    assert any("message=upload.no_id" in m for m in warnings), warnings
    assert not [m for m in warnings if "response=" in m], warnings
