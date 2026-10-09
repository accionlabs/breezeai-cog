"""C# HotChocolate framework parser: attribute-declared operations, the enforced field-name
transformation, DTO/guard/loader capture, requestDTO honest-null, capture gating, selection
against the sibling C# parsers, and schema validity."""

from __future__ import annotations

import gzip
import json

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog import analyze_repo
from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.imports import CSharpIndex, build_csharp_index
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.index_common import ClassHeritage
from breezeai_cog.parsers.csharp_hotchocolate.parser import CSharpHotChocolateParser
from breezeai_cog.parsers.csharp_hotchocolate.registered_roots import (
    NAME as REGISTERED_ROOTS,
    RegisteredRoots,
    registered_roots,
)
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


def test_route_records_carry_no_operation_annotations() -> None:
    # The spec reserves a statement's `decorators` for field/property annotations on a
    # declaration; an operation annotation is decomposed into method/endpoint/routeKind. The
    # attributes stay on the owning Function, where the base parser records them.
    rec = _parse(CSharpHotChocolateParser(), QUERIES, "BookQueries.cs")
    assert _by_endpoint(rec)["books"].decorators == []
    owner = next(f for f in rec.functions if f.name == "GetBooks")
    assert {"UsePaging", "UseFiltering"} <= {d.name for d in owner.decorators}


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
        assert ops[endpoint].method == "RESOLVE_FIELD"  # the spec's verb for a field resolver
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

    [Subscribe(With = nameof(SubscribeToRemoved))]
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


def test_bare_topic_emits_no_consumer() -> None:
    # [Topic] with no argument defaults to the *member* name, not the GraphQL field name --
    # ChilliCream's own workshop pairs it with a publisher sending to nameof(TheMethodAsync).
    # Recording the field name would be a fabricated address, so nothing is emitted.
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    topics = {s.endpoint for s in _consumers(rec)}
    assert "onReviewPosted" not in topics


def test_subscribe_with_stream_method_emits_no_consumer() -> None:
    # [Subscribe(With = nameof(...))] moves the topic into that method's body, where it is
    # commonly interpolated -- unreadable from the declaration.
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    assert "onBookRemoved" not in {s.endpoint for s in _consumers(rec)}


def test_runtime_built_topic_emits_the_route_only() -> None:
    # The topic is interpolated inside SubscribeAsync, so there is no address to record. The
    # subscription field is still captured; only the consumer half is withheld.
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    assert "onBookUpdated" in _by_endpoint(rec)
    assert not [s for s in _consumers(rec) if "bookUpdated" in (s.endpoint or "")]


def test_consumers_do_not_inflate_the_route_inventory() -> None:
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIPTIONS, "BookSubscriptions.cs")
    assert len(_routes(rec)) == 4       # one per subscription field
    assert len(_consumers(rec)) == 1    # only the literal [Topic("bookAdded")]


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


def test_nullable_return_type_is_not_recorded_as_the_dto() -> None:
    # Task<Book?> unwraps to Book, not "Book?" -- the annotation is not part of the type name and
    # would name no captured class.
    src = SUBSCRIPTIONS.replace(b"public Book OnBookAdded", b"public Book? OnBookAdded")
    rec = _parse(CSharpHotChocolateParser(), src, "BookSubscriptions.cs")
    assert _by_endpoint(rec)["onBookAdded"].responseDTO == "Book"


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


def test_method_expression_field_takes_the_resolver_naming_convention() -> None:
    # Field(t => t.GetHero(default)) binds a method; HotChocolate names it as it names any
    # resolver (Get/Async stripped, camel-cased). Only a call on the lambda's own parameter
    # selects a member of the type, so t => Helpers.Make(t) names nothing.
    src = b"""using HotChocolate.Types;
namespace StarWars {
  public class QueryType : ObjectType<Query> {
    protected override void Configure(IObjectTypeDescriptor<Query> d) {
      d.Field(t => t.GetHero(default)).Type<CharacterType>();
      d.Field(t => t.GetCharacter<int>(default, default));
      d.Field((Query t) => t.Search(default));
      d.Field(t => t.GetReviewsAsync(default)).Name("allReviews");
      d.Field(t => Helpers.Make(t));
    }
  }
}
"""
    ops = _by_endpoint(_parse(CSharpHotChocolateParser(), src, "QueryType.cs"))
    assert set(ops) == {"hero", "character", "search", "allReviews"}
    assert ops["hero"].routeKind == "query"


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


# ---- parser selection: the composition root ------------------------------------------------

# The application's composition root: it registers the GraphQL server *and* maps REST endpoints.
# csharp-aspnet owns it and already emits the GraphQL HTTP mount itself.
PROGRAM = b'''using Microsoft.AspNetCore.Builder;
using HotChocolate.AspNetCore;
var builder = WebApplication.CreateBuilder(args);
builder.Services.AddGraphQLServer().AddQueryType<BookQueries>();
var app = builder.Build();
app.MapGet("/health", () => "ok");
app.MapPost("/webhook/{id}", (string id) => Results.Ok());
app.MapGraphQL("/graphql");
app.Run();
'''

# Mentions the library but declares no schema — nothing here for this parser to find.
MENTION_ONLY = b'''using HotChocolate;
namespace Catalog {
  public class BookMapper {
    public Book Map(BookDto dto) => new Book { Title = dto.Title };
  }
}
'''

# A subscription extension using [SubscribeAndResolve] rather than [Subscribe].
SUBSCRIBE_AND_RESOLVE = b'''using HotChocolate;
namespace Catalog {
  [ExtendObjectType(OperationTypeNames.Subscription)]
  public class LiveFeed {
    [SubscribeAndResolve]
    public async IAsyncEnumerable<Book> OnFeed(ITopicEventReceiver receiver) { yield break; }
  }
}
'''


def test_composition_root_is_not_claimed() -> None:
    # Claiming Program.cs would trade the whole application's route inventory for nothing.
    assert not CSharpHotChocolateParser().claims("Program.cs", PROGRAM)


def test_composition_root_keeps_its_routes() -> None:
    registry.discover_builtin()
    assert registry.select("Program.cs", PROGRAM).name == "csharp-aspnet"
    rec = _parse(registry.select("Program.cs", PROGRAM), PROGRAM, "Program.cs")
    routes = {(s.method, s.endpoint, s.framework) for s in _routes(rec)}
    assert ("GET", "/health", "aspnet") in routes
    assert ("POST", "/webhook/{id}", "aspnet") in routes
    # The GraphQL mount is captured there too, so nothing is lost by declining the file.
    assert ("POST", "/graphql", "graphql") in routes


def test_library_mention_alone_does_not_claim() -> None:
    # The guard is positive on schema *declarations*, not on a using directive — the composition
    # root imports the library as well.
    assert not CSharpHotChocolateParser().claims("BookMapper.cs", MENTION_ONLY)


def test_subscribe_and_resolve_is_claimed_and_captured() -> None:
    assert CSharpHotChocolateParser().claims("LiveFeed.cs", SUBSCRIBE_AND_RESOLVE)
    rec = _parse(CSharpHotChocolateParser(), SUBSCRIBE_AND_RESOLVE, "LiveFeed.cs")
    op = _by_endpoint(rec)["onFeed"]
    assert op.routeKind == "subscription"
    # The topic is built from the receiver inside the body, so no consumer record.
    assert _consumers(rec) == []


def test_declared_error_types_stay_on_the_function() -> None:
    # [Error<T>] annotates the operation, so it is not repeated on the route record -- but it is
    # still in the graph, on the method that declares it.
    src = b'''using HotChocolate;
namespace Catalog {
  [MutationType]
  public class BookMutations {
    [Error<TitleEmptyException>]
    [Error<NoSpeakerException>]
    public Book AddBook(Book book) => book;
  }
}
'''
    rec = _parse(CSharpHotChocolateParser(), src, "BookMutations.cs")
    assert _by_endpoint(rec)["addBook"].decorators == []
    owner = next(f for f in rec.functions if f.name == "AddBook")
    assert {"Error<TitleEmptyException>", "Error<NoSpeakerException>"} == {d.name for d in owner.decorators}


def test_named_attribute_argument_resolves_the_target() -> None:
    # HotChocolate's own tests write the target as a named argument, with a comment inside the
    # attribute. Missing that put the whole argument text into the endpoint.
    src = b'''using HotChocolate.Types;
namespace Catalog {
  [ExtendObjectType(
      // extends every type inheriting this one
      extendsType: typeof(Book))]
  public class Extensions {
    public string Any([Parent] Book parent) => null;
  }
}
'''
    rec = _parse(CSharpHotChocolateParser(), src, "Extensions.cs")
    op = _by_endpoint(rec)["Book.any"]
    assert op.routeKind == "field_resolver"


def test_multi_argument_generic_return_keeps_the_outer_type() -> None:
    # FieldResult<T, TError> has no single payload type; the concatenation "string, FooException"
    # is not a type name, so the declared outer type is recorded instead.
    src = b'''using HotChocolate;
namespace Catalog {
  [MutationType]
  public class M {
    public FieldResult<string, FooException> DoSomething(string s) => null;
  }
}
'''
    rec = _parse(CSharpHotChocolateParser(), src, "M.cs")
    assert _by_endpoint(rec)["doSomething"].responseDTO == "FieldResult"


# ---- generated DataLoader types -------------------------------------------------------------

# A resolver taking the interface HotChocolate's generator emits for a [DataLoader] method that
# lives in another file. The interface itself exists nowhere in the source.
GENERATED_LOADER_USE = b'''using HotChocolate;
namespace Catalog {
  [QueryType]
  public class SessionQueries {
    public Task<Session> GetSessionByIdAsync(
        int id, ISessionByIdDataLoader sessionById, CatalogDb db) => null;
  }
}
'''

LOADER_DECLARATION = b'''namespace Catalog {
  public static class SessionDataLoaders {
    [DataLoader]
    public static async Task<IReadOnlyDictionary<int, Session>> SessionByIdAsync(
        IReadOnlyList<int> ids, CatalogDb db) => null;
  }
}
'''


def test_generated_loader_is_recognised_via_the_index() -> None:
    from breezeai_cog.parsers.csharp.imports import _index_data_loaders
    from breezeai_cog.parsers.treesitter import parse_source

    # Build the index entry the repo pre-pass would produce from the declaring file.
    index = CSharpIndex()
    root = parse_source("csharp", LOADER_DECLARATION, 0).root_node
    _index_data_loaders(root, LOADER_DECLARATION, "SessionDataLoaders.cs", index)
    assert "ISessionByIdDataLoader" in index.data_loaders

    rec = _parse_with_index(GENERATED_LOADER_USE, "SessionQueries.cs", index)
    assert _by_endpoint(rec)["sessionById"].dataLoaders == ["ISessionByIdDataLoader"]


def test_named_loader_stays_unrecognised_without_the_index() -> None:
    # No index entry means no evidence the type is a loader -- the `…DataLoader` name alone is a
    # convention, so the field stays empty rather than matching on the suffix.
    rec = _parse(CSharpHotChocolateParser(), GENERATED_LOADER_USE, "SessionQueries.cs")
    assert _by_endpoint(rec)["sessionById"].dataLoaders is None


# A field resolver on an [ExtendObjectType] class taking a source-generated loader. The wiring
# for this path was missed once: the loader map reached root operations but not extensions.
EXTENSION_WITH_LOADER = b'''using HotChocolate;
using HotChocolate.Types;
namespace Shop {
  [ExtendObjectType(typeof(Order))]
  public static class OrderTypeExtensions {
    public static Task<Customer> GetCustomerAsync(
        [Parent] Order order, ICustomerByIdDataLoader customerById) => null;
  }
}
'''

LOADER_FOR_EXTENSION = b'''namespace Shop {
  public static class OrderDataLoaders {
    [DataLoader]
    public static Task<IReadOnlyDictionary<int, Customer>> CustomerByIdAsync(
        IReadOnlyList<int> ids, OrdersDbContext db) => null;
  }
}
'''


def test_field_resolver_resolves_a_generated_loader() -> None:
    from breezeai_cog.parsers.csharp.imports import _index_data_loaders
    from breezeai_cog.parsers.treesitter import parse_source

    index = CSharpIndex()
    root = parse_source("csharp", LOADER_FOR_EXTENSION, 0).root_node
    _index_data_loaders(root, LOADER_FOR_EXTENSION, "OrderDataLoaders.cs", index)

    rec = _parse_with_index(EXTENSION_WITH_LOADER, "OrderTypeExtensions.cs", index)
    op = _by_endpoint(rec)["Order.customer"]
    assert op.routeKind == "field_resolver"
    assert op.dataLoaders == ["ICustomerByIdDataLoader"]


def test_fluent_chain_with_an_intermediate_builder_call() -> None:
    # Real fluent code chains several builders before the resolver; the Field call must still be
    # found, and a later .Name() must still win over the declared name.
    src = b'''using HotChocolate.Types;
namespace Shop {
  public class ReportingQueryType : ObjectType<Query> {
    protected override void Configure(IObjectTypeDescriptor<Query> descriptor) {
      descriptor
          .Field("dailyRevenue")
          .Argument("day", a => a.Type<NonNullType<DateType>>())
          .Resolve(ctx => ctx.Service<ReportingService>().DailyRevenue());

      descriptor.Field("legacyTotals").Name("totals").Resolve(ctx => null);
      descriptor.Field("internalDebug").Ignore();
    }
  }
}
'''
    rec = _parse(CSharpHotChocolateParser(), src, "ReportingTypes.cs")
    assert set(_by_endpoint(rec)) == {"dailyRevenue", "totals"}


# ---- registration-only roots (AddQueryType<T>() with no attribute on the class) ----------------
#
# Older (v11/v12) codebases register a plain class as a root in the composition root. The C#
# repo index binds each registration to its declaring file, so these tests build a real index
# over a small repo rather than seeding parser state — a seeded parser is how the pipeline gap
# (the index never reaching the parser) went unnoticed.

SETUP = b"""using HotChocolate.AspNetCore;
using Catalog;
var builder = WebApplication.CreateBuilder(args);
builder.Services.AddGraphQLServer()
    .AddQueryType<BookQueries>()
    .AddMutationType<BookMutations>();
    // .AddSubscriptionType<Ghost>()  -- commented out, so not a registration
"""

BOOK_QUERIES = b"""namespace Catalog {
  public class BookQueries {
    public Task<Book> GetBookByIdAsync(int id) => null;
    public IQueryable<Book> GetBooks() => null;
    private Book Hidden() => null;
  }
}
"""

BOOK_MUTATIONS = b"""namespace Catalog {
  public class BookMutations {
    public Task<Book> AddBook(string title) => null;
  }
}
"""


def _repo_index(tmp_path, files: dict[str, bytes]) -> CSharpIndex:
    """Write ``files`` under ``tmp_path`` and build the real C# repo index over them."""
    paths = []
    for rel, src in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(src)
        if rel.endswith(".cs"):
            paths.append(f)
    return build_csharp_index(tmp_path, paths)


def _roots(index: CSharpIndex) -> RegisteredRoots:
    """The fact the registered-roots detector's index stage put on the C# index."""
    return registered_roots(index)


def _graphql_routes(rec: FileRecord) -> list:
    return [s for s in rec.statements if s.semanticType == "route" and s.framework == "graphql"]


def _parse_selected(index: CSharpIndex, rel: str, src: bytes) -> FileRecord:
    """Parse ``rel`` with whichever parser selection picks, as the pipeline does."""
    parser = registry.select(rel, src)
    ctx = ParseContext(path=rel, abs_path=None, source=src, repo_root=None,
                       capture_statements=True, resolution_index=index)
    return parser.parse_file(ctx)


def test_registrations_bind_to_the_declaring_files(tmp_path) -> None:
    index = _repo_index(tmp_path, {"Api/Program.cs": SETUP, "Api/BookQueries.cs": BOOK_QUERIES,
                                   "Api/BookMutations.cs": BOOK_MUTATIONS})
    assert _roots(index).files == {"Api/BookQueries.cs": {"BookQueries": "query"},
                                   "Api/BookMutations.cs": {"BookMutations": "mutation"}}
    assert _roots(index).types == {"BookQueries": "query", "BookMutations": "mutation"}
    # Only the resolved fact reaches the workers, not the raw registrations.
    assert set(index.facts) == {REGISTERED_ROOTS}


def test_registration_only_root_emits_operations_through_the_detector(tmp_path) -> None:
    # The file carries nothing HotChocolate, so it stays a plain C# file; the registered-roots
    # detector adds its operations from the index fact.
    index = _repo_index(tmp_path, {"Api/Program.cs": SETUP, "Api/BookQueries.cs": BOOK_QUERIES})
    assert registry.select("Api/BookQueries.cs", BOOK_QUERIES).name == "csharp"
    rec = _parse_selected(index, "Api/BookQueries.cs", BOOK_QUERIES)
    ops = _by_endpoint(rec)
    assert set(ops) == {"bookById", "books"}  # private methods are never exposed
    assert ops["bookById"].routeKind == "query" and ops["bookById"].method == "QUERY"
    assert rec.framework == "graphql"


def test_registration_only_class_is_plain_without_an_index() -> None:
    # Single-file parsing has no repo pre-pass, so nothing says this class is a root.
    rec = _parse(CSharpParser(), BOOK_QUERIES, "Api/BookQueries.cs")
    assert _graphql_routes(rec) == []


def test_a_file_only_referencing_a_registered_root_gets_no_routes(tmp_path) -> None:
    ref = b"namespace Catalog { public class BookService { public BookQueries Queries { get; } } }"
    index = _repo_index(tmp_path, {"Api/Program.cs": SETUP, "Api/BookQueries.cs": BOOK_QUERIES,
                                   "Api/BookService.cs": ref})
    assert _graphql_routes(_parse_selected(index, "Api/BookService.cs", ref)) == []


def test_registrations_on_any_builder_are_read(tmp_path) -> None:
    # AddGraphQL() (non-ASP.NET hosts) and the v10/v11 SchemaBuilder register roots exactly like
    # AddGraphQLServer(); missing them would leave those roots' fields looking like a data type's.
    host = b"using Catalog;\nservices.AddGraphQL().AddQueryType<BookQueries>();"
    legacy = b"using Catalog;\nvar schema = SchemaBuilder.New().AddMutationType<BookMutations>().Create();"
    index = _repo_index(tmp_path, {"Fn/Startup.cs": host, "Legacy/Schema.cs": legacy,
                                   "Api/BookQueries.cs": BOOK_QUERIES,
                                   "Api/BookMutations.cs": BOOK_MUTATIONS})
    assert _roots(index).files == {"Api/BookQueries.cs": {"BookQueries": "query"},
                                   "Api/BookMutations.cs": {"BookMutations": "mutation"}}


def test_a_registering_file_gets_no_routes_for_its_own_root(tmp_path) -> None:
    # The composition root declares its own root class and maps a REST endpoint. It stays with its
    # own parser and keeps its REST routes; the root still resolves by name for an extension
    # declared elsewhere.
    setup = b"""using HotChocolate;
var builder = WebApplication.CreateBuilder(args);
builder.Services.AddGraphQL().AddQueryType<RootQuery>();
var app = builder.Build();
app.MapGet("/health", () => "ok");
public class RootQuery { public string GetPing() => "pong"; }
"""
    ext = b"""using HotChocolate.Types;
[ExtendObjectType(typeof(RootQuery))]
public class Reports { public int GetDailyTotal() => 0; }
"""
    index = _repo_index(tmp_path, {"Api/Program.cs": setup, "Api/Reports.cs": ext})
    assert "Api/Program.cs" not in _roots(index).files
    assert _graphql_routes(_parse_selected(index, "Api/Program.cs", setup)) == []
    assert _roots(index).types == {"RootQuery": "query"}
    assert _by_endpoint(_parse_selected(index, "Api/Reports.cs", ext))["dailyTotal"].routeKind == "query"


def test_detector_and_parser_never_both_emit(tmp_path) -> None:
    # A registered root in a file the HotChocolate parser owns (it also holds an attribute-declared
    # extension): the parser emits the root's operations, the detector stays out — no duplicates.
    mixed = b"""using HotChocolate.Types;
namespace Catalog {
  public class BookQueries { public Book GetBookById(int id) => null; }
  [ExtendObjectType(typeof(Author))]
  public class AuthorBooks { public int GetBookCount([Parent] Author author) => 0; }
}
"""
    index = _repo_index(tmp_path, {"Api/Program.cs": SETUP, "Api/Types.cs": mixed})
    rec = _parse_selected(index, "Api/Types.cs", mixed)
    endpoints = [s.endpoint for s in _graphql_routes(rec)]
    assert sorted(endpoints) == ["Author.bookCount", "bookById"]


def test_same_named_roots_bind_per_service(tmp_path) -> None:
    # A monorepo: each service registers its own BookQueries, and an unregistered BookQueries
    # elsewhere stays plain. Keyed by simple name, all three would have been roots.
    def setup(ns: bytes) -> bytes:
        return b"using " + ns + b";\nbuilder.Services.AddGraphQLServer().AddQueryType<BookQueries>();"

    def queries(ns: bytes) -> bytes:
        return b"namespace " + ns + b" { public class BookQueries { public Book GetBook() => null; } }"

    index = _repo_index(tmp_path, {
        "Shop/Program.cs": setup(b"Shop.GraphQL"), "Shop/BookQueries.cs": queries(b"Shop.GraphQL"),
        "Admin/Program.cs": setup(b"Admin.GraphQL"), "Admin/BookQueries.cs": queries(b"Admin.GraphQL"),
        "Legacy/BookQueries.cs": queries(b"Legacy"),
    })
    assert set(_roots(index).files) == {"Shop/BookQueries.cs", "Admin/BookQueries.cs"}
    # Not every BookQueries is a root, so the name alone resolves nothing.
    assert "BookQueries" not in _roots(index).types
    legacy = queries(b"Legacy")
    assert _graphql_routes(_parse_selected(index, "Legacy/BookQueries.cs", legacy)) == []


def test_ambiguous_registration_binds_nothing(tmp_path) -> None:
    # Two in-scope namespaces both declare BookQueries and no project breaks the tie: the
    # compiler would reject it (CS0104), so nothing is guessed.
    setup = b"using A;\nusing B;\nbuilder.Services.AddGraphQLServer().AddQueryType<BookQueries>();"
    index = _repo_index(tmp_path, {
        "Program.cs": setup,
        "A/BookQueries.cs": b"namespace A { public class BookQueries { } }",
        "B/BookQueries.cs": b"namespace B { public class BookQueries { } }",
    })
    assert _roots(index).files == {}


def test_same_project_wins_a_registration_tie(tmp_path) -> None:
    # As the compiler does (CS0436): the BookQueries in the registering project beats an
    # identically-named one imported from another project.
    setup = b"using A;\nusing B;\nbuilder.Services.AddGraphQLServer().AddQueryType<BookQueries>();"
    index = _repo_index(tmp_path, {
        "Api/Api.csproj": b"<Project />",
        "Api/Program.cs": setup,
        "Api/BookQueries.cs": b"namespace A { public class BookQueries { } }",
        "Lib/Lib.csproj": b"<Project />",
        "Lib/BookQueries.cs": b"namespace B { public class BookQueries { } }",
    })
    assert set(_roots(index).files) == {"Api/BookQueries.cs"}


def test_conflicting_registrations_are_dropped(tmp_path) -> None:
    # Two composition roots register one class as different kinds: one is wrong, and nothing
    # here says which.
    index = _repo_index(tmp_path, {
        "Shop/Program.cs": b"using Shared;\nb.AddGraphQLServer().AddQueryType<Root>();",
        "Admin/Program.cs": b"using Shared;\nb.AddGraphQLServer().AddMutationType<Root>();",
        "Shared/Root.cs": b"namespace Shared { public class Root { } }",
    })
    assert _roots(index) == RegisteredRoots()


def test_partial_root_binds_every_part(tmp_path) -> None:
    index = _repo_index(tmp_path, {
        "Api/Program.cs": SETUP,
        "Api/BookQueries.cs": b"namespace Catalog { public partial class BookQueries { } }",
        "Api/BookQueries.Admin.cs": b"namespace Catalog { public partial class BookQueries { } }",
    })
    assert set(_roots(index).files) == {"Api/BookQueries.cs", "Api/BookQueries.Admin.cs"}


def test_same_named_types_in_other_projects_are_not_bound(tmp_path) -> None:
    # Several sample projects each declare ConferencePlanner.GraphQL.Query and register their own.
    # Same namespace, but different projects: each registration binds only its own project's
    # class, and an unregistered project's same-named class is never a root.
    query = b"namespace ConferencePlanner.GraphQL { public class Query { public int GetX() => 1; } }"
    setup = b"using ConferencePlanner.GraphQL;\nb.AddGraphQLServer().AddQueryType<Query>();"
    index = _repo_index(tmp_path, {
        "s1/S1.csproj": b"<Project />", "s1/Startup.cs": setup, "s1/Query.cs": query,
        "s2/S2.csproj": b"<Project />", "s2/Startup.cs": setup, "s2/Query.cs": query,
        "s3/S3.csproj": b"<Project />", "s3/Query.cs": query,
    })
    assert set(_roots(index).files) == {"s1/Query.cs", "s2/Query.cs"}


def test_a_root_in_a_referenced_project_is_bound(tmp_path) -> None:
    # The registering project declares no BookQueries; it references the library that does
    # (here transitively, through Core), so it binds there.
    index = _repo_index(tmp_path, {
        "Api/Api.csproj": b'<Project><ItemGroup><ProjectReference Include="..\\Core\\Core.csproj" /></ItemGroup></Project>',
        "Api/Program.cs": b"using Catalog;\nb.AddGraphQLServer().AddQueryType<BookQueries>();",
        "Core/Core.csproj": b'<Project><ItemGroup><ProjectReference Include="../Lib/Lib.csproj" /></ItemGroup></Project>',
        "Core/Marker.cs": b"namespace Core { class Marker { } }",
        "Lib/Lib.csproj": b"<Project />",
        "Lib/BookQueries.cs": BOOK_QUERIES,
    })
    assert index.project_refs["Api"] == {"Core", "Lib"}
    assert set(_roots(index).files) == {"Lib/BookQueries.cs"}


def test_a_root_in_an_unreferenced_project_is_not_bound(tmp_path) -> None:
    # Demo.Tests sees the parent namespace Demo, and an unrelated sample project declares
    # Demo.Subscription. The test project does not reference that sample, so the compiler could
    # never bind to it — neither do we.
    index = _repo_index(tmp_path, {
        "Tests/Tests.csproj": b'<Project><ItemGroup><ProjectReference Include="..\\Server\\Server.csproj" /></ItemGroup></Project>',
        "Tests/UnitTest1.cs": b"namespace Demo.Tests { class T { void M() { b.AddSubscriptionType<Subscription>(); } } }",
        "Server/Server.csproj": b"<Project />",
        "Server/Other.cs": b"namespace Demo.Server { class Other { } }",
        "Sample/Sample.csproj": b"<Project />",
        "Sample/Subscription.cs": b"namespace Demo { public class Subscription { public int GetX() => 1; } }",
    })
    assert _roots(index).files == {}


def test_project_file_usings_bring_namespaces_into_scope(tmp_path) -> None:
    # Program.cs has no using: the namespace comes from <Using Include> in the project file (or a
    # Directory.Build.props above it). A Static using imports a type, not a namespace.
    setup = b"b.AddGraphQLServer().AddQueryType<Query>().AddMutationType<Mutation>();"
    index = _repo_index(tmp_path, {
        "Directory.Build.props": b'<Project><ItemGroup><Using Include="Shared" /></ItemGroup></Project>',
        "App/App.csproj": (b'<Project><ItemGroup Condition="\'$(ImplicitUsings)\' == \'enable\'">'
                           b'<Using Include="SubscriptionDemo" />'
                           b'<Using Include="Other" Static="true" /></ItemGroup></Project>'),
        "App/Program.cs": setup,
        "App/Query.cs": b"namespace SubscriptionDemo { public class Query { public int GetX() => 1; } }",
        "App/Mutation.cs": b"namespace Shared { public class Mutation { public int DoY() => 1; } }",
    })
    assert index.project_usings == {"App": {"SubscriptionDemo", "Shared"}}
    assert _roots(index).files == {"App/Query.cs": {"Query": "query"},
                                   "App/Mutation.cs": {"Mutation": "mutation"}}


def test_extension_of_a_registered_root_yields_operations(tmp_path) -> None:
    # The target is declared in another file, so it resolves through the name-only lookup.
    ext = b"""using HotChocolate.Types;
namespace Catalog {
  [ExtendObjectType(typeof(BookQueries))]
  public class BookSearch { public Book SearchBooks(string term) => null; }
}
"""
    index = _repo_index(tmp_path, {"Api/Program.cs": SETUP, "Api/BookQueries.cs": BOOK_QUERIES,
                                   "Api/BookSearch.cs": ext})
    op = _by_endpoint(_parse_selected(index, "Api/BookSearch.cs", ext))["searchBooks"]
    assert op.routeKind == "query"


@pytest.mark.parametrize("jobs", [1, 2])
def test_registration_only_root_through_the_pipeline(tmp_path, jobs) -> None:
    # End to end: the index is built in the main process and must reach parser selection and
    # parsing in the workers. Unit tests over a hand-built index cannot see that wiring.
    repo = tmp_path / "repo"
    (repo / "Api").mkdir(parents=True)
    (repo / "Api" / "Program.cs").write_bytes(SETUP)
    (repo / "Api" / "BookQueries.cs").write_bytes(BOOK_QUERIES)
    result = analyze_repo(repo, capture_statements=True, out=tmp_path / "out", jobs=jobs)
    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    rec = next(json.loads(line) for line in lines if '"Api/BookQueries.cs"' in line)
    routes = {s["endpoint"]: s["method"] for s in rec["statements"] if s.get("semanticType") == "route"}
    assert routes == {"bookById": "QUERY", "books": "QUERY"}
    assert rec["framework"] == "graphql"


# v11/v12 fluent roots: AddQueryType<BookQueriesType>() registers the descriptor class itself,
# AddMutationType<BookMutations>() the type a descriptor describes. Neither carries an attribute.
REGISTRATION_ONLY_DESCRIPTORS = b"""using HotChocolate.Types;
namespace Catalog {
  public class BookQueriesType : ObjectType<BookQueries> {
    protected override void Configure(IObjectTypeDescriptor<BookQueries> d) {
      d.Field("books").Resolve(ctx => null);
    }
  }
  public class BookMutationsType : ObjectType<BookMutations> {
    protected override void Configure(IObjectTypeDescriptor<BookMutations> d) {
      d.Field("addBook").Resolve(ctx => null);
    }
  }
  public class BookType : ObjectType<Book> {
    protected override void Configure(IObjectTypeDescriptor<Book> d) {
      d.Field("title").Resolve(ctx => null);
    }
  }
}
"""


def test_registration_only_descriptor_emits_operations(tmp_path) -> None:
    setup = (b"using Catalog;\nbuilder.Services.AddGraphQLServer()\n"
             b"  .AddQueryType<BookQueriesType>().AddMutationType<BookMutations>();")
    index = _repo_index(tmp_path, {"Api/Program.cs": setup, "Api/Types.cs": REGISTRATION_ONLY_DESCRIPTORS,
                                   "Api/BookMutations.cs": BOOK_MUTATIONS})
    ops = _by_endpoint(_parse_selected(index, "Api/Types.cs", REGISTRATION_ONLY_DESCRIPTORS))
    # The registered descriptor and the descriptor of a registered type are roots; an
    # unregistered data type's fields stay shape.
    assert set(ops) == {"books", "addBook"}
    assert ops["books"].routeKind == "query" and ops["addBook"].routeKind == "mutation"


# The non-generic ObjectTypeExtension names the type it extends with d.Name(...), not a generic
# argument — the form both fluent root extensions in a real service took.
NAMED_EXTENSIONS = b"""using HotChocolate.Types;
namespace Shop {
  public class ReportQueries : ObjectTypeExtension {
    protected override void Configure(IObjectTypeDescriptor descriptor) {
      descriptor.Name(OperationTypeNames.Query);
      descriptor.Field("categories").Resolve(ctx => null);
    }
  }

  public class AdminMutations : ObjectTypeExtension {
    protected override void Configure(IObjectTypeDescriptor descriptor) {
      descriptor.Name("Mutation").Description("Admin operations");
      descriptor.Field("approveAllPendingOrders").Resolve(ctx => null);
    }
  }
}
"""


def test_named_extension_resolves_its_root() -> None:
    ops = _by_endpoint(_parse(CSharpHotChocolateParser(), NAMED_EXTENSIONS, "Extensions.cs"))
    assert set(ops) == {"categories", "approveAllPendingOrders"}
    assert ops["categories"].routeKind == "query"
    # Name(...) heading a chain still names the type.
    assert ops["approveAllPendingOrders"].routeKind == "mutation"


def test_unreadable_or_non_root_name_yields_no_routes() -> None:
    # A constant is not in front of us, a field-level .Name() renames the field rather than the
    # type, and a data type's fields are its shape — none of them is an endpoint.
    src = b"""using HotChocolate.Types;
namespace Shop {
  public class ByConstant : ObjectTypeExtension {
    protected override void Configure(IObjectTypeDescriptor d) {
      d.Name(Names.Query);
      d.Field("a").Resolve(ctx => null);
    }
  }
  public class FieldRename : ObjectTypeExtension {
    protected override void Configure(IObjectTypeDescriptor d) {
      d.Field("b").Name("Query").Resolve(ctx => null);
    }
  }
  public class DataType : ObjectTypeExtension {
    protected override void Configure(IObjectTypeDescriptor d) {
      d.Name("Book");
      d.Field("c").Resolve(ctx => null);
    }
  }
}
"""
    assert _routes(_parse(CSharpHotChocolateParser(), src, "Extensions.cs")) == []


# Fluent authorization: per field on the chain, and type-wide on the descriptor itself.
FLUENT_GUARDS = b"""using HotChocolate.Types;
namespace Shop {
  public class ReportQueries : ObjectTypeExtension<Query> {
    protected override void Configure(IObjectTypeDescriptor<Query> d) {
      d.Field("dailySalesReport").Authorize("Admin").Resolve(ctx => null);
      d.Field("categories").Resolve(ctx => null);
    }
  }
  public class AdminMutations : ObjectTypeExtension {
    protected override void Configure(IObjectTypeDescriptor d) {
      d.Name(OperationTypeNames.Mutation);
      d.Authorize();
      d.Field("notifyLowStock").Authorize().Resolve(ctx => null);
      d.Field("restock").Authorize(policy: "Ops").Resolve(ctx => null);
    }
  }
}
"""


def test_fluent_field_authorize_becomes_a_guard() -> None:
    ops = _by_endpoint(_parse(CSharpHotChocolateParser(), FLUENT_GUARDS, "Guards.cs"))
    report = ops["dailySalesReport"]
    assert report.guards == ["Authorize(Admin)"] and report.authRequired is True
    # An unguarded field stays unguarded, as an unattributed method does.
    assert ops["categories"].guards is None and ops["categories"].authRequired is None


def test_descriptor_authorize_guards_every_field() -> None:
    ops = _by_endpoint(_parse(CSharpHotChocolateParser(), FLUENT_GUARDS, "Guards.cs"))
    # Type-level Authorize() applies to every field and dedupes with an identical field one,
    # mirroring class-level [Authorize].
    assert ops["notifyLowStock"].guards == ["Authorize"]
    assert ops["restock"].guards == ["Authorize", "Authorize(Ops)"]
