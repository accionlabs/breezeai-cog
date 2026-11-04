"""GraphQL.Client operations in C#: one api_call per root field, in the TypeScript graphql-client
shape, gated on the receiver's declared client type; the document only from literals."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.schemas import FileRecord

CLIENT = b'''using GraphQL;
using GraphQL.Client.Http;
using GraphQL.Client.Abstractions;
public class ShopApi {
  private const string OrdersQuery = @"
    query Orders($input: OrderFilter!) {
      orders(filter: $input) { id }
      customer { name }
    }";
  private static readonly string Both = "query A { a } " + "mutation PlaceOrder { placeOrder { id } }";
  private readonly IGraphQLClient _client;

  public async Task Run(string dynamicQuery, GraphQLHttpClient http) {
    await _client.SendQueryAsync<OrdersResponse>(new GraphQLRequest { Query = OrdersQuery });
    var req = new GraphQLRequest(Both, null, "PlaceOrder");
    await this._client.SendMutationAsync<object>(req);
    await http.SendQueryAsync<object>(new GraphQLRequest { Query = dynamicQuery });
    await http.SendQueryAsync<object>(new GraphQLRequest { Query = Both });
    var sub = http.CreateSubscriptionStream<Msg>(new GraphQLRequest {
      Query = "subscription OnMessage { messageAdded { id } }" });
  }
}
'''


def _parse(src: bytes, name: str = "ShopApi.cs") -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None,
                       capture_statements=True)
    return CSharpParser().parse_file(ctx)


def _ops(rec: FileRecord) -> list[tuple]:
    return [(s.startLine, s.method, s.routeKind, s.endpoint, s.handler, s.requestDTO)
            for s in rec.statements if s.semanticType == "api_call" and s.framework == "graphql"]


def test_one_api_call_per_root_field() -> None:
    assert _ops(_parse(CLIENT)) == [
        (14, "QUERY", "query", "orders", "Orders", "OrderFilter!"),
        (16, "MUTATION", "mutation", "placeOrder", "PlaceOrder", None),
        (17, "QUERY", "query", None, None, None),        # the document is not a literal
        (18, "QUERY", "query", None, None, None),        # two operations, none named
        (19, "SUBSCRIPTION", "subscription", "messageAdded", "OnMessage", None),
        (14, "QUERY", "query", "customer", "Orders", "OrderFilter!"),  # second root field
    ]


def test_first_root_field_enriches_the_call_statement() -> None:
    rec = _parse(CLIENT)
    first = next(s for s in rec.statements if s.endpoint == "orders")
    second = next(s for s in rec.statements if s.endpoint == "customer")
    assert first.nodeType == "expression_statement"
    assert second.nodeType == "invocation_expression" and second.id != first.id


def test_untyped_receivers_are_not_graphql() -> None:
    src = b'''using GraphQL;
class Other {
  private readonly IApiClient _api;
  async Task Run() {
    await _api.SendQueryAsync(new GraphQLRequest { Query = "query { x }" });
    await client.SendMutationAsync(new GraphQLRequest { Query = "mutation { y }" });
  }
}
'''
    assert _ops(_parse(src)) == []


def test_fixture_files_are_skipped() -> None:
    assert _ops(_parse(CLIENT, "__mocks__/ShopApi.cs")) == []
