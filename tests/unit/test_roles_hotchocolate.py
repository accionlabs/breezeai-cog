"""Role-table tests for HotChocolate (docs/entry-point-roles/hotchocolate.md)."""

from __future__ import annotations

import pytest

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

PROGRAM = """using Microsoft.AspNetCore.Builder;
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
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


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


@pytest.mark.xfail(
    strict=True, reason="root registered by AddQueryType<T>() in another file not resolved"
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


@pytest.mark.xfail(strict=True, reason="inline descriptor resolver: handler holds the field name")
def test_descriptor_inline_resolver_is_anonymous(analyze_files) -> None:
    stmts = analyze_files({"QueryType.cs": DESCRIPTOR})
    [r] = _eps(stmts)
    assert not r.handler and r.handlerLine == _line(DESCRIPTOR, 'descriptor.Field("books")')


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


@pytest.mark.xfail(strict=True, reason="entity reference resolver not recorded")
def test_reference_resolver_is_binding(analyze_files) -> None:
    stmts = analyze_files({"BookResolvers.cs": REFERENCE})
    [r] = _eps(stmts)
    assert (r.method, r.routeKind, r.endpoint, r.handler) == (
        "RESOLVE_REFERENCE",
        "query",
        "Book",
        "ResolveBook",
    )


@pytest.mark.xfail(strict=True, reason="Relay node resolver not recorded")
def test_node_resolver_is_binding(analyze_files) -> None:
    stmts = analyze_files({"BookNode.cs": NODE})
    [r] = _eps(stmts)
    assert (r.method, r.routeKind, r.endpoint, r.handler) == (
        "RESOLVE_REFERENCE",
        "query",
        "Book",
        "GetBookAsync",
    )


@pytest.mark.xfail(strict=True, reason="GraphQL transport mount recorded as a route")
def test_map_graphql_is_wiring(analyze_files) -> None:
    stmts = analyze_files({"Program.cs": PROGRAM})
    assert _eps(stmts) == []


def test_add_graphql_server_is_none(analyze_files) -> None:
    stmts = analyze_files({"Program.cs": PROGRAM})
    line = _line(PROGRAM, "AddGraphQLServer")
    assert [s for s in _eps(stmts) if s.startLine == line] == []
