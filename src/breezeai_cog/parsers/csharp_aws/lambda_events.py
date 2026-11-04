"""AWS Lambda entry-point detection for C# / .NET.

A .NET Lambda's entry point is the handler method
``public Task FunctionHandler(S3Event evt, ILambdaContext context)``, which otherwise lands as
an ordinary method. This detector surfaces it as an entry point whose ``framework`` is the
trigger's service (``aws-s3``, ``aws-sqs``, ``aws-apigw``, …; the file is labelled
``aws-lambda``), mirroring the TypeScript ``aws_events`` handler detection — including its
semanticType split: event-source triggers are ``eventbus_consumer``, HTTP-facing events
(APIGateway/ALB) are ``route``. ``endpoint`` stays empty: the queue, bucket or path is set in
deployment config, not in the handler (the method name is not an address).

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

from ...emit import file_id
from ...schemas import SemanticType
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..aws_deploy import Trigger, deployed_handlers
from ..emits import Emit
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
    "SimpleEmailEvent": "aws-ses",
}
# A scheduled event is triggered by a schedule (an EventBridge rule): a timer. Its schedule lives
# in deployment config, so the endpoint stays empty.
_TIMER_EVENT_TYPES = {"ScheduledEvent"}
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
        if t in _TIMER_EVENT_TYPES:
            return "timer", "aws-eventbridge", None
        if t in _EVENT_TYPES:
            return "eventbus_consumer", _EVENT_TYPES[t], None
        if t in _ROUTE_EVENT_TYPES:
            return "route", _ROUTE_EVENT_TYPES[t], "route"
    return None


def _type_name(method: Node, source: bytes) -> str | None:
    """``Namespace.Type`` of the type declaring ``method`` (nested: ``Namespace.Outer.Inner``) —
    the middle part of a .NET Lambda handler string ``Assembly::Namespace.Type::Method``."""
    names: list[str] = []
    n = method.parent
    while n is not None:
        if n.type in ("class_declaration", "struct_declaration", "record_declaration",
                      "namespace_declaration"):
            name = n.child_by_field_name("name")
            if name is None:
                return None
            names.append(node_text(name, source))
        n = n.parent
    root = method
    while root.parent is not None:
        root = root.parent
    scoped = next((c for c in root.named_children
                   if c.type == "file_scoped_namespace_declaration"), None)
    scoped_name = scoped.child_by_field_name("name") if scoped is not None else None
    if scoped_name is not None:
        names.append(node_text(scoped_name, source))
    return ".".join(reversed(names)) or None


def detect_lambda_handlers(
    root: Node, source: bytes, path: str, index: object = None,
) -> list[Emit]:
    """AWS Lambda handler entry points in a C# file. Additive — invoked for every C# file,
    self-guarded by a cheap byte check, layering on top of whatever framework already owns the
    file. Each handler is a convention over a method (parented to the file, like the ASP.NET
    route detectors).

    A handler the deployment config names (``Assembly::Namespace.Type::Method``, see
    :mod:`..aws_deploy`) takes its triggers from there: one record per trigger, with its address
    — whether or not its parameters say it is a Lambda."""
    fact = deployed_handlers(index)
    deployed = fact.types if fact is not None else {}
    if b"ILambdaContext" not in source and not deployed:
        return []
    found: list[Emit] = []
    for method in _all(root, "method_declaration"):
        name_node = method.child_by_field_name("name")
        name = node_text(name_node, source) if name_node is not None else None
        triggers: tuple[Trigger, ...] = ()
        if deployed and name is not None:
            type_name = _type_name(method, source)
            triggers = deployed.get(type_name, {}).get(name, ()) if type_name else ()
        info = _handler_kind(method, source) if b"ILambdaContext" in source else None
        if info is None and not triggers:
            continue
        sl = method.start_point[0] + 1
        base: dict[str, object] = {"handler": name, "handlerLine": sl}
        if info is not None:
            semantic, framework, route_kind = info
            base.update({"semanticType": semantic, "framework": framework,
                         "routeKind": route_kind,
                         "method": "PERIODIC" if semantic == "timer" else None})
        for fields in ([{**base, **t.fields()} for t in triggers] or [base]):
            found.append(Emit(
                method, fields, parent=file_id(path), node_type="synthetic",
                text=first_line(node_text(method, source))[:120],
            ))
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = detect_lambda_handlers(dc.root, dc.source, dc.path, dc.ctx.resolution_index)
    return Detection(found, label="aws-lambda") if found else None


# An entry-point emitter, so fixture/test files are skipped.
register_detector(Detector(
    name="lambda-handlers", gate=Gate("csharp", order=10, skip_fixtures=True), detect=_detect,
    frameworks=(
        "aws-lambda", "aws-s3", "aws-sqs", "aws-sns", "aws-eventbridge", "aws-dynamodb",
        "aws-kinesis", "aws-ses", "aws-apigw",
    ),
))
