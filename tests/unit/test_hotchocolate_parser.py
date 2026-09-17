"""C# HotChocolate framework parser: attribute-declared operations, the enforced field-name
transformation, DTO/guard/loader capture, requestDTO honest-null, capture gating, selection
against the sibling C# parsers, and schema validity."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.csharp_hotchocolate.parser import CSharpHotChocolateParser
from breezeai_cog.schemas import FileRecord

# A query root: the naming convention (Get/Async stripped, camelCased), an explicit
# [GraphQLName] override, a [GraphQLIgnore]d member, a non-public helper, paging middleware,
# a DataLoader parameter, and an [Authorize] guard inherited from the class.
QUERIES = b'''
using HotChocolate;
using HotChocolate.Types;
namespace Catalog.Books {
  [QueryType]
  [Authorize]
  public class BookQueries {
    [UsePaging]
    [UseFiltering]
    public IQueryable<Book> GetBooks(CatalogDb db) => db.Books;

    public Task<Book> GetBookByIdAsync([GraphQLName("id")] int bookId, CatalogDb db) => null;

    [GraphQLName("bookSearch")]
    public IEnumerable<Book> SearchBooks(
        string term, IDataLoader<int, Author> loader, AuthorDataLoader named) => null;

    [GraphQLIgnore]
    public Book Hidden() => null;

    private Book Helper() => null;
  }
}
'''

# A mutation root with a method-level policy guard, plus a subscription root.
MUTATIONS = b'''
using HotChocolate;
namespace Catalog.Books {
  [MutationType]
  public class BookMutations {
    [Authorize(Policy = "Admin")]
    [UseMutationConvention]
    public async Task<Book> AddBook([GraphQLName("input")] AddBookInput input, CatalogDb db) => null;
  }

  [SubscriptionType]
  public class BookSubscriptions {
    public Book OnBookAdded([EventMessage] Book book) => book;
  }
}
'''

# graphql-dotnet: must stay with csharp-graphql even though it mentions HotChocolate in a using.
GRAPHQL_DOTNET = b'''
using HotChocolate;
using GraphQL.Types;
namespace Api {
  public class Query : ObjectGraphType {
    public Query() { Field<StringType>("ping"); }
  }
}
'''


def _parse(parser, src, name, *, capture=True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None, capture_statements=capture)
    return parser.parse_file(ctx)


def _routes(rec: FileRecord):
    return [s for s in rec.statements if s.semanticType == "route"]


def _by_endpoint(rec: FileRecord) -> dict[str, object]:
    return {s.endpoint: s for s in _routes(rec)}


def test_query_operations_detected() -> None:
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    assert {s.endpoint for s in _routes(rec)} == {"books", "bookById", "bookSearch"}
    assert rec.framework == "graphql"
    fn_ids = {f.id for f in rec.functions}
    for s in _routes(rec):
        assert s.parentId in fn_ids  # parented to the resolver method
        assert s.routeKind == "query" and s.method == "QUERY"
        assert s.framework == "graphql" and s.nodeType == "synthetic"


def test_field_name_is_the_wire_name_not_the_method_name() -> None:
    # DefaultNamingConventions strips Get/Async and camelCases; handler keeps the C# name.
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    ops = _by_endpoint(rec)
    assert ops["bookById"].handler == "GetBookByIdAsync"
    assert ops["books"].handler == "GetBooks"


def test_graphql_name_attribute_overrides_the_convention() -> None:
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    ops = _by_endpoint(rec)
    assert ops["bookSearch"].handler == "SearchBooks"  # not "searchBooks"


def test_ignored_and_non_public_members_excluded() -> None:
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    endpoints = {s.endpoint for s in _routes(rec)}
    assert "hidden" not in endpoints   # [GraphQLIgnore]
    assert "helper" not in endpoints   # private


def test_response_dto_is_the_declared_type_not_the_generated_wrapper() -> None:
    # [UsePaging] makes the client see a Connection<Book>; that type exists nowhere in the
    # repo, so the declared inner type is recorded instead.
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    ops = _by_endpoint(rec)
    assert ops["books"].responseDTO == "Book"
    assert ops["bookById"].responseDTO == "Book"


def test_middleware_attributes_kept_on_decorators() -> None:
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    names = {d.name for d in _by_endpoint(rec)["books"].decorators}
    assert names == {"UsePaging", "UseFiltering"}


def test_request_dto_only_from_an_argument_marker() -> None:
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    ops = _by_endpoint(rec)
    # [GraphQLName] on the parameter marks it as a real argument.
    assert ops["bookById"].requestDTO == "int"
    # An unattributed CatalogDb is a DI-injected service, not a request type → honest-null.
    assert ops["books"].requestDTO is None


def test_dataloader_parameters_captured() -> None:
    # Only the declared generic loader type is matched. `AuthorDataLoader named` is skipped: a
    # `…DataLoader` name suffix is a team convention, and the enforced fact (its base type) is
    # in another file.
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    assert _by_endpoint(rec)["bookSearch"].dataLoaders == ["IDataLoader<int, Author>"]


def test_class_level_authorize_becomes_a_guard() -> None:
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    for s in _routes(rec):
        assert s.authRequired is True and s.guards == ["Authorize"]


def test_mutation_and_subscription_kinds() -> None:
    rec = _parse(CSharpHotChocolateParser(), MUTATIONS, "BookMutations.cs")
    ops = _by_endpoint(rec)
    assert ops["addBook"].routeKind == "mutation" and ops["addBook"].method == "MUTATION"
    assert ops["addBook"].requestDTO == "AddBookInput"
    assert ops["addBook"].guards == ["Authorize(Admin)"]  # policy name, per the C# attr extractor
    assert ops["onBookAdded"].routeKind == "subscription"


def test_no_statements_without_capture_flag() -> None:
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs", capture=False)
    assert _routes(rec) == []
    assert rec.classes and rec.functions  # structural capture is unaffected


def test_fixture_files_emit_no_routes() -> None:
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "__mocks__/BookQueries.cs")
    assert _routes(rec) == []


def test_selection_prefers_hotchocolate_over_base_csharp() -> None:
    registry.discover_builtin()
    assert registry.select("BookQueries.cs", QUERIES).name == "csharp-hotchocolate"


def test_graphql_dotnet_file_stays_with_its_own_parser() -> None:
    # The guards are mutually exclusive: an ObjectGraphType file is never claimed here, so the
    # equal-claim tie that registry.select would resolve by registration order cannot arise.
    registry.discover_builtin()
    assert not CSharpHotChocolateParser().claims("Query.cs", GRAPHQL_DOTNET)
    assert registry.select("Query.cs", GRAPHQL_DOTNET).name == "csharp-graphql"


def test_plain_csharp_is_untouched() -> None:
    plain = b"namespace A { public class B { public int C() => 1; } }"
    assert not CSharpHotChocolateParser().claims("B.cs", plain)
    rec = _parse(CSharpParser(), plain, "B.cs")
    assert _routes(rec) == []


def test_records_validate_against_the_schema() -> None:
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    for src, name in ((QUERIES, "BookQueries.cs"), (MUTATIONS, "BookMutations.cs")):
        rec = _parse(CSharpHotChocolateParser(), src, name)
        errors = list(validator.iter_errors(json.loads(to_line(rec))))
        assert not errors, errors
