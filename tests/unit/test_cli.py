"""CLI smoke tests via Typer's CliRunner."""

from __future__ import annotations

import gzip
import json

import pytest
from typer.testing import CliRunner

from breezeai_cog import __version__
from breezeai_cog.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_log_files(monkeypatch):
    monkeypatch.setenv("BREEZEAI_COG_LOG_TO_FILE", "false")  # don't create ./logs in tests


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_capabilities() -> None:
    result = runner.invoke(app, ["capabilities"])
    assert result.exit_code == 0
    caps = json.loads(result.stdout)
    assert "python" in caps["languages"] and caps["schemaVersion"] == "2.1"


def test_repo_to_json_tree(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    (repo / "b.py").write_text("class B:\n    pass\n")
    out_dir = tmp_path / "results"  # --out is a directory; filename is derived

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    out = out_dir / "repo-project-analysis.ndjson.gz"
    assert out.exists()
    records = [json.loads(line) for line in gzip.open(out, "rt", encoding="utf-8").read().splitlines()]
    assert records[0]["__type"] == "projectMetaData"
    assert records[0]["totalFiles"] == 2


def test_repo_to_json_tree_defaults_to_cog_dir(tmp_path) -> None:
    """With no --out, the export lands in <repo>/.cog and the repo's parent stays clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")

    result = runner.invoke(app, ["repo-to-json-tree", "--repo", str(repo), "--jobs", "1"])
    assert result.exit_code == 0, result.output

    assert (repo / ".cog" / "repo-project-analysis.ndjson.gz").exists()
    # nothing leaked into the repo's parent (the old default location)
    assert list(tmp_path.glob("*.ndjson.gz")) == []


def test_out_redirects_only_export_skip_report_stays_in_cog(tmp_path) -> None:
    """--out redirects the ndjson.gz but the skip report still goes to <repo>/.cog."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    (repo / "notes.txt").write_text("hello\n")  # unsupported → produces a skip report
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "repo-project-analysis.ndjson.gz").exists()
    assert (repo / ".cog" / "repo-skipped-report.json").exists()
    assert not (repo / ".cog" / "repo-project-analysis.ndjson.gz").exists()


def test_warns_when_cog_not_gitignored(tmp_path) -> None:
    """A .gitignore that doesn't ignore .cog triggers a readable nudge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    (repo / ".gitignore").write_text("*.pyc\n")

    result = runner.invoke(app, ["repo-to-json-tree", "--repo", str(repo), "--jobs", "1"])
    assert result.exit_code == 0, result.output
    assert ".cog/" in result.output and ".gitignore" in result.output


def test_no_warning_when_cog_gitignored(tmp_path) -> None:
    """No nudge when .cog is already ignored, and none when there's no .gitignore at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    (repo / ".gitignore").write_text(".cog/\n")

    result = runner.invoke(app, ["repo-to-json-tree", "--repo", str(repo), "--jobs", "1"])
    assert result.exit_code == 0, result.output
    assert "not ignored by" not in result.output

    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    (repo2 / "a.py").write_text("def f():\n    return 1\n")
    result2 = runner.invoke(app, ["repo-to-json-tree", "--repo", str(repo2), "--jobs", "1"])
    assert result2.exit_code == 0, result2.output
    assert "not ignored by" not in result2.output


def test_skip_rows_go_to_log_file_not_console(tmp_path, monkeypatch) -> None:
    """Each skipped file gets its own `file.skipped` row in the LOG FILE (path + reason),
    but those rows never appear on the console — they must not pollute the summary output."""
    monkeypatch.setenv("BREEZEAI_COG_LOG_TO_FILE", "true")  # override the autouse off-switch
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    (repo / "notes.txt").write_text("x\n")  # unsupported
    (repo / "data.bin").write_text("y\n")  # unsupported

    result = runner.invoke(app, ["repo-to-json-tree", "--repo", str(repo), "--jobs", "1"])
    assert result.exit_code == 0, result.output
    # never on the console (would pollute the summary)
    assert "file.skipped" not in result.output

    logs = list((repo / ".cog" / "logs").glob("breezeai-cog-*.log"))
    assert logs, "no log file written"
    content = logs[0].read_text("utf-8")
    assert content.count("file.skipped") == 2  # one row per skipped file
    assert "path=notes.txt" in content and "path=data.bin" in content
    assert "reason=unsupported" in content
    assert "analysis.complete" in content  # summary is captured too


def test_repo_to_json_tree_writes_skip_report(tmp_path) -> None:
    """After analysis, a <repo>-skipped-report.json sidecar lists ignored/unsupported files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    (repo / "notes.txt").write_text("hello\n")  # unsupported extension
    (repo / "image.bin").write_text("data\n")  # unsupported extension
    (repo / "node_modules").mkdir()  # ignored directory (built-in default_ignores)
    (repo / "node_modules" / "dep.js").write_text("module.exports = {}\n")
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output

    # The skip report always lands in <repo>/.cog — never in --out (which only redirects the export).
    sidecar = repo / ".cog" / "repo-skipped-report.json"
    assert sidecar.exists()
    assert not (out_dir / "repo-skipped-report.json").exists()
    report = json.loads(sidecar.read_text())
    assert report["summary"].get("unsupported", 0) >= 2
    assert report["unsupportedExtensions"].get(".txt") == 1
    assert report["unsupportedExtensions"].get(".bin") == 1
    assert "node_modules" in report["ignoredDirectories"]
    # the pruned dir's contents are not enumerated as individual files
    assert not any(f["path"].startswith("node_modules/") for f in report["files"])


def test_repo_to_json_tree_batch(tmp_path) -> None:
    """--batch analyzes each immediate subdirectory as its own project."""
    workspace = tmp_path / "workspace"
    (workspace / "proj-a").mkdir(parents=True)
    (workspace / "proj-b").mkdir()
    (workspace / ".hidden").mkdir()  # dot dirs are skipped
    (workspace / "proj-a" / "a.py").write_text("def f():\n    return 1\n")
    (workspace / "proj-b" / "b.py").write_text("class B:\n    pass\n")
    (workspace / ".hidden" / "h.py").write_text("def h():\n    return 0\n")
    (workspace / "loose.py").write_text("x = 1\n")  # loose files are ignored
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app,
        ["repo-to-json-tree", "--repo", str(workspace), "--batch", "--out", str(out_dir), "--jobs", "1"],
    )
    assert result.exit_code == 0, result.output

    a = out_dir / "proj-a-project-analysis.ndjson.gz"
    b = out_dir / "proj-b-project-analysis.ndjson.gz"
    assert a.exists() and b.exists()
    # exactly two projects — .hidden and loose.py produce no output
    assert sorted(p.name for p in out_dir.glob("*.ndjson.gz")) == [
        "proj-a-project-analysis.ndjson.gz",
        "proj-b-project-analysis.ndjson.gz",
    ]
    meta_a = json.loads(gzip.open(a, "rt", encoding="utf-8").readline())
    assert meta_a["__type"] == "projectMetaData"
    assert meta_a["totalFiles"] == 1


def test_repo_to_json_tree_batch_empty(tmp_path) -> None:
    """--batch on a folder with no subdirectories exits non-zero."""
    workspace = tmp_path / "empty"
    workspace.mkdir()
    result = runner.invoke(app, ["repo-to-json-tree", "--repo", str(workspace), "--batch"])
    assert result.exit_code == 1


def test_repo_to_json_tree_no_output_when_nothing_parsed(tmp_path) -> None:
    """A repo with no parseable source files writes no .ndjson.gz."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.txt").write_text("just prose, no code\n")  # not a supported source file
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    assert not (out_dir / "repo-project-analysis.ndjson.gz").exists()
    assert list(out_dir.glob("*.ndjson.gz")) == []
    assert "no ndjson written" in result.output


def test_repo_to_json_tree_no_output_for_trivial_config_only(tmp_path) -> None:
    """A folder whose only file is a trivial config (no code, no real signal) writes nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Title\n\njust docs\n")  # classified config, no content
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    assert list(out_dir.glob("*.ndjson.gz")) == []
    assert "no ndjson written" in result.output


def test_repo_to_json_tree_writes_config_repo_with_dependencies(tmp_path) -> None:
    """A config-only repo that carries real signal (dependencies) still produces output."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"name": "x", "dependencies": {"left-pad": "^1.0.0"}}\n'
    )
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "repo-project-analysis.ndjson.gz").exists()


def test_repo_to_json_tree_batch_skips_empty_projects(tmp_path) -> None:
    """--batch emits files only for subdirs that actually parse to records."""
    workspace = tmp_path / "workspace"
    (workspace / "code").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "code" / "a.py").write_text("def f():\n    return 1\n")
    (workspace / "docs" / "notes.txt").write_text("no code here\n")
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app,
        ["repo-to-json-tree", "--repo", str(workspace), "--batch", "--out", str(out_dir), "--jobs", "1"],
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out_dir.glob("*.ndjson.gz")) == [
        "code-project-analysis.ndjson.gz",
    ]


def test_repo_to_json_tree_batch_upload(tmp_path, monkeypatch) -> None:
    """--batch --upload uploads each sub-project and polls for status before the next."""
    workspace = tmp_path / "workspace"
    (workspace / "proj-a").mkdir(parents=True)
    (workspace / "proj-b").mkdir()
    (workspace / "empty").mkdir()  # no code files → no upload, no poll
    (workspace / "proj-a" / "a.py").write_text("def f():\n    return 1\n")
    (workspace / "proj-b" / "b.py").write_text("class B:\n    pass\n")
    (workspace / "empty" / "notes.txt").write_text("no code\n")
    out_dir = tmp_path / "results"

    uploaded: list[str] = []
    polled: list[str] = []

    def fake_upload(settings, file_path, *, repository_name: str, on_attempt=None):  # type: ignore[misc]
        uploaded.append(repository_name)
        return {"_id": f"id-{repository_name}"}

    def fake_poll(settings, ontology_id, *, poll_interval=60, overall_timeout=None, on_waiting=None, on_response=None):  # type: ignore[misc]
        polled.append(ontology_id)
        return "active"

    import breezeai_cog.services.batch_upload as _bu

    monkeypatch.setattr(_bu, "upload_ontology", fake_upload)
    monkeypatch.setattr(_bu, "poll_ontology_status", fake_poll)

    result = runner.invoke(
        app,
        [
            "repo-to-json-tree",
            "--repo", str(workspace),
            "--batch",
            "--out", str(out_dir),
            "--jobs", "1",
            "--upload",
            "--baseurl", "http://fake.test",
            "--uuid", "test-uuid-123",
            "--user-api-key", "test-key",
        ],
    )
    assert result.exit_code == 0, result.output
    # one upload + one poll per project with parseable code; empty sub-dir skipped
    assert sorted(uploaded) == ["proj-a", "proj-b"]
    assert sorted(polled) == ["id-proj-a", "id-proj-b"]


def test_repo_to_json_tree_batch_upload_partial_failure(tmp_path, monkeypatch) -> None:
    """--batch --upload continues after one upload failure; reports non-zero at the end."""
    workspace = tmp_path / "workspace"
    (workspace / "proj-a").mkdir(parents=True)
    (workspace / "proj-b").mkdir()
    (workspace / "proj-a" / "a.py").write_text("def f():\n    return 1\n")
    (workspace / "proj-b" / "b.py").write_text("class B:\n    pass\n")
    out_dir = tmp_path / "results"

    from breezeai_cog.errors import UploadError

    upload_calls: list[str] = []
    polled: list[str] = []

    def fake_upload(settings, file_path, *, repository_name: str, on_attempt=None):  # type: ignore[misc]
        upload_calls.append(repository_name)
        if repository_name == "proj-a":  # first project (sorted) fails
            raise UploadError("network error")
        return {"_id": f"id-{repository_name}"}

    def fake_poll(settings, ontology_id, *, poll_interval=60, overall_timeout=None, on_waiting=None, on_response=None):  # type: ignore[misc]
        polled.append(ontology_id)
        return "active"

    import breezeai_cog.services.batch_upload as _bu

    monkeypatch.setattr(_bu, "upload_ontology", fake_upload)
    monkeypatch.setattr(_bu, "poll_ontology_status", fake_poll)

    result = runner.invoke(
        app,
        [
            "repo-to-json-tree",
            "--repo", str(workspace),
            "--batch",
            "--out", str(out_dir),
            "--jobs", "1",
            "--upload",
            "--baseurl", "http://fake.test",
            "--uuid", "test-uuid",
            "--user-api-key", "test-key",
        ],
    )
    # overall exit is non-zero because at least one upload failed
    assert result.exit_code == 1
    # both sub-projects were analyzed (ndjson files written)
    assert (out_dir / "proj-a-project-analysis.ndjson.gz").exists()
    assert (out_dir / "proj-b-project-analysis.ndjson.gz").exists()
    # both uploads attempted — batch did not stop at the first failure
    assert sorted(upload_calls) == ["proj-a", "proj-b"]
    # poll only runs for the project whose upload succeeded
    assert polled == ["id-proj-b"]
    # the failure reason is surfaced on the console, not just the log file
    assert "Upload errors:" in result.output
    assert "proj-a" in result.output and "network error" in result.output


# ---------------------------------------------------------------------------
# --repo-list selection
# ---------------------------------------------------------------------------

def _workspace_with(tmp_path, names):
    ws = tmp_path / "workspace"
    for n in names:
        (ws / n).mkdir(parents=True)
        (ws / n / "m.py").write_text("def f():\n    return 1\n")
    return ws


def test_repo_list_requires_batch(tmp_path) -> None:
    ws = _workspace_with(tmp_path, ["a"])
    listing = tmp_path / "list.txt"
    listing.write_text("a\n")
    result = runner.invoke(app, ["repo-to-json-tree", "--repo", str(ws), "--repo-list", str(listing)])
    assert result.exit_code == 1
    assert "--repo-list requires --batch" in result.output


def test_repo_list_filters_to_named_subdirs(tmp_path) -> None:
    ws = _workspace_with(tmp_path, ["a", "b", "c"])
    out_dir = tmp_path / "out"
    listing = tmp_path / "list.txt"
    listing.write_text("# only these\na\n\nc\n")

    result = runner.invoke(
        app,
        ["repo-to-json-tree", "--repo", str(ws), "--batch", "--repo-list", str(listing),
         "--out", str(out_dir), "--jobs", "1"],
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out_dir.glob("*.ndjson.gz")) == [
        "a-project-analysis.ndjson.gz",
        "c-project-analysis.ndjson.gz",
    ]


def test_repo_list_unknown_name_errors(tmp_path) -> None:
    ws = _workspace_with(tmp_path, ["a"])
    listing = tmp_path / "list.txt"
    listing.write_text("a\nnope\n")
    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(ws), "--batch", "--repo-list", str(listing), "--jobs", "1"]
    )
    assert result.exit_code == 1
    assert "nope" in result.output


# ---------------------------------------------------------------------------
# resume + upload observability
# ---------------------------------------------------------------------------

def _fake_upload_poll(monkeypatch, uploaded, polled):
    import breezeai_cog.services.batch_upload as _bu

    def fake_upload(settings, file_path, *, repository_name, on_attempt=None):
        uploaded.append(repository_name)
        return {"_id": f"id-{repository_name}"}

    def fake_poll(settings, ontology_id, *, poll_interval=60, overall_timeout=None, on_waiting=None, on_response=None):
        polled.append(ontology_id)
        return "active"

    monkeypatch.setattr(_bu, "upload_ontology", fake_upload)
    monkeypatch.setattr(_bu, "poll_ontology_status", fake_poll)


def test_batch_upload_resume_skips_completed_and_clears_state(tmp_path, monkeypatch) -> None:
    import json as _json

    from breezeai_cog.utils.paths import cog_dir

    ws = _workspace_with(tmp_path, ["proj-a", "proj-b"])
    # Pre-seed a resume state marking proj-a already uploaded.
    state_file = cog_dir(ws) / "batch-upload-state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(_json.dumps({"completed": ["proj-a"]}))

    uploaded: list[str] = []
    polled: list[str] = []
    _fake_upload_poll(monkeypatch, uploaded, polled)

    result = runner.invoke(
        app,
        ["repo-to-json-tree", "--repo", str(ws), "--batch", "--out", str(tmp_path / "out"),
         "--jobs", "1", "--upload", "--baseurl", "http://x", "--uuid", "u", "--user-api-key", "k"],
    )
    assert result.exit_code == 0, result.output
    assert uploaded == ["proj-b"]  # proj-a skipped
    assert "already uploaded" in result.output
    # state file deleted after the run completes fully
    assert not state_file.exists()


def test_batch_upload_raw_response_goes_to_log_not_console(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BREEZEAI_COG_LOG_TO_FILE", "true")  # override autouse off-switch
    from breezeai_cog.utils.paths import cog_dir

    ws = _workspace_with(tmp_path, ["proj-a"])
    uploaded: list[str] = []
    polled: list[str] = []
    _fake_upload_poll(monkeypatch, uploaded, polled)

    result = runner.invoke(
        app,
        ["repo-to-json-tree", "--repo", str(ws), "--batch", "--out", str(tmp_path / "out"),
         "--jobs", "1", "--upload", "--baseurl", "http://x", "--uuid", "u", "--user-api-key", "k"],
    )
    assert result.exit_code == 0, result.output
    # the old raw-response console prefixes are gone
    assert "Upload response:" not in result.output
    assert "Poll response:" not in result.output
    # the raw response is captured in the workspace .cog log file
    logs = list((cog_dir(ws) / "logs").glob("breezeai-cog-*.log"))
    assert logs, "no log file written"
    content = logs[0].read_text("utf-8")
    assert "upload.response" in content and "id-proj-a" in content


def test_batch_upload_force_reuploads_everything(tmp_path, monkeypatch) -> None:
    import json as _json

    from breezeai_cog.utils.paths import cog_dir

    ws = _workspace_with(tmp_path, ["proj-a", "proj-b"])
    state_file = cog_dir(ws) / "batch-upload-state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(_json.dumps({"completed": ["proj-a", "proj-b"]}))  # both "done"

    uploaded: list[str] = []
    polled: list[str] = []
    _fake_upload_poll(monkeypatch, uploaded, polled)

    result = runner.invoke(
        app,
        ["repo-to-json-tree", "--repo", str(ws), "--batch", "--force", "--out", str(tmp_path / "out"),
         "--jobs", "1", "--upload", "--baseurl", "http://x", "--uuid", "u", "--user-api-key", "k"],
    )
    assert result.exit_code == 0, result.output
    assert "--force" in result.output
    # both re-uploaded despite the pre-seeded state
    assert sorted(uploaded) == ["proj-a", "proj-b"]
    assert not state_file.exists()  # cleared after a fully-successful run


def test_upload_tuning_flags_plumb_into_settings(tmp_path, monkeypatch) -> None:
    import breezeai_cog.services.batch_upload as _bu

    captured: dict[str, object] = {}

    def fake_upload(settings, file_path, *, repository_name, on_attempt=None):
        captured["timeout"] = settings.upload_timeout
        captured["parallelism"] = settings.upload_parallelism
        captured["retries"] = settings.upload_max_retries
        return {"_id": f"id-{repository_name}"}

    monkeypatch.setattr(_bu, "upload_ontology", fake_upload)
    monkeypatch.setattr(_bu, "poll_ontology_status", lambda s, oid, **k: "active")

    ws = _workspace_with(tmp_path, ["proj-a"])
    result = runner.invoke(
        app,
        ["repo-to-json-tree", "--repo", str(ws), "--batch", "--out", str(tmp_path / "out"),
         "--jobs", "1", "--upload", "--baseurl", "http://x", "--uuid", "u", "--user-api-key", "k",
         "--upload-timeout", "30", "--parallel-uploads", "2", "--upload-max-retries", "0"],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"timeout": 30.0, "parallelism": 2, "retries": 0}
