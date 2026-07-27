"""AWS messaging / Lambda event detection for TypeScript/JavaScript (the
"event/messaging" family, extended beyond Vert.x to the AWS SDK).

AWS SNS/SQS/EventBridge follow the same producer→address→consumer shape as the Vert.x
event bus, so they reuse the existing ``eventbus_*`` semantic types (no schema change) —
the concrete transport lives in ``framework`` (``aws-sns``/``aws-sqs``/``aws-eventbridge``/
``aws-lambda``). This detector is **additive**: the registry picks one parser per file, so
rather than a peer framework parser (which would displace NestJS/Express on a shared file)
this runs inside ``TypeScriptParser.extract`` and layers on top of whatever else the file
got. Producers are call-based (mirrors ``java_vertx.events``); consumers are Lambda handler
functions identified by their ``aws-lambda`` handler *type annotation*.

Producers (SDK v3 command objects and v2 methods):
  ``client.send(new PublishCommand({TopicArn}))``     → eventbus_publish  (aws-sns)
  ``client.send(new SendMessageCommand({QueueUrl}))`` → eventbus_send     (aws-sqs)
  ``client.send(new PutEventsCommand({...}))``        → eventbus_publish  (aws-eventbridge)
  ``sqs.sendMessage({QueueUrl})`` / ``sendMessageBatch`` → eventbus_send  (aws-sqs)
  ``sns.publish({TopicArn})`` (guarded)               → eventbus_publish  (aws-sns)

Consumers / entry points (``aws-lambda`` handler types on an exported const):
  ``const h: SQSHandler = …``   → eventbus_consumer (aws-sqs)
  ``const h: SNSHandler = …``   → eventbus_consumer (aws-sns)
  ``const h: APIGatewayProxyHandlerV2 = …`` → route (aws-apigw) — an HTTP entry, *not* an event.

**Endpoint honesty:** the address (TopicArn/QueueUrl) is usually an injected config
symbol (``this.topicArn``), and the queue→Lambda binding lives in infra (Terraform/CDK),
not the code — so ``endpoint`` is set only from a *string literal*, else left ``None``. A
symbol is never passed off as a resolved address (the producer→consumer edge is a backend
enrichment concern, not this parser's). Mutates ``record``; returns a file framework label."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Function, SemanticType, Statement
from ..treesitter import first_line, node_text

# SDK v3 ``new XxxCommand({...})`` → (semanticType, framework).
_V3_COMMANDS: dict[str, tuple[SemanticType, str]] = {
    "PublishCommand": ("eventbus_publish", "aws-sns"),
    "PublishBatchCommand": ("eventbus_publish", "aws-sns"),
    "SendMessageCommand": ("eventbus_send", "aws-sqs"),
    "SendMessageBatchCommand": ("eventbus_send", "aws-sqs"),
    "PutEventsCommand": ("eventbus_publish", "aws-eventbridge"),
}
# SDK v2 ``client.<method>({...})`` → (semanticType, framework, needs_hint). ``publish`` is
# a generic name (GraphQL PubSub, event emitters) so it only counts with an SNS receiver
# hint or a ``TopicArn`` argument; the SQS/EventBridge methods are distinctive on their own.
_V2_METHODS: dict[str, tuple[SemanticType, str, bool]] = {
    "sendMessage": ("eventbus_send", "aws-sqs", False),
    "sendMessageBatch": ("eventbus_send", "aws-sqs", False),
    "putEvents": ("eventbus_publish", "aws-eventbridge", False),
    "publish": ("eventbus_publish", "aws-sns", True),
}
# ``aws-lambda`` handler type → (semanticType, framework). Event-source triggers.
_CONSUMER_HANDLERS = {
    "SQSHandler": "aws-sqs",
    "SNSHandler": "aws-sns",
    "EventBridgeHandler": "aws-eventbridge",
    "ScheduledHandler": "aws-eventbridge",
    "DynamoDBStreamHandler": "aws-dynamodb",
    "KinesisStreamHandler": "aws-kinesis",
    "S3Handler": "aws-s3",
    "SESHandler": "aws-ses",
    "CloudFrontRequestHandler": "aws-cloudfront",
    "CloudFrontResponseHandler": "aws-cloudfront",
}
# HTTP-facing Lambda handler types → route (not an event consumer).
_ROUTE_HANDLERS = {"APIGatewayProxyHandler", "APIGatewayProxyHandlerV2", "ALBHandler"}
# Keys in an SDK argument object that name the destination address.
_ADDRESS_KEYS = {"TopicArn", "TargetArn", "QueueUrl", "EventBusName"}

# AWS Lambda *event* parameter types → framework. Used to recognise an UNTYPED handler
# (``export const handler = async (event: S3Event) => …`` / ``exports.handler = …``) — the
# dominant real-world shape, which carries no ``: SQSHandler`` annotation for the maps above.
# Detection keys on the FIRST parameter's type; ``APIGateway*``/``ALB*`` events are HTTP
# (→ route), the rest are event-source consumers.
_EVENT_PARAM_TYPES = {
    "S3Event": "aws-s3",
    "SQSEvent": "aws-sqs",
    "SNSEvent": "aws-sns",
    "DynamoDBStreamEvent": "aws-dynamodb",
    "KinesisStreamEvent": "aws-kinesis",
    "EventBridgeEvent": "aws-eventbridge",
    "ScheduledEvent": "aws-eventbridge",
    "SESEvent": "aws-ses",
    "CloudFrontRequestEvent": "aws-cloudfront",
    "CloudFrontResponseEvent": "aws-cloudfront",
}
_ROUTE_PARAM_TYPES = {
    "APIGatewayProxyEvent": "aws-apigw",
    "APIGatewayProxyEventV2": "aws-apigw",
    "ALBEvent": "aws-apigw",
}
# Exported names that mark a Lambda entry point (``export const handler`` / ``exports.handler``).
_HANDLER_NAMES = {"handler", "lambdaHandler", "main"}


def _has_aws(source: bytes) -> bool:
    """Cheap byte guard so the walk only runs on files touching the AWS SDK / Lambda."""
    return b"aws-sdk" in source or b"aws-lambda" in source


def _walk(root: Node, types: frozenset[str]) -> list[Node]:
    out: list[Node] = []

    def go(n: Node) -> None:
        if n.type in types:
            out.append(n)
        for c in n.named_children:
            go(c)

    go(root)
    return out


def _address(args: Node | None, source: bytes) -> tuple[str | None, bool]:
    """(literal endpoint, has-address-key) from an SDK call's first object argument.
    Endpoint is set only from a plain string literal — a symbol (``this.topicArn``) stays
    ``None`` (honest); ``has_key`` reports whether an address key was present at all."""
    if args is None:
        return None, False
    for a in args.named_children:
        if a.type != "object":
            continue
        for pair in a.named_children:
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            if key is None or node_text(key, source) not in _ADDRESS_KEYS:
                continue
            value = pair.child_by_field_name("value")
            if value is not None and value.type == "string":
                frag = next((c for c in value.named_children if c.type == "string_fragment"), None)
                return (node_text(frag, source) if frag is not None else ""), True
            return None, True  # address present but an unresolved symbol
    return None, False


def _producer(call: Node, source: bytes) -> tuple[SemanticType, str, str | None, str] | None:
    """→ (semanticType, method, endpoint, framework) for an AWS producer call, else None."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_expression":
        return None
    prop = fn.child_by_field_name("property")
    method = node_text(prop, source) if prop is not None else ""
    obj = fn.child_by_field_name("object")
    receiver = node_text(obj, source).lower() if obj is not None else ""
    args = call.child_by_field_name("arguments")
    first = args.named_children[0] if (args is not None and args.named_children) else None

    if method == "send" and first is not None and first.type == "new_expression":
        ctor = first.child_by_field_name("constructor")
        cname = node_text(ctor, source) if ctor is not None else ""
        info = _V3_COMMANDS.get(cname)
        if info is None:
            return None
        sem, fw = info
        endpoint, _ = _address(first.child_by_field_name("arguments"), source)
        return sem, cname, endpoint, fw

    info2 = _V2_METHODS.get(method)
    if info2 is not None:
        sem, fw, needs_hint = info2
        endpoint, has_key = _address(args, source)
        if needs_hint and not has_key and "sns" not in receiver:
            return None
        return sem, method, endpoint, fw
    return None


def _type_name(annotation: Node, source: bytes) -> str | None:
    """Base type name of a ``: Type`` / ``: Type<...>`` annotation (generics stripped)."""
    inner = annotation.named_children[0] if annotation.named_children else None
    if inner is None:
        return None
    if inner.type == "generic_type":
        name = inner.child_by_field_name("name") or (
            inner.named_children[0] if inner.named_children else None
        )
        return node_text(name, source) if name is not None else None
    return node_text(inner, source)


def _handler(
    decl: Node, source: bytes
) -> tuple[SemanticType, str, str | None, str | None] | None:
    """→ (semanticType, framework, routeKind, handlerName) for a Lambda handler const."""
    annotation = next((c for c in decl.named_children if c.type == "type_annotation"), None)
    if annotation is None:
        return None
    tname = _type_name(annotation, source)
    if tname is None:
        return None
    name_node = decl.child_by_field_name("name")
    hname = node_text(name_node, source) if name_node is not None else None
    if tname in _CONSUMER_HANDLERS:
        return "eventbus_consumer", _CONSUMER_HANDLERS[tname], None, hname
    if tname in _ROUTE_HANDLERS:
        return "route", "aws-apigw", "route", hname
    return None


def _first_param_type(fn: Node, source: bytes) -> str | None:
    """Base type name of an arrow/function's FIRST parameter (``(event: S3Event)`` → ``S3Event``),
    generics stripped (``EventBridgeEvent<'x'>`` → ``EventBridgeEvent``). None if untyped."""
    params = fn.child_by_field_name("parameters")
    if params is None:
        return None
    first = next((c for c in params.named_children
                  if c.type in ("required_parameter", "optional_parameter")), None)
    if first is None:
        return None
    annotation = next((c for c in first.named_children if c.type == "type_annotation"), None)
    return _type_name(annotation, source) if annotation is not None else None


def _fn_value(node: Node) -> Node | None:
    """The arrow/function assigned in a ``variable_declarator`` or ``assignment_expression``."""
    val = node.child_by_field_name("value") if node.type == "variable_declarator" else (
        node.child_by_field_name("right") if node.type == "assignment_expression" else None
    )
    return val if val is not None and val.type in ("arrow_function", "function_expression") else None


def _untyped_handler(node: Node, source: bytes) -> tuple[SemanticType, str, str | None, str | None] | None:
    """A Lambda entry point declared WITHOUT a handler type annotation — recognised by an
    exported ``handler`` name whose function's first parameter is an AWS event type.
    → (semanticType, framework, routeKind, handlerName), else None.

    Precision gate: BOTH the handler name (``handler``/``exports.handler``) AND an AWS event
    parameter type must be present, so an ordinary ``export const handler = (x) => …`` (no AWS
    event) never matches."""
    if node.type == "variable_declarator":
        name_node = node.child_by_field_name("name")
        hname = node_text(name_node, source) if name_node is not None else None
    elif node.type == "assignment_expression":  # exports.handler = / module.exports.handler =
        left = node.child_by_field_name("left")
        prop = left.child_by_field_name("property") if left is not None and left.type == "member_expression" else None
        hname = node_text(prop, source) if prop is not None else None
    else:
        return None
    if hname not in _HANDLER_NAMES:
        return None
    fn = _fn_value(node)
    if fn is None:
        return None
    ptype = _first_param_type(fn, source)
    if ptype in _EVENT_PARAM_TYPES:
        return "eventbus_consumer", _EVENT_PARAM_TYPES[ptype], None, hname
    if ptype in _ROUTE_PARAM_TYPES:
        return "route", _ROUTE_PARAM_TYPES[ptype], "route", hname
    return None


def _enclosing_statement(line: int, statements: list[Statement]) -> Statement | None:
    best: Statement | None = None
    best_span: int | None = None
    for s in statements:
        if s.startLine <= line <= s.endLine:
            span = s.endLine - s.startLine
            if best_span is None or span < best_span:
                best, best_span = s, span
    return best


def _owner_function(line: int, functions: list[Function], fallback: str) -> str:
    best = None
    best_span: int | None = None
    for f in functions:
        if f.startLine <= line <= f.endLine:
            span = f.endLine - f.startLine
            if best_span is None or span < best_span:
                best, best_span = f, span
    return best.id if best is not None else fallback


def detect_aws_events(root: Node, source: bytes, path: str, record: FileRecord) -> str | None:
    """Enrich/add AWS event statements on ``record``. Returns a file-level framework label
    (``aws-lambda`` for a handler file, else the first producer transport) or ``None``."""
    if not _has_aws(source):
        return None
    fid = file_id(path)
    seen = {s.id for s in record.statements}
    file_fw: str | None = None
    is_lambda = False

    for call in _walk(root, frozenset({"call_expression"})):
        info = _producer(call, source)
        if info is None:
            continue
        sem, method, endpoint, fw = info
        line = call.start_point[0] + 1
        stmt = _enclosing_statement(line, record.statements)
        if stmt is not None and stmt.semanticType is None:  # enrich the base statement in place
            stmt.semanticType = sem
            stmt.framework = fw
            stmt.method = method
            if endpoint:
                stmt.endpoint = endpoint
        else:  # inside a lambda the base skipped, or the span is already classified → add
            new_id = disambiguate(statement_id(path, line, call.start_point[1]), seen)
            seen.add(new_id)
            record.statements.append(Statement(
                id=new_id, parentId=_owner_function(line, record.functions, fid),
                nodeType=call.type, semanticType=sem, text=first_line(node_text(call, source)),
                method=method, endpoint=endpoint, framework=fw,
                startLine=line, endLine=call.end_point[0] + 1, path=path,
            ))
        file_fw = file_fw or fw

    # Handler entry points: a typed const (``const h: SQSHandler = …``), else an untyped but
    # AWS-event-parameterised ``handler`` export (``export const handler = (e: S3Event) => …`` /
    # ``exports.handler = …``). One statement per handler; the typed form wins if both apply.
    for decl in _walk(root, frozenset({"variable_declarator", "assignment_expression"})):
        hinfo = _handler(decl, source) if decl.type == "variable_declarator" else None
        if hinfo is None:
            hinfo = _untyped_handler(decl, source)
        if hinfo is None:
            continue
        sem, fw, route_kind, hname = hinfo
        line = decl.start_point[0] + 1
        new_id = disambiguate(statement_id(path, line, decl.start_point[1]), seen)
        seen.add(new_id)
        record.statements.append(Statement(
            id=new_id, parentId=_owner_function(line, record.functions, fid),
            nodeType=decl.type, semanticType=sem, text=first_line(node_text(decl, source)),
            framework=fw, handler=hname, routeKind=route_kind,
            startLine=line, endLine=decl.end_point[0] + 1, path=path,
        ))
        is_lambda = True

    return "aws-lambda" if is_lambda else file_fw
