"""Role-table tests for HotChocolate (docs/entry-point-roles/hotchocolate.md)."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

EP = {"route", "eventbus_consumer", "timer"}

QUERIES = """using HotChocolate;
using HotChocolate.Authorization;
using HotChocolate.Types;

namespace Books;

[QueryType]
public class BookQueries
{
    public Book GetBookById(int id) => new Book();

    [Authorize]
    public Book GetSecret() => new Book();

    internal Book Hidden() => new Book();
}
"""

EXTEND_ROOT = """using HotChocolate.Types;

namespace Books;

[ExtendObjectType(OperationTypeNames.Mutation)]
public class BookMutations
{
    public Book AddBook(string title) => new Book();
}
"""

EXTEND_DATA = """using HotChocolate;
using HotChocolate.Types;

namespace Books;

[ExtendObjectType(typeof(Book))]
public class BookExtensions
{
    public Author GetAuthor([Parent] Book book) => new Author();
}
"""

SUBSCRIPTIONS = """using HotChocolate;
using HotChocolate.Types;

namespace Books;

[SubscriptionType]
public class BookSubscriptions
{
    [Subscribe]
    [Topic("book-added")]
    public Book OnBookAdded([EventMessage] Book book) => book;
}
"""

DESCRIPTOR = """using HotChocolate.Types;

namespace Books;

public class QueryType : ObjectType<Query>
{
    protected override void Configure(IObjectTypeDescriptor<Query> descriptor)
    {
        descriptor.Field("books").Resolve(ctx => new Book[0]);
    }
}
"""

REGISTERED = """namespace Books;

public class AuthorQueries
{
    public Author GetAuthorById(int id) => new Author();
}
"""

PROGRAM = """using Books;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.DependencyInjection;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddGraphQLServer().AddQueryType<AuthorQueries>();
var app = builder.Build();
app.MapGraphQL();
app.Run();
"""

REFERENCE = """using HotChocolate;
using HotChocolate.ApolloFederation.Types;
using HotChocolate.Types;

namespace Books;

[ExtendObjectType(typeof(Book))]
public class BookResolvers
{
    [ReferenceResolver]
    public static Book ResolveBook(int id) => new Book();
}
"""

NODE = """using HotChocolate.Types;
using HotChocolate.Types.Relay;

namespace Books;

[Node]
[ExtendObjectType(typeof(Book))]
public class BookNode
{
    [NodeResolver]
    public static Book GetBookAsync(int id) => new Book();
}
"""


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _line(src: str, snippet: str) -> int:
    return next(i for i, line in enumerate(src.splitlines(), 1) if snippet in line)


def test_root_type_attribute_is_wiring(analyze_files) -> None:
    stmts = analyze_files({"BookQueries.cs": QUERIES})
    assert [s for s in _eps(stmts) if s.startLine == _line(QUERIES, "public class")] == []


def test_public_method_of_root_is_binding(analyze_files) -> None:
    stmts = analyze_files({"BookQueries.cs": QUERIES})
    [r] = [s for s in _eps(stmts) if s.handler == "GetBookById"]
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.framework) == (
        "route",
        "QUERY",
        "query",
        "bookById",
        "graphql",
    )
    assert r.handlerLine == _line(QUERIES, "GetBookById")


def test_non_public_member_is_none(analyze_files) -> None:
    stmts = analyze_files({"BookQueries.cs": QUERIES})
    assert [s for s in _eps(stmts) if s.handler == "Hidden"] == []


def test_extend_root_is_wiring_members_are_bindings(analyze_files) -> None:
    stmts = analyze_files({"BookMutations.cs": EXTEND_ROOT})
    [r] = _eps(stmts)
    assert (r.method, r.routeKind, r.endpoint, r.handler) == (
        "MUTATION",
        "mutation",
        "addBook",
        "AddBook",
    )


def test_add_query_type_registration_is_wiring(analyze_files) -> None:
    stmts = analyze_files({"AuthorQueries.cs": REGISTERED, "Program.cs": PROGRAM})
    [r] = [s for s in _eps(stmts) if s.path == "AuthorQueries.cs"]
    assert (r.method, r.endpoint, r.handler) == ("QUERY", "authorById", "GetAuthorById")
    assert [
        s
        for s in _eps(stmts)
        if s.path == "Program.cs" and s.startLine == _line(PROGRAM, "AddQueryType")
    ] == []


def test_descriptor_field_on_root_is_binding(analyze_files) -> None:
    stmts = analyze_files({"QueryType.cs": DESCRIPTOR})
    [r] = _eps(stmts)
    assert (r.method, r.routeKind, r.endpoint) == ("QUERY", "query", "books")


REGISTERED_DESCRIPTOR = """using HotChocolate.Types;
namespace Books;
public class QueryType : ObjectType<Query>
{
    protected override void Configure(IObjectTypeDescriptor<Query> descriptor)
    {
        descriptor.Field(t => t.GetHero(default));
    }
}
"""

ROOT_CLASS = """namespace Books;
public class Query
{
    public Character GetHero(Episode episode) => new Character();
}
"""


def test_registered_descriptor_leads_to_the_described_types_method(analyze_files) -> None:
    # `AddQueryType<QueryType>()` makes the descriptor a root; its field `t => t.GetHero(…)` is
    # the method of the type it describes (`ObjectType<Query>` → Query), in another file.
    program = PROGRAM.replace("AddQueryType<AuthorQueries>", "AddQueryType<QueryType>")
    stmts = analyze_files({"QueryType.cs": REGISTERED_DESCRIPTOR, "Query.cs": ROOT_CLASS,
                           "Program.cs": program})
    [r] = [s for s in _eps(stmts) if s.path == "QueryType.cs"]
    assert (r.method, r.endpoint, r.handler) == ("QUERY", "hero", "GetHero")
    assert (r.handlerPath, r.handlerLine) == ("Query.cs", _line(ROOT_CLASS, "GetHero"))


def test_registration_binds_only_a_type_in_scope(analyze_files) -> None:
    # Without `using Books;` the registration cannot name Books.AuthorQueries (it would not
    # compile): no binding is made rather than a guessed one.
    program = PROGRAM.replace("using Books;\n", "")
    stmts = analyze_files({"AuthorQueries.cs": REGISTERED, "Program.cs": program})
    assert [s for s in _eps(stmts) if s.path == "AuthorQueries.cs"] == []


def test_descriptor_inline_resolver_is_anonymous(analyze_files) -> None:
    stmts = analyze_files({"QueryType.cs": DESCRIPTOR})
    [r] = _eps(stmts)
    assert not r.handler and r.handlerLine == _line(DESCRIPTOR, 'descriptor.Field("books")')


def test_descriptor_field_handler_is_never_the_field_name(analyze_files) -> None:
    src = """using HotChocolate.Types;
namespace Books;
public class QueryType : ObjectType<Query>
{
    protected override void Configure(IObjectTypeDescriptor<Query> descriptor)
    {
        descriptor.Field(q => q.GetAuthors());
        descriptor.Field("byId").ResolveWith<BookResolvers>(r => r.GetById(default));
        descriptor.Field("titles").ResolveWith<TitleResolvers>(r => r.All());
        descriptor.Field("plain");
    }
}
public class BookResolvers
{
    public Book GetById(int id) => new Book();
}
"""
    got = {r.endpoint: (r.handler, r.handlerLine) for r in _eps(analyze_files({"QueryType.cs": src}))}
    assert got == {
        "authors": ("GetAuthors", None),          # the runtime type's member, declared elsewhere
        "byId": ("GetById", _line(src, "public Book GetById")),
        "titles": ("All", None),                  # a resolver class in another file
        "plain": (None, None),                    # resolved by convention: not in front of us
    }


def test_data_type_field_is_none(analyze_files) -> None:
    stmts = analyze_files({"BookExtensions.cs": EXTEND_DATA})
    assert _eps(stmts) == []
    assert [s.routeKind for s in stmts if s.semanticType == "route"] == ["field_resolver"]


def test_authorize_is_guard(analyze_files) -> None:
    stmts = analyze_files({"BookQueries.cs": QUERIES})
    [r] = [s for s in _eps(stmts) if s.handler == "GetSecret"]
    assert r.guards == ["Authorize"]
    assert len(_eps(stmts)) == 2  # the guard adds no record of its own


def test_subscription_is_binding(analyze_files) -> None:
    stmts = analyze_files({"BookSubscriptions.cs": SUBSCRIPTIONS})
    [r] = [s for s in _eps(stmts) if s.semanticType == "route"]
    assert (r.method, r.routeKind, r.endpoint) == ("SUBSCRIPTION", "subscription", "onBookAdded")


def test_subscription_topic_is_consumer(analyze_files) -> None:
    stmts = analyze_files({"BookSubscriptions.cs": SUBSCRIPTIONS})
    [r] = [s for s in _eps(stmts) if s.semanticType == "eventbus_consumer"]
    assert (r.endpoint, r.handler) == ("book-added", "OnBookAdded")


def test_reference_resolver_is_binding(analyze_files) -> None:
    stmts = analyze_files({"BookResolvers.cs": REFERENCE})
    [r] = _eps(stmts)
    assert (r.method, r.routeKind, r.endpoint, r.handler) == (
        "RESOLVE_REFERENCE",
        "query",
        "Book",
        "ResolveBook",
    )


def test_node_resolver_is_binding(analyze_files) -> None:
    stmts = analyze_files({"BookNode.cs": NODE})
    [r] = _eps(stmts)
    assert (r.method, r.routeKind, r.endpoint, r.handler) == (
        "RESOLVE_REFERENCE",
        "query",
        "Book",
        "GetBookAsync",
    )



def test_node_resolver_on_the_query_root_is_also_a_field(analyze_files) -> None:
    src = """using System.Threading.Tasks;
using HotChocolate.Types;
using HotChocolate.Types.Relay;

[QueryType]
public class SessionQueries
{
    [NodeResolver]
    public Task<Session?> GetSessionByIdAsync(int id) => null!;
}
"""
    stmts = analyze_files({"SessionQueries.cs": src})
    # One method, two triggers: the query field and the Relay node resolver.
    got = sorted((r.method, r.endpoint, r.handler) for r in _eps(stmts))
    assert got == [
        ("QUERY", "sessionById", "GetSessionByIdAsync"),
        ("RESOLVE_REFERENCE", "Session", "GetSessionByIdAsync"),
    ]


def test_fluent_node_resolver_is_binding(analyze_files) -> None:
    src = """using HotChocolate.Types;
using HotChocolate.Types.Relay;

public class AttendeeType : ObjectType<Attendee>
{
    protected override void Configure(IObjectTypeDescriptor<Attendee> descriptor)
    {
        descriptor
            .ImplementsNode()
            .IdField(a => a.Id)
            .ResolveNode((ctx, id) => ctx.DataLoader<AttendeeByIdDataLoader>().LoadAsync(id));
    }
}
"""
    stmts = analyze_files({"AttendeeType.cs": src})
    [r] = _eps(stmts)
    line = _line(src, ".ResolveNode(")
    # An inline handler: no name, the lambda's line.
    assert (r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine, r.startLine) == (
        "RESOLVE_REFERENCE", "query", "Attendee", None, line, line,
    )

def test_map_graphql_is_a_post_route(analyze_files) -> None:
    # The Target Spec's current form: the transport mount is a POST route with no handler.
    stmts = analyze_files({"Program.cs": PROGRAM})
    assert [(s.method, s.endpoint, s.handler) for s in _eps(stmts)] == [("POST", "/graphql", None)]


def test_add_graphql_server_is_none(analyze_files) -> None:
    stmts = analyze_files({"Program.cs": PROGRAM})
    line = _line(PROGRAM, "AddGraphQLServer")
    assert [s for s in _eps(stmts) if s.startLine == line] == []
