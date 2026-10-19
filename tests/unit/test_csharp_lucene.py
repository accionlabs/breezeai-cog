"""Lucene.NET index access in C#: read/write detection gated on the receiver's declared type,
the look-alike calls it must leave alone, and capture gating."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.schemas import FileRecord

# A search service in the shape real Lucene code takes: a searcher and a writer held as fields, a
# reader as a property, query construction alongside the access, and document building.
SEARCH_SERVICE = b'''using Lucene.Net.Index;
using Lucene.Net.Search;
namespace Cis {
  public class ContractSearch {
    private readonly IndexSearcher _searcher;
    private IndexWriter _writer;
    public IndexReader Reader { get; set; }

    public IEnumerable<int> Find(string term, QueryParser parser) {
      var query = parser.Parse(term);
      var hits = _searcher.Search(query, 50);
      var top = _searcher.Doc(hits.ScoreDocs[0].Doc);
      var stored = Reader.Document(3);
      return new[] { 1 };
    }

    public void Index(Contract c) {
      var doc = new Document();
      doc.Add(new Field("title", c.Title, Field.Store.YES, Field.Index.ANALYZED));
      _writer.AddDocument(doc);
      _writer.Commit();
      _writer.Optimize();
    }

    public void Reopen(Directory dir) {
      IndexReader local = DirectoryReader.Open(dir);
      var cached = _memo.Search("x");
      var sb = new StringBuilder();
      sb.Append("Search");
      var n = _list.Count();
    }
  }
}
'''

# Same method names, no Lucene type anywhere — nothing here is index access.
LOOK_ALIKES = b'''using System.Text;
namespace Cis {
  public class ReportBuilder {
    private readonly ICacheService _memo;
    private readonly List<int> _list;

    public void Build() {
      var hit = _memo.Search("contracts");
      var one = _list.Count();
      var sb = new StringBuilder();
      sb.Append("Search");
      _repo.Commit();
    }
  }
}
'''


def _parse(src: bytes, name: str = "ContractSearch.cs", *, capture: bool = True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None,
                       capture_statements=capture)
    return CSharpParser().parse_file(ctx)


def _access(rec: FileRecord):
    return [s for s in rec.statements if s.semanticType == "db_method_call"]


def test_reads_and_writes_detected() -> None:
    rec = _parse(SEARCH_SERVICE)
    assert {s.method for s in _access(rec)} == {
        "Search", "Doc", "Document", "AddDocument", "Commit", "Optimize", "Open"}


def test_receiver_may_be_a_field_a_property_or_the_type_itself() -> None:
    rec = _parse(SEARCH_SERVICE)
    by_method = {s.method: s for s in _access(rec)}
    assert by_method["Search"].text.startswith("_searcher.")     # field
    assert by_method["Document"].text.startswith("Reader.")       # property
    assert by_method["Open"].text.startswith("DirectoryReader.")  # static factory


def test_query_and_document_construction_are_not_access() -> None:
    # Building a query or a document is ordinary code; only the calls that touch the index are
    # marked. Their text is still captured, so nothing is lost.
    rec = _parse(SEARCH_SERVICE)
    texts = [s.text for s in _access(rec)]
    assert not any("parser.Parse" in t for t in texts)
    assert not any("doc.Add" in t for t in texts)
    all_text = " ".join(s.text for s in rec.statements)
    assert "parser.Parse(term)" in all_text and "doc.Add(new Field" in all_text


def test_look_alike_calls_on_untyped_receivers_are_ignored() -> None:
    # Same verbs, receivers not declared as a Lucene type -- the verb alone must never qualify.
    rec = _parse(SEARCH_SERVICE)
    texts = [s.text for s in _access(rec)]
    for noise in ("_memo.Search", "sb.Append", "_list.Count"):
        assert not any(noise in t for t in texts), noise


def test_a_file_without_lucene_yields_nothing() -> None:
    rec = _parse(LOOK_ALIKES, "ReportBuilder.cs")
    assert _access(rec) == []


def test_access_is_parented_to_the_enclosing_method() -> None:
    rec = _parse(SEARCH_SERVICE)
    fn_ids = {f.id for f in rec.functions}
    for s in _access(rec):
        assert s.parentId in fn_ids


def test_nodetype_is_the_real_call_node() -> None:
    # The call is a genuine AST node, so nothing here is synthetic.
    rec = _parse(SEARCH_SERVICE)
    assert {s.nodeType for s in _access(rec)} == {"invocation_expression"}


def test_no_hint_is_invented() -> None:
    # The spec's dataAccessHint enum has no `lucene`, and the near values name other products.
    rec = _parse(SEARCH_SERVICE)
    assert all(s.dataAccessHint is None for s in _access(rec))


def test_no_access_without_capture_flag() -> None:
    rec = _parse(SEARCH_SERVICE, capture=False)
    assert _access(rec) == []
    assert rec.classes and rec.functions  # structural capture is unaffected


def test_records_validate_against_the_schema() -> None:
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    rec = _parse(SEARCH_SERVICE)
    errors = list(validator.iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
