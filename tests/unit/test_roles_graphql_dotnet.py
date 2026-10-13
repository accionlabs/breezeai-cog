"""Role-table tests for graphql-dotnet (docs/entry-point-roles/graphql-dotnet.md)."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}

QUERY = """using GraphQL.Types;

namespace Books;

public class BookQuery : ObjectGraphType
{
    public BookQuery(IBookRepository repo)
    {
        Field<BookType>("book").Resolve(ctx => repo.Find(1));
        Field<BookType>("namedBook").Resolve(ResolveBook);
    }

    private Book ResolveBook(IResolveFieldContext ctx) => new Book();
}
"""

MUTATION = """using GraphQL.Types;

namespace Books;

public class BookMutation : ObjectGraphType
{
    public BookMutation()
    {
        Field<BookType>("addBook").Resolve(ctx => new Book());
    }
}
"""

DATA_TYPE = """using GraphQL.Types;

namespace Books;

public class BookType : ObjectGraphType<Book>
{
    public BookType()
    {
        Field(x => x.Title);
        Field<AuthorType>("author").Resolve(ctx => new Author());
    }
}
"""

SCHEMA = """using GraphQL.Types;

namespace Books;

public class BookSchema : Schema
{
    public BookSchema(IServiceProvider sp) : base(sp)
    {
        Query = sp.GetRequiredService<RootQueries>();
    }
}
"""

OFF_CONVENTION_ROOT = """using GraphQL.Types;

namespace Books;

public class RootQueries2 : ObjectGraphType
{
    public RootQueries2()
    {
        Field<BookType>("latest").Resolve(ctx => new Book());
    }
}
"""


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def _line(src: str, snippet: str) -> int:
    return next(i for i, line in enumerate(src.splitlines(), 1) if snippet in line)


def test_operation_type_is_wiring(analyze_files) -> None:
    stmts = analyze_files({"BookQuery.cs": QUERY})
    cls = _line(QUERY, "public class BookQuery")
    assert [s for s in _eps(stmts) if s.startLine == cls] == []


def test_field_on_query_root_is_binding(analyze_files) -> None:
    stmts = analyze_files({"BookQuery.cs": QUERY})
    [r] = [s for s in _eps(stmts) if s.endpoint == "book"]
    assert (r.semanticType, r.method, r.routeKind, r.framework) == (
        "route",
        "QUERY",
        "query",
        "graphql",
    )


def test_field_on_mutation_root_is_binding(analyze_files) -> None:
    stmts = analyze_files({"BookMutation.cs": MUTATION})
    [r] = _eps(stmts)
    assert (r.method, r.routeKind, r.endpoint) == ("MUTATION", "mutation", "addBook")


def test_inline_resolver_is_anonymous(analyze_files) -> None:
    stmts = analyze_files({"BookQuery.cs": QUERY})
    [r] = [s for s in _eps(stmts) if s.endpoint == "book"]
    assert not r.handler and r.handlerLine == _line(QUERY, 'Field<BookType>("book")')


def test_named_resolver_is_handler(analyze_files) -> None:
    stmts = analyze_files({"BookQuery.cs": QUERY})
    [r] = [s for s in _eps(stmts) if s.endpoint == "namedBook"]
    assert (r.handler, r.handlerLine) == ("ResolveBook", _line(QUERY, "private Book ResolveBook"))


def test_data_type_field_is_none(analyze_files) -> None:
    stmts = analyze_files({"BookType.cs": DATA_TYPE})
    assert _eps(stmts) == []


def test_schema_root_assignment_is_wiring(analyze_files) -> None:
    stmts = analyze_files({"BookSchema.cs": SCHEMA})
    assert _eps(stmts) == []


@pytest.mark.xfail(strict=True, reason="root named off the Query/Mutation convention not found")
def test_root_assigned_in_schema_is_found(analyze_files) -> None:
    schema = SCHEMA.replace("RootQueries", "RootQueries2")
    stmts = analyze_files({"BookSchema.cs": schema, "RootQueries2.cs": OFF_CONVENTION_ROOT})
    [r] = _eps(stmts)
    assert (r.method, r.endpoint) == ("QUERY", "latest")
