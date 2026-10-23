from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.ruby.parser import RubyParser


def _parse(tmp_path: Path, file_path: str, source: bytes):
    absolute = tmp_path / file_path
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(source)
    return RubyParser().parse_file(ParseContext(
        path=file_path,
        abs_path=absolute,
        source=source,
        repo_root=tmp_path,
    ))


def test_require_relative_without_dot_is_relative_to_importing_file(tmp_path: Path) -> None:
    (tmp_path / "lib" / "app").mkdir(parents=True)
    (tmp_path / "lib" / "app" / "helper.rb").write_text("class NestedHelper; end\n")
    (tmp_path / "helper.rb").write_text("class RootHelper; end\n")

    record = _parse(tmp_path, "lib/app/main.rb", b"require_relative 'helper'\n")

    assert record.importFiles == ["lib/app/helper.rb"]
    assert record.externalImports == []


def test_nested_require_relative_normalizes_parent_segments(tmp_path: Path) -> None:
    target = tmp_path / "lib" / "app" / "shared" / "helper.rb"
    target.parent.mkdir(parents=True)
    target.write_text("class SharedHelper; end\n")

    record = _parse(
        tmp_path,
        "lib/app/tasks/job.rb",
        b"require_relative '../shared/helper'\n",
    )

    assert record.importFiles == ["lib/app/shared/helper.rb"]
    assert record.externalImports == []


def test_stdlib_require_does_not_bind_same_named_internal_files(tmp_path: Path) -> None:
    (tmp_path / "lib" / "logger.rb").parent.mkdir(parents=True)
    (tmp_path / "lib" / "logger.rb").write_text("class ProjectLogger; end\n")

    record = _parse(tmp_path, "lib/app/main.rb", b"require 'logger'\n")

    assert "lib/logger.rb" not in record.importFiles
    assert record.importFiles == []
    assert record.externalImports == ["logger"]


def test_bare_require_resolves_unique_targets_under_lib_roots(tmp_path: Path) -> None:
    (tmp_path / "lib" / "sinatra.rb").parent.mkdir(parents=True)
    (tmp_path / "lib" / "sinatra.rb").write_text("module Sinatra; end\n")
    (tmp_path / "lib" / "sinatra" / "base.rb").parent.mkdir(parents=True)
    (tmp_path / "lib" / "sinatra" / "base.rb").write_text("class Base; end\n")
    (tmp_path / "lib" / "rack" / "protection.rb").parent.mkdir(parents=True)
    (tmp_path / "lib" / "rack" / "protection.rb").write_text("module Rack; end\n")

    record = _parse(
        tmp_path,
        "examples/app.rb",
        b"require 'sinatra'\nrequire 'sinatra/base'\nrequire 'rack/protection'\n",
    )

    assert record.importFiles == ["lib/sinatra.rb", "lib/sinatra/base.rb", "lib/rack/protection.rb"]
    assert record.externalImports == []


def test_bare_require_does_not_search_importer_directory_and_keeps_external_gems(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "third_party.rb").write_text("class ThirdParty; end\n")

    record = _parse(
        tmp_path,
        "app/main.rb",
        b"require 'third_party'\nrequire 'rack-protection'\n",
    )

    assert record.importFiles == []
    assert record.externalImports == ["third_party", "rack-protection"]


def test_ambiguous_bare_require_stays_external(tmp_path: Path) -> None:
    for target in (tmp_path / "lib" / "shared.rb", tmp_path / "engine" / "lib" / "shared.rb"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("class Shared; end\n")

    record = _parse(tmp_path, "app/main.rb", b"require 'shared'\n")

    assert record.importFiles == []
    assert record.externalImports == ["shared"]


def test_unresolved_and_dynamic_requires_emit_no_internal_relationship(tmp_path: Path) -> None:
    record = _parse(
        tmp_path,
        "app/main.rb",
        b"require_relative 'missing/helper'\nrequire \"feature/#{name}\"\n",
    )

    assert record.importFiles == []
    assert record.externalImports == ["missing/helper"]
