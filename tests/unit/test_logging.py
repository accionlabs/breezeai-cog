"""Tests for logging: renderers, file handler, and context binding."""

from __future__ import annotations

import json

from breezeai_cog.config import Settings
from breezeai_cog.logging import (
    HOSTNAME,
    _render_json,
    _render_plaintext,
    bind_context,
    clear_context,
    get_logger,
    setup_logging,
)


def test_render_plaintext_basic() -> None:
    line = _render_plaintext(None, None, {"timestamp": "2026-06-29T00:00:00",
                                          "level": "info", "event": "scan.done", "files": 3})
    assert "2026-06-29T00:00:00 INFO" in line
    assert f"hostname={HOSTNAME}" in line
    assert "message=scan.done" in line
    assert "files=3" in line


def test_render_plaintext_quotes_values_with_spaces() -> None:
    line = _render_plaintext(None, None, {"level": "warning", "event": "x", "note": "has space"})
    assert 'note="has space"' in line  # quoted via json when it contains a space


def test_render_json_flat() -> None:
    out = json.loads(_render_json(None, None, {"timestamp": "T", "level": "INFO",
                                               "event": "db.query", "rows": 10}))
    assert out["message"] == "db.query"
    assert out["level"] == "info"  # lowercased
    assert out["hostname"] == HOSTNAME
    assert out["rows"] == 10


def test_setup_logging_writes_file_with_context(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        log_to_file=True,
        log_format="json",
        log_location=tmp_path,
        log_level="INFO",
    )
    setup_logging(settings)
    try:
        bind_context(run_id="r1", repo="my-app")
        get_logger("breezeai_cog.test").info("file.event", k="v")
    finally:
        clear_context()

    logs = list(tmp_path.glob("breezeai-cog-*.log"))
    assert logs, "no dated log file created"
    content = logs[0].read_text("utf-8")
    record = json.loads(content.strip().splitlines()[-1])
    assert record["message"] == "file.event"
    assert record["k"] == "v"
    assert record["run_id"] == "r1" and record["repo"] == "my-app"
