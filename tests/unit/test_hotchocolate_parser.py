"""C# HotChocolate framework parser: attribute-declared operations, the enforced field-name
transformation, DTO/guard/loader capture, requestDTO honest-null, capture gating, selection
against the sibling C# parsers, and schema validity."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.imports import CSharpIndex
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.index_common import ClassHeritage
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


# ---- [ExtendObjectType] --------------------------------------------------------------------

# Three extension classes: one extending the root by schema name, one extending a data type
# (with and without an explicit [Parent]), and one whose target cannot be classified here.
EXTENSIONS = b'''
using HotChocolate;
using HotChocolate.Types;
namespace Catalog.Books {
  [ExtendObjectType(OperationTypeNames.Query)]
  public class ReportQueries {
    public Report GetSalesReport(int year) => null;
  }

  [ExtendObjectType(typeof(Book))]
  public class BookFields {
    public Task<Author> GetAuthor([Parent] Book book, IDataLoader<int, Author> loader) => null;
    public IEnumerable<Review> GetReviews(Book book, CatalogDb db) => null;
  }

  [ExtendObjectType(typeof(CatalogApi))]
  public class CatalogFields {
    public Report GetSummary(int year) => null;
  }
}
'''

# The root lives in another file and carries [MutationType]; the extension targets it by CLR
# name, so it only resolves through the repo heritage index.
CROSS_FILE_EXTENSION = b'''
using HotChocolate;
namespace Catalog.Books {
  [ExtendObjectType(typeof(BookMutations))]
  public class ArchiveMutations {
    public Book ArchiveBook(int id) => null;
  }
}
'''


def _index(heritage):
    """A real CSharpIndex carrying only the heritage entries a test needs."""
    return CSharpIndex(class_heritage=heritage)


def _parse_with_index(src, name, index):
    ctx = ParseContext(
        path=name, abs_path=None, source=src, repo_root=None,
        capture_statements=True, resolution_index=index,
    )
    return CSharpHotChocolateParser().parse_file(ctx)


def test_root_extension_yields_operations() -> None:
    # [ExtendObjectType(OperationTypeNames.Query)] names the root by its schema name.
    rec = _parse(CSharpHotChocolateParser(), EXTENSIONS, "Extensions.cs")
    op = _by_endpoint(rec)["salesReport"]
    assert op.routeKind == "query" and op.method == "QUERY" and op.handler == "GetSalesReport"


def test_data_type_extension_yields_field_resolvers() -> None:
    rec = _parse(CSharpHotChocolateParser(), EXTENSIONS, "Extensions.cs")
    ops = _by_endpoint(rec)
    for endpoint in ("Book.author", "Book.reviews"):
        assert ops[endpoint].routeKind == "field_resolver"
        assert ops[endpoint].method == "QUERY"
    assert ops["Book.author"].dataLoaders == ["IDataLoader<int, Author>"]
    assert ops["Book.reviews"].responseDTO == "Review"


def test_parent_binding_by_convention_is_honoured() -> None:
    # GetReviews has no [Parent]; its first parameter is typed as the extended type, which the
    # resolver compiler binds as the parent — an enforced signal, so it is not dropped.
    rec = _parse(CSharpHotChocolateParser(), EXTENSIONS, "Extensions.cs")
    assert "Book.reviews" in _by_endpoint(rec)


def test_unclassifiable_target_emits_nothing() -> None:
    # CatalogApi is a root only because Program.cs registers it; this parser does not read that,
    # and no method binds a CatalogApi parent — so the class is skipped rather than guessed at.
    rec = _parse(CSharpHotChocolateParser(), EXTENSIONS, "Extensions.cs")
    assert "summary" not in _by_endpoint(rec)
    assert "CatalogApi.summary" not in _by_endpoint(rec)


def test_root_attributed_in_another_file_resolves_via_heritage() -> None:
    from breezeai_cog.schemas import Decorator

    index = _index({"BookMutations": ClassHeritage(extends=None, decorators=[Decorator(name="MutationType")])})
    rec = _parse_with_index(CROSS_FILE_EXTENSION, "ArchiveMutations.cs", index)
    op = _by_endpoint(rec)["archiveBook"]
    assert op.routeKind == "mutation" and op.method == "MUTATION"


def test_ambiguous_heritage_entry_is_not_resolved() -> None:
    # A None entry means the name is declared by more than one class — honest-null, so the
    # extension stays unresolved instead of picking one.
    rec = _parse_with_index(CROSS_FILE_EXTENSION, "ArchiveMutations.cs", _index({"BookMutations": None}))
    assert _routes(rec) == []


# ---- subscriptions -------------------------------------------------------------------------

# Four subscription forms: a literal topic, a bare [Topic] (defaults to the field name), no
# [Topic] at all (same default), and the receiver-driven form whose topic is built in the body.
SUBSCRIPTIONS = b'''
using HotChocolate;
using HotChocolate.Subscriptions;
using HotChocolate.Types;
namespace Catalog.Books {
  [SubscriptionType]
  public class BookSubscriptions {
    [Subscribe]
    [Topic("bookAdded")]
    public Book OnBookAdded([EventMessage] Book book) => book;

    [Subscribe]
    [Topic]
    public Review OnReviewPosted([EventMessage] Review review) => review;

    [Subscribe]
    public Book OnBookRemoved([EventMessage] Book book) => book;

    [SubscribeAndResolve]
    public async IAsyncEnumerable<Book> OnBookUpdated(int bookId, ITopicEventReceiver receiver) {
      var stream = await receiver.SubscribeAsync<Book>($"bookUpdated_{bookId}");
    }
  }
}
'''


def _consumers(rec: FileRecord):
    return [s for s in rec.statements if s.semanticType == "eventbus_consumer"]


def test_subscription_emits_route_and_topic_consumer() -> None:
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    route = _by_endpoint(rec)["onBookAdded"]
    assert route.routeKind == "subscription" and route.method == "SUBSCRIPTION"
    consumer = next(s for s in _consumers(rec) if s.endpoint == "bookAdded")
    # Two addresses, one method: the client names the field, the server reads the topic.
    assert consumer.parentId == route.parentId and consumer.id != route.id
    assert consumer.framework == "graphql" and consumer.handler == "OnBookAdded"


def test_bare_and_absent_topic_default_to_the_field_name() -> None:
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    topics = {s.endpoint for s in _consumers(rec)}
    assert "onReviewPosted" in topics   # [Topic] with no argument
    assert "onBookRemoved" in topics    # no [Topic] at all — same framework default


def test_runtime_built_topic_emits_the_route_only() -> None:
    # The topic is interpolated inside SubscribeAsync, so there is no address to record. The
    # subscription field is still captured; only the consumer half is withheld.
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    assert "onBookUpdated" in _by_endpoint(rec)
    assert not [s for s in _consumers(rec) if "bookUpdated" in (s.endpoint or "")]


def test_consumers_do_not_inflate_the_route_inventory() -> None:
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    assert len(_routes(rec)) == 4       # one per subscription field
    assert len(_consumers(rec)) == 3    # the resolvable topics only


def test_non_subscription_operations_emit_no_consumer() -> None:
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    assert _consumers(rec) == []


def test_consumer_anchors_at_the_topic_attribute() -> None:
    # The consumer record exists because of [Topic("bookAdded")], so its span points there —
    # not at the whole member, which is what the route covers.
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    route = _by_endpoint(rec)["onBookAdded"]
    consumer = next(s for s in _consumers(rec) if s.endpoint == "bookAdded")
    topic_line = next(i for i, line in enumerate(SUBSCRIPTIONS.decode().splitlines(), 1)
                      if '[Topic("bookAdded")]' in line)
    assert consumer.startLine == consumer.endLine == topic_line
    assert route.startLine < consumer.startLine <= route.endLine  # inside the member's span


def test_consumer_falls_back_to_the_subscribe_attribute() -> None:
    # No [Topic] at all: the declaring syntax is [Subscribe], so anchor there.
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    consumer = next(s for s in _consumers(rec) if s.endpoint == "onBookRemoved")
    line = consumer.startLine
    assert '[Subscribe]' in SUBSCRIPTIONS.decode().splitlines()[line - 1]


def test_consumer_ids_are_independent_of_the_route() -> None:
    # Each id comes from the position of the syntax that produced it, so no consumer id is a
    # disambiguated duplicate of its route's — ids are the backend's unique key.
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    ids = [s.id for s in rec.statements]
    assert len(ids) == len(set(ids))
    assert not [i for i in ids if "#" in i]


# ---- fluent descriptor style ---------------------------------------------------------------

# A root descriptor (string form, property form, a fluent rename, an ignored field) and a data
# descriptor whose fields are shape, not endpoints.
DESCRIPTORS = b'''
using HotChocolate.Types;
namespace Catalog {
  public class QueryType : ObjectType<Query> {
    protected override void Configure(IObjectTypeDescriptor<Query> d) {
      d.Field("books").Resolve(ctx => ctx.Service<CatalogDb>().Books);
      d.Field(f => f.Stats).Type<StatsType>();
      d.Field("legacySearch").Name("search");
      d.Field("internalOnly").Ignore();
    }
  }

  public class BookType : ObjectType<Book> {
    protected override void Configure(IObjectTypeDescriptor<Book> d) {
      d.Field(f => f.Title).Type<StringType>();
      d.Field(f => f.Isbn).Ignore();
    }
  }
}
'''

# The described root is named only by the Configure parameter type (no generic base).
DESCRIPTOR_VIA_PARAM = b'''
using HotChocolate.Types;
namespace Catalog {
  public class MutationType : ObjectType {
    protected override void Configure(IObjectTypeDescriptor<Mutation> d) {
      d.Field("addBook").Resolve(ctx => null);
    }
  }
}
'''


def test_root_descriptor_fields_are_operations() -> None:
    rec = _parse(CSharpHotChocolateParser(), DESCRIPTORS, "Types.cs")
    ops = _by_endpoint(rec)
    assert set(ops) == {"books", "stats", "search"}
    for s in ops.values():
        assert s.routeKind == "query" and s.method == "QUERY"
        # A fluent field has a real backing node, unlike the attribute-derived routes.
        assert s.nodeType == "invocation_expression"


def test_property_expression_field_takes_the_framework_casing() -> None:
    rec = _parse(CSharpHotChocolateParser(), DESCRIPTORS, "Types.cs")
    assert "stats" in _by_endpoint(rec)   # Field(f => f.Stats)


def test_fluent_rename_wins_over_the_declared_name() -> None:
    rec = _parse(CSharpHotChocolateParser(), DESCRIPTORS, "Types.cs")
    ops = _by_endpoint(rec)
    assert "search" in ops and "legacySearch" not in ops


def test_ignored_field_is_not_an_operation() -> None:
    rec = _parse(CSharpHotChocolateParser(), DESCRIPTORS, "Types.cs")
    assert "internalOnly" not in _by_endpoint(rec)


def test_data_descriptor_fields_are_not_operations() -> None:
    # BookType describes Book's shape; title/isbn are fields of a type, not endpoints. Emitting
    # them is the over-capture the sibling graphql-dotnet parser measured.
    rec = _parse(CSharpHotChocolateParser(), DESCRIPTORS, "Types.cs")
    endpoints = set(_by_endpoint(rec))
    assert "title" not in endpoints and "isbn" not in endpoints


def test_descriptor_target_from_the_configure_parameter() -> None:
    rec = _parse(CSharpHotChocolateParser(), DESCRIPTOR_VIA_PARAM, "MutationType.cs")
    op = _by_endpoint(rec)["addBook"]
    assert op.routeKind == "mutation" and op.method == "MUTATION"


def test_descriptor_fields_are_emitted_in_source_order() -> None:
    rec = _parse(CSharpHotChocolateParser(), DESCRIPTORS, "Types.cs")
    lines = [s.startLine for s in _routes(rec)]
    assert lines == sorted(lines)


def test_descriptor_routes_parented_to_configure() -> None:
    rec = _parse(CSharpHotChocolateParser(), DESCRIPTORS, "Types.cs")
    configure_ids = {f.id for f in rec.functions if f.name == "Configure"}
    assert {s.parentId for s in _routes(rec)} <= configure_ids
