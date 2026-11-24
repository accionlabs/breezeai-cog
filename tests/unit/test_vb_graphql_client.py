"""GraphQL.Client operations in VB: the C# detector's rules on the VB tree — one api_call per root
field, gated on the receiver's declared client type; the document only from string literals."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.vb.parser import VbParser
from breezeai_cog.schemas import FileRecord

CLIENT = b'''Imports GraphQL
Imports GraphQL.Client.Http
Imports GraphQL.Client.Abstractions

Public Class ShopApi
    Private ReadOnly _client As IGraphQLClient

    Public Async Function Run(dynamicQuery As String, http As GraphQLHttpClient) As Task
        Await _client.SendQueryAsync(Of OrdersResponse)(New GraphQLRequest With {
            .Query = "query Orders($input: OrderFilter!) { orders(filter: $input) { id } " & "customer { name } }"})
        Dim req = New GraphQLRequest With {
            .Query = "query A { a } mutation PlaceOrder { placeOrder { id } }",
            .OperationName = "PlaceOrder"}
        Await Me._client.SendMutationAsync(Of Object)(req)
        Await http.SendQueryAsync(Of Object)(New GraphQLRequest With {.Query = dynamicQuery})
        Await http.SendQueryAsync(Of Object)(New GraphQLRequest With {.Query = "query A { a } query B { b }"})
        Dim positional = Await http.SendQueryAsync(Of Object)(New GraphQLRequest("query { stock { sku } }"))
        Dim subscription = http.CreateSubscriptionStream(Of Msg)(New GraphQLRequest With {
            .Query = "subscription OnMessage { messageAdded { id } }"})
        Await http.SendQueryAsync(Of Object)(New GraphQLRequest With {.Query = "query { a } " & _
            "b { c }"})
    End Function
End Class
'''


def _parse(src: bytes, name: str = "ShopApi.vb") -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None,
                       capture_statements=True)
    return VbParser().parse_file(ctx)


def _ops(rec: FileRecord) -> list[tuple]:
    return [(s.startLine, s.method, s.routeKind, s.endpoint, s.handler, s.requestDTO)
            for s in rec.statements if s.semanticType == "api_call" and s.framework == "graphql"]


def test_one_api_call_per_root_field() -> None:
    assert _ops(_parse(CLIENT)) == [
        (9, "QUERY", "query", "orders", "Orders", "OrderFilter!"),
        (14, "MUTATION", "mutation", "placeOrder", "PlaceOrder", None),  # a local's request
        (15, "QUERY", "query", None, None, None),        # the document is not a literal
        (16, "QUERY", "query", None, None, None),        # two operations, none named
        (17, "QUERY", "query", "stock", None, None),     # the constructor's query argument
        (18, "SUBSCRIPTION", "subscription", "messageAdded", "OnMessage", None),
        # a document continued onto the next line: the grammar can't read it, so no endpoint
        (20, "QUERY", "query", None, None, None),
        (9, "QUERY", "query", "customer", "Orders", "OrderFilter!"),  # second root field
    ]


def test_first_root_field_enriches_the_call_statement() -> None:
    rec = _parse(CLIENT)
    first = next(s for s in rec.statements if s.endpoint == "orders")
    second = next(s for s in rec.statements if s.endpoint == "customer")
    assert first.nodeType == "call_statement" and first.id == "ShopApi.vb:9:8"
    assert first.text.startswith("Await _client.SendQueryAsync(Of OrdersResponse)(")
    assert second.nodeType == "element_access" and second.id != first.id


def test_untyped_receivers_are_not_graphql() -> None:
    src = b'''Imports GraphQL
Class Other
    Private ReadOnly _api As IApiClient
    Async Function Run() As Task
        Await _api.SendQueryAsync(Of Object)(New GraphQLRequest With {.Query = "query { x }"})
        Await client.SendMutationAsync(Of Object)(New GraphQLRequest With {.Query = "mutation { y }"})
    End Function
End Class
'''
    assert _ops(_parse(src)) == []


def test_fixture_files_are_skipped() -> None:
    assert _ops(_parse(CLIENT, "__mocks__/ShopApi.vb")) == []


def test_a_client_name_is_bound_in_its_own_class() -> None:
    src = b'''Imports GraphQL
Imports GraphQL.Client.Http
Class Orders
    Private ReadOnly _gql As GraphQLHttpClient
    Async Function Run() As Task
        Await _gql.SendQueryAsync(Of Object)(New GraphQLRequest With {.Query = "query { orders { id } }"})
    End Function
End Class
Class Customers
    Private ReadOnly _gql As IMyGql
    Async Function Run() As Task
        Await _gql.SendQueryAsync(Of Object)(New GraphQLRequest With {.Query = "query { customers { id } }"})
    End Function
End Class
'''
    assert [(op[0], op[3]) for op in _ops(_parse(src))] == [(6, "orders")]
