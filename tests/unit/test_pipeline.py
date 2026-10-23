"""End-to-end pipeline / public-API tests on a tiny temp repo."""

from __future__ import annotations

import gzip
import json

import breezeai_cog
from breezeai_cog import analyze_repo, capabilities, iter_file_records


def _make_repo(root) -> None:
    (root / "app.py").write_text("import os\n\nclass A:\n    def m(self):\n        return 1\n\ndef top():\n    return 2\n")
    (root / "util.py").write_text("def helper():\n    return 3\n")
    (root / ".venv").mkdir()
    (root / ".venv" / "junk.py").write_text("should = 'be ignored'\n")  # pruned by builtin
    (root / "test_app.py").write_text("def test_x():\n    assert True\n")  # excluded (test_*.py)


def test_analyze_repo_writes_metadata_first(tmp_path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _make_repo(repo)

    result = analyze_repo(repo)
    assert result.out_path is not None and result.out_path.exists()

    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    records = [json.loads(line) for line in lines]

    assert records[0]["__type"] == "projectMetaData"
    meta = records[0]
    assert meta["totalFiles"] == 2  # app.py + util.py (.venv pruned, test_ excluded)
    assert meta["totalClasses"] == 1
    assert meta["totalFunctions"] == 3  # A.m, top, helper
    assert meta["analyzedLanguages"] == ["python"]
    assert meta["toolVersion"] == breezeai_cog.__version__

    paths = sorted(r["path"] for r in records[1:])
    assert paths == ["app.py", "util.py"]


def test_data_only_repo_is_saved(tmp_path) -> None:
    # a code-less repo whose only file is a captured JSON data document still writes its export
    repo = tmp_path / "data"
    repo.mkdir()
    (repo / "workflow.json").write_text('{"nodes":[{"id":"1","name":"a"}],"active":true}')
    result = analyze_repo(repo)
    assert result.written is True
    assert result.out_path is not None and result.out_path.exists()
    meta = json.loads(gzip.open(result.out_path, "rt", encoding="utf-8").readline())
    assert meta["configs"]["capturedDataDocuments"] >= 1


def test_empty_json_only_repo_is_not_saved(tmp_path) -> None:
    # empty / scalar JSON captures nothing → no export file is written (capturedDataDocuments 0)
    repo = tmp_path / "empty"
    repo.mkdir()
    (repo / "config.json").write_text("{}")
    (repo / "other.json").write_text("null")
    result = analyze_repo(repo)
    assert result.written is False
    assert not (result.out_path and result.out_path.exists())


def test_iter_file_records(tmp_path) -> None:
    repo = tmp_path / "r2"
    repo.mkdir()
    _make_repo(repo)
    records = list(iter_file_records(repo))
    assert sorted(r.path for r in records) == ["app.py", "util.py"]
    assert all(isinstance(r.loc, int) for r in records)


def test_capabilities() -> None:
    caps = capabilities()
    assert "python" in caps["languages"]
    assert ".py" in caps["extensions"]
    assert caps["schemaVersion"] == "2.2"


def test_run_reports_progress(tmp_path) -> None:
    """pipeline.run forwards live (done, total) progress as files complete."""
    from breezeai_cog.config import Settings
    from breezeai_cog.core import pipeline
    from breezeai_cog.emit.sinks import MemorySink

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(5):
        (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    seen: list[tuple[int, int]] = []
    pipeline.run(repo, Settings(jobs=1), MemorySink(), progress=lambda d, t: seen.append((d, t)))

    assert seen[0] == (0, 5)                       # total set up front
    assert seen[-1] == (5, 5)                       # ends at total
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)  # monotonic
    assert all(t == 5 for _, t in seen)             # total stable


def test_extensionless_ruby_executable_reaches_extraction(tmp_path) -> None:
    repo = tmp_path / "repo"
    executable = repo / "exe" / "rubocop"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(
        b"#!/usr/bin/env ruby\n"
        b"class Rubocop\n"
        b"  def run\n"
        b"    puts :ok\n"
        b"  end\n"
        b"end\n"
    )

    records = {record.path: record for record in iter_file_records(repo)}

    assert "exe/rubocop" in records
    assert records["exe/rubocop"].language == "ruby"
    assert any(cls.name == "Rubocop" for cls in records["exe/rubocop"].classes)
    assert any(fn.name == "run" for fn in records["exe/rubocop"].functions)


def test_existing_ruby_project_filenames_still_reach_extraction(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("task.rake", "rubocop.gemspec", "Gemfile", "Rakefile"):
        (repo / name).write_text("class ProjectFile\n  def run; end\nend\n", encoding="utf-8")

    records = {record.path: record for record in iter_file_records(repo)}

    assert set(records) == {"task.rake", "rubocop.gemspec", "Gemfile", "Rakefile"}
    assert all(record.language == "ruby" for record in records.values())
    assert all(any(function.name == "run" for function in record.functions)
               for record in records.values())


def test_ruby_class_metric_excludes_modules_and_counts_nested_classes(tmp_path) -> None:
    from breezeai_cog.config import Settings
    from breezeai_cog.core import pipeline
    from breezeai_cog.emit.sinks import MemorySink

    cases = [
        (
            "classes_only",
            "class Outer\n  class Inner\n  end\nend\n",
            2,
            [("Outer", "class"), ("Inner", "class")],
        ),
        (
            "modules_only",
            "module Outer\n  module Inner\n  end\nend\n",
            0,
            [("Outer", "module"), ("Inner", "module")],
        ),
        (
            "classes_and_modules",
            "class Widget\nend\nmodule Helpers\nend\n",
            1,
            [("Widget", "class"), ("Helpers", "module")],
        ),
        (
            "nested_classes_and_modules",
            "class Outer\n  module Namespace\n    class Inner\n    end\n    module Nested\n    end\n  end\nend\n",
            2,
            [
                ("Outer", "class"),
                ("Namespace", "module"),
                ("Inner", "class"),
                ("Nested", "module"),
            ],
        ),
    ]

    for name, source, expected_count, expected_nodes in cases:
        repo = tmp_path / name
        repo.mkdir()
        (repo / "sample.rb").write_text(source, encoding="utf-8")
        sink = MemorySink()

        metadata = pipeline.run(repo, Settings(repo=repo, jobs=1), sink)

        assert metadata.totalClasses == expected_count
        record = next(record for record in sink.records if record.path == "sample.rb")
        assert [(node.name, node.type) for node in record.classes] == expected_nodes


def test_project_loc_excludes_config_records_and_keeps_ruby_source(tmp_path) -> None:
    from breezeai_cog.config import Settings
    from breezeai_cog.core import pipeline
    from breezeai_cog.emit.sinks import MemorySink

    cases = [
        ("ruby_source", {"app.rb": "class App\n  def run; end\nend\n"}, 3),
        ("config_only", {"settings.json": '{\n  "enabled": true\n}\n'}, 0),
        (
            "mixed",
            {
                "app.rb": "class App\n  def run; end\nend\n",
                "settings.json": '{\n  "enabled": true\n}\n',
            },
            3,
        ),
    ]
    for name, files, expected_loc in cases:
        repo = tmp_path / name
        repo.mkdir()
        for relative_path, content in files.items():
            (repo / relative_path).write_text(content, encoding="utf-8")

        metadata = pipeline.run(
            repo,
            Settings(repo=repo, jobs=1),
            MemorySink(),
        )

        assert metadata.totalLinesOfCode == expected_loc


def test_diff_metadata_loc_excludes_config_records() -> None:
    from breezeai_cog.schemas import FileRecord
    from breezeai_cog.services.diff import _InfraStreamSink

    class Upload:
        def write_line(self, _line: str) -> None:
            pass

    sink = _InfraStreamSink(Upload(), None)
    sink.write(FileRecord(id="ruby", path="app.rb", type="code", language="ruby", loc=3))
    sink.write(FileRecord(id="json", path="config.json", type="config", language="config", loc=7))

    assert sink.loc == 3


def test_diff_metadata_class_count_excludes_ruby_modules_only() -> None:
    from breezeai_cog.schemas import Class, FileRecord
    from breezeai_cog.services.diff import _InfraStreamSink

    class Upload:
        def write_line(self, _line: str) -> None:
            pass

    sink = _InfraStreamSink(Upload(), None)
    sink.write(FileRecord(
        id="ruby", path="app.rb", type="code", language="ruby", loc=0,
        classes=[
            Class(
                id="ruby-class", parentId="ruby", path="app.rb", name="Widget",
                type="class", startLine=1, endLine=2,
            ),
            Class(
                id="ruby-module", parentId="ruby", path="app.rb", name="Helpers",
                type="module", startLine=3, endLine=4,
            ),
        ],
    ))
    sink.write(FileRecord(
        id="python", path="app.py", type="code", language="python", loc=0,
        classes=[
            Class(
                id="python-class", parentId="python", path="app.py", name="Widget",
                type="class", startLine=1, endLine=2,
            ),
            Class(
                id="python-module", parentId="python", path="app.py", name="Helpers",
                type="module", startLine=3, endLine=4,
            ),
        ],
    ))

    assert sink.classes == 3


def _make_markup_repo(root) -> None:
    (root / "app.py").write_text("def top():\n    return 1\n")
    (root / "page.html").write_text("<div *ngFor='let x of xs'>{{ x }}</div>\n")
    (root / "Default.aspx").write_text("<%@ Page %>\n")


def test_templates_skipped_by_default(tmp_path) -> None:
    repo = tmp_path / "markup"
    repo.mkdir()
    _make_markup_repo(repo)

    records = list(iter_file_records(repo))
    assert sorted(r.path for r in records) == ["app.py"]


def test_capture_templates_includes_markup(tmp_path) -> None:
    repo = tmp_path / "markup"
    repo.mkdir()
    _make_markup_repo(repo)

    records = list(iter_file_records(repo, capture_templates=True))
    assert sorted(r.path for r in records) == ["Default.aspx", "app.py", "page.html"]


def test_template_skips_are_counted_and_reconcile(tmp_path) -> None:
    """A `template` skip joins the reason tally and keeps
    `scanned == parsed + failed + skipped` intact."""
    from breezeai_cog.config import Settings
    from breezeai_cog.core import pipeline
    from breezeai_cog.core.skips import SkipReport

    repo = tmp_path / "markup"
    repo.mkdir()
    _make_markup_repo(repo)

    report = SkipReport()
    entries = list(pipeline._scan_entries(repo, Settings(repo=repo), report))
    assert report.counts["template"] == 2  # page.html + Default.aspx
    assert len(entries) == 1
    # markup is not misfiled as an unsupported extension
    assert ".html" not in report.extensions and ".aspx" not in report.extensions


def test_vue_import_edges_never_dangle(tmp_path) -> None:
    """A `.ts` importing a skipped `.vue` must not keep an edge to a File node that was
    never emitted (parsers/typescript/imports.py resolves `.vue` on purpose)."""
    repo = tmp_path / "vue"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Avatar.vue").write_text("<template><b/></template>\n")
    (repo / "src" / "router.ts").write_text(
        "import { createRouter } from 'vue-router'\nimport Avatar from './Avatar.vue'\n"
        "export const router = createRouter({ routes: [{ path: '/a', component: Avatar }] })\n"
    )

    default = {r.path: r for r in iter_file_records(repo)}
    assert "src/Avatar.vue" not in default
    assert default["src/router.ts"].importFiles == []  # pruned, not dangling

    opted_in = {r.path: r for r in iter_file_records(repo, capture_templates=True)}
    assert "src/Avatar.vue" in opted_in
    assert opted_in["src/router.ts"].importFiles == ["src/Avatar.vue"]
