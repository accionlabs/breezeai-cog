"""Spark .read / .write / .sql detection tests (D7 fallback) + schema validation."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.scala.parser import ScalaParser
from breezeai_cog.schemas import FileRecord

SPARK_SRC = b"""package com.example.jobs

import org.apache.spark.sql.SparkSession

class SparkJob {
  def run(spark: SparkSession): Unit = {
    val df1 = spark.read.format("csv").option("header", "true").load("data/in.csv")
    val df2 = spark.read.parquet("data/input.parquet")
    df1.write.format("parquet").mode("overwrite").save("data/out.parquet")
    df2.write.csv("data/output.csv")
    val res = spark.sql("SELECT id, name FROM users WHERE active = true")
  }
}
"""

NON_SPARK_SRC = b"""package com.example
class NonSparkLoader {
  def loadData(): Unit = {
    val cfg = new ConfigLoader().load("app.conf")
    new ImageSaver().save("out.png")
  }
}
"""


def _parse_spark(tmp_path: Path, src: bytes = SPARK_SRC, name: str = "SparkJob.scala") -> FileRecord:
    parser = ScalaParser()
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src)
    ctx = ParseContext(
        path=name,
        abs_path=p,
        source=src,
        repo_root=tmp_path,
        capture_statements=True,
        statement_text_limit=200,
    )
    return parser.parse_file(ctx)


def test_spark_read_write_detection(tmp_path: Path) -> None:
    rec = _parse_spark(tmp_path)
    assert rec.framework == "spark"
    stmts = rec.statements

    # Case 8: spark.read.format(...).load(...) -> db_method_call, hint=spark, method=read
    read_calls = [
        s for s in stmts
        if s.semanticType == "db_method_call" and s.dataAccessHint == "spark" and s.method == "read"
    ]
    assert len(read_calls) == 2
    assert any(s.endpoint == "data/in.csv" for s in read_calls)
    assert any(s.endpoint == "data/input.parquet" for s in read_calls)

    # Case 9: df.write.format(...).save(...) -> method=write
    write_calls = [
        s for s in stmts
        if s.semanticType == "db_method_call" and s.dataAccessHint == "spark" and s.method == "write"
    ]
    assert len(write_calls) == 2
    assert any(s.endpoint == "data/out.parquet" for s in write_calls)
    assert any(s.endpoint == "data/output.csv" for s in write_calls)

    # Case 10: spark.sql("SELECT ...") -> query_statement
    sql_stmts = [s for s in stmts if s.semanticType == "query_statement"]
    assert len(sql_stmts) == 1
    assert "SELECT" in sql_stmts[0].text


def test_non_spark_file_not_detected(tmp_path: Path) -> None:
    # Case 11: Non-Spark file with .load()/.save() -> no org.apache.spark guard hit -> no spark detections
    rec = _parse_spark(tmp_path, NON_SPARK_SRC, "NonSpark.scala")
    spark_stmts = [s for s in rec.statements if s.dataAccessHint == "spark"]
    assert len(spark_stmts) == 0
    assert rec.framework is None


def test_spark_schema_validation(tmp_path: Path) -> None:
    # Case 12: schema validation
    rec = _parse_spark(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
