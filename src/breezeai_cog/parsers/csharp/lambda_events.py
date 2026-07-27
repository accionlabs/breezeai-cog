"""AWS Lambda entry-point detection for C# / .NET.

A .NET Lambda's entry point is the handler method
``public Task FunctionHandler(S3Event evt, ILambdaContext context)``, which otherwise lands as
an ordinary method. This detector surfaces it as an entry point on ``framework="aws-lambda"``,
mirroring the TypeScript ``aws_events`` handler detection — including its semanticType split:
event-source triggers are ``eventbus_consumer``, HTTP-facing events (APIGateway/ALB) are
``route``.

**Additive** (like ``detect_express``/``detect_aws_events``): a Lambda handler is an orthogonal
capability, not an exclusive framework identity — the same file can be an ASP.NET controller AND
a Lambda entry point (``Amazon.Lambda.AspNetCoreServer``), or carry EF/GraphQL. So this runs
inside ``CSharpParser.extract`` on top of whatever else the file is, rather than as a peer
claiming parser that would displace the aspnet/wcf detector on a shared file.

Precision gate: a method qualifies only when its parameter list carries BOTH ``ILambdaContext``
AND an AWS *event* type (``S3Event``/``SQSEvent``/…). Requiring both rejects the common helper
methods that merely take ``ILambdaContext`` as a passed-through argument (``ImportData``,
``ReportResult``, …) — the dominant false-positive shape. ``APIGatewayProxyRequest``/
``ApplicationLoadBalancerRequest`` events are HTTP entries (``route``); the rest are
event-source consumers (``eventbus_consumer``)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, SemanticType, Statement
from ..treesitter import first_line, node_text

# The .NET context parameter every real handler carries — the primary marker.
_CONTEXT_TYPE = "ILambdaContext"

# AWS event parameter type (base name) → framework. Event-source triggers → consumer entries.
_EVENT_TYPES: dict[str, str] = {
    "S3Event": "aws-s3",
    "SQSEvent": "aws-sqs",
    "SNSEvent": "aws-sns",
    "DynamoDBEvent": "aws-dynamodb",
    "KinesisEvent": "aws-kinesis",
    "CloudWatchEvent": "aws-eventbridge",
    "ScheduledEvent": "aws-eventbridge",
    "SimpleEmailEvent": "aws-ses",
}
# HTTP-facing event types → route (not an event consumer).
_ROUTE_EVENT_TYPES: dict[str, str] = {
    "APIGatewayProxyRequest": "aws-apigw",
    "APIGatewayHttpApiV2ProxyRequest": "aws-apigw",
    "ApplicationLoadBalancerRequest": "aws-apigw",
}


def _all(root: Node, typ: str) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == typ:
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _base_type(param: Node, source: bytes) -> str | None:
    """The parameter's declared type as a base name, generics stripped
    (``EventBridgeEvent<X>`` → ``EventBridgeEvent``, ``Amazon.Lambda...S3Event`` → ``S3Event``)."""
    tnode = param.child_by_field_name("type")
    if tnode is None:
        return None
    text = node_text(tnode, source)
    text = text.split("<", 1)[0]           # drop generic args
    return text.rsplit(".", 1)[-1].strip()  # drop namespace qualifier


def _handler_kind(method: Node, source: bytes) -> tuple[SemanticType, str, str | None] | None:
    """→ (semanticType, framework, routeKind) if this method is a Lambda handler — its parameter
    list has ILambdaContext AND a recognised AWS event type — else None. Matches the TS
    ``aws_events`` modelling of the same concept: event-source triggers are
    ``eventbus_consumer`` (routeKind ``None``); HTTP-facing events (APIGateway/ALB) are
    ``route`` (routeKind ``"route"``)."""
    params = method.child_by_field_name("parameters")
    if params is None:
        return None
    types = [t for p in params.named_children if p.type == "parameter"
             and (t := _base_type(p, source)) is not None]
    if _CONTEXT_TYPE not in types:
        return None
    for t in types:
        if t in _EVENT_TYPES:
            return "eventbus_consumer", _EVENT_TYPES[t], None
        if t in _ROUTE_EVENT_TYPES:
            return "route", _ROUTE_EVENT_TYPES[t], "route"
    return None


def detect_lambda_handlers(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Add AWS Lambda handler entry-point statements to ``record``; return True if any matched.
    Additive — invoked from ``CSharpParser.extract`` for every C# file, self-guarded by a cheap
    byte check, layering on top of whatever framework already owns the file. Each handler is a
    convention over a method (parented to the file, like the ASP.NET route detectors)."""
    if b"ILambdaContext" not in source:
        return False
    seen = {s.id for s in record.statements}
    found = False
    for method in _all(root, "method_declaration"):
        info = _handler_kind(method, source)
        if info is None:
            continue
        semantic, framework, route_kind = info
        name_node = method.child_by_field_name("name")
        name = node_text(name_node, source) if name_node is not None else None
        sl = method.start_point[0] + 1
        new_id = disambiguate(statement_id(path, sl, method.start_point[1]), seen)
        seen.add(new_id)
        record.statements.append(Statement(
            id=new_id,
            parentId=file_id(path),
            nodeType="synthetic",
            semanticType=semantic,
            text=first_line(node_text(method, source))[:120],
            endpoint=name,
            framework="aws-lambda",
            handler=name,
            handlerLine=sl,
            routeKind=route_kind,
            startLine=sl,
            endLine=method.end_point[0] + 1,
            path=path,
        ))
        found = True
    return found
