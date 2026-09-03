"""Tests for the batch-upload orchestration: UploadState resume tracking and
run_batch_uploads (marks done only on backend 'active', records failures, honors
parallelism). upload_ontology / poll_ontology_status are faked — no network."""

from __future__ import annotations

import json

import breezeai_cog.services.batch_upload as bu
from breezeai_cog.config import Settings
from breezeai_cog.errors import UploadError
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
