"""Tests for emit: id convention, NDJSON serialization, and the file sink's
projectMetaData-first output."""

from __future__ import annotations

import gzip
import json

from breezeai_cog.emit import (
    FileSink,
    class_id,
    disambiguate,
    file_id,
    function_id,
    statement_id,
    to_line,
)
from breezeai_cog.schemas import FileRecord, Function, ProjectMetaData


def test_id_convention() -> None:
    assert file_id("a/b.py") == "a/b.py"
    assert class_id("a.py", "Foo") == "a.py#Foo"
    assert function_id("a.py", "getX", 15) == "a.py#getX@15"
    assert function_id("a.py", "getX", 15, class_name="Ctrl") == "a.py#Ctrl#getX@15"
    assert function_id("a.py", None, 7) == "a.py#<anonymous>@7"  # arrow/lambda
    assert statement_id("a.py", 3, 4) == "a.py:3:4"


def test_disambiguate_ordinal() -> None:
    seen: set[str] = set()
    a = disambiguate("a.py#f@1", seen)
    b = disambiguate("a.py#f@1", seen)  # collision (e.g. overload on same line)
    c = disambiguate("a.py#f@1", seen)
    assert (a, b, c) == ("a.py#f@1", "a.py#f@1#2", "a.py#f@1#3")


def test_to_line_roundtrip() -> None:
    rec = FileRecord(id="a.py", path="a.py", type="code", language="python", loc=1)
    line = to_line(rec)
    assert line.endswith("\n")
    parsed = json.loads(line)
    assert parsed["id"] == "a.py" and parsed["type"] == "code"
    assert "metadata" not in parsed  # None fields omitted


def _meta() -> ProjectMetaData:
    return ProjectMetaData(
        repositoryName="r", analyzedLanguages=["python"], totalFiles=2,
        totalFunctions=1, totalClasses=0, totalLinesOfCode=3,
        generatedAt="2026-06-29T00:00:00Z", toolVersion="0.0.0",
    )


def test_file_sink_metadata_first(tmp_path) -> None:
    out = tmp_path / "repo-project-analysis.ndjson.gz"
    sink = FileSink(out)
    sink.write(FileRecord(id="a.py", path="a.py", type="code", language="python", loc=1,
                          functions=[Function(id="a.py#f@1", parentId="a.py", name="f",
                                              type="function", startLine=1, endLine=1)]))
    sink.write(FileRecord(id="b.py", path="b.py", type="code", language="python", loc=2))
    sink.finalize(_meta())

    assert out.exists()
    assert not (tmp_path / "repo-project-analysis.ndjson.gz.body.tmp").exists()  # temp cleaned

    lines = gzip.open(out, "rt", encoding="utf-8").read().splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["__type"] == "projectMetaData"  # FIRST line
    assert [r["path"] for r in records[1:]] == ["a.py", "b.py"]
    assert records[1]["functions"][0]["id"] == "a.py#f@1"
