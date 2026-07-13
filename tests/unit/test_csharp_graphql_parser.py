"""C# graphql-dotnet (code-first) framework parser: operation detection (base type +
name convention), operation-vs-data-type separation, kind mapping, builder-chain handling,
capture-gating, selection, and schema validity."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.csharp_aspnet.parser import AspNetCoreParser
from breezeai_cog.parsers.csharp_graphql.parser import CSharpGraphQLParser
from breezeai_cog.schemas import FileRecord

# Root query + a namespace query type (grounded on the real ProductCatalogue schema):
# grouping Field<T>("name") + leaf Field<T,U>("name") + builder Field<T>("name").Resolve(…)
# + async FieldAsync + expression-only Field(x => …) which has no string name.
QUERY = b'''
using GraphQL.Types;
namespace Api.GraphQL {
  public sealed class Query : ObjectGraphType {
    public Query() {
      Name = "Query";
      Field<ProductQuery, object>("products").Resolve(ctx => new { });
    }
  }

  public class ProductQuery : GraphQL.Types.ObjectGraphType {
    public ProductQuery() {
      Field<ProductType, ProductDTO>("productById").Resolve(ctx => svc.Get());
      FieldAsync<ListGraphType<ProductType>>("allProducts", resolve: async ctx => await svc.All());
      Field(x => x.ignored);
    }
  }
}
'''

# A mutation namespace (plural name) + a data/result type that must NOT yield operations.
MUTATION = b'''
using GraphQL.Types;
namespace Api.GraphQL {
  public class ProductMutations : ObjectGraphType {
    public ProductMutations() {
      Field<ProductType, ProductDTO>("createProduct").Resolve(ctx => svc.Create());
    }
  }

  public class ProductResultType : ObjectGraphType<Result<ProductDTO>> {
    public ProductResultType() {
      Field("success", r => r.IsSucceeded);
      Field<ProductType, ProductDTO>("product");
    }
  }

  public class ProductType : ObjectGraphType<ProductDTO> {
    public ProductType() { Field(x => x.Name); }
  }
}
'''


# Subscription type uses the AddField(new FieldType { Name = "…" }) idiom (grounded on the
# real ProductCatalogue Subscription.cs), not Field<T>("name").
SUBSCRIPTION = b'''
using GraphQL.Types;
namespace Api.GraphQL {
  public class Subscription : ObjectGraphType {
    public Subscription() {
      Name = "Subscription";
      AddField(new FieldType {
        Name = "claimLock",
        Type = typeof(LockoutType),
        Resolver = new FuncFieldResolver<LockoutDTO>(LockoutResolver),
      });
      AddField(new FieldType { Name = "watchLocks", Type = typeof(LockoutType) });
    }
  }
}
'''


def _parse(parser, src, name, *, capture=True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None, capture_statements=capture)
    return parser.parse_file(ctx)


def _routes(rec: FileRecord):
    return [s for s in rec.statements if s.semanticType == "route"]


def test_query_operations_detected() -> None:
    rec = _parse(CSharpGraphQLParser(), QUERY, "Query.cs")
    ops = {(s.method, s.endpoint) for s in _routes(rec)}
    assert ops == {("QUERY", "products"), ("QUERY", "productById"), ("QUERY", "allProducts")}
    assert rec.framework == "graphql"
    # every op parented to its operation-type class, kind + framework set
    cls_ids = {c.id for c in rec.classes}
    for s in _routes(rec):
        assert s.parentId in cls_ids and s.routeKind == "query" and s.framework == "graphql"


def test_expression_only_field_skipped() -> None:
    # Field(x => x.ignored) carries no string name — not fabricated (honest-null).
    rec = _parse(CSharpGraphQLParser(), QUERY, "Query.cs")
    assert "ignored" not in {s.endpoint for s in _routes(rec)}


def test_data_and_result_types_excluded() -> None:
    # Only the ProductMutations operation type yields a route; ObjectGraphType<T> wrappers
    # (ProductResultType, ProductType) are data, not operations.
    rec = _parse(CSharpGraphQLParser(), MUTATION, "Mutation.cs")
    ops = {(s.method, s.endpoint) for s in _routes(rec)}
    assert ops == {("MUTATION", "createProduct")}
    assert all(s.routeKind == "mutation" for s in _routes(rec))


def test_subscription_addfield_idiom() -> None:
    # AddField(new FieldType { Name = "…" }) — name from the object initializer, not a
    # positional arg. Grounded on the real Subscription.cs.
    rec = _parse(CSharpGraphQLParser(), SUBSCRIPTION, "Subscription.cs")
    ops = {(s.method, s.endpoint) for s in _routes(rec)}
    assert ops == {("SUBSCRIPTION", "claimLock"), ("SUBSCRIPTION", "watchLocks")}
    assert all(s.routeKind == "subscription" for s in _routes(rec))


def test_routes_require_capture() -> None:
    rec = _parse(CSharpGraphQLParser(), QUERY, "Query.cs", capture=False)
    assert _routes(rec) == [] and rec.framework is None


def test_non_graphql_csharp_not_claimed() -> None:
    # A plain ObjectGraphType-free C# file must not be claimed by the GraphQL parser.
    assert CSharpGraphQLParser().claims("Plain.cs", b"namespace X { class C {} }") is False


def test_output_validates() -> None:
    for src, name in [(QUERY, "Query.cs"), (MUTATION, "Mutation.cs")]:
        rec = _parse(CSharpGraphQLParser(), src, name)
        errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                      .iter_errors(json.loads(to_line(rec))))
        assert not errors, errors


def test_selection() -> None:
    registry.clear()
    for p in (CSharpParser(), AspNetCoreParser(), CSharpGraphQLParser()):
        registry.register(p)
    assert registry.select("Query.cs", QUERY).name == "csharp-graphql"       # graphql wins
    assert registry.select("plain.cs", b"class C {}").name == "csharp"         # base fallback
    registry.clear()
