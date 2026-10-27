"""AWS Lambda handlers in Go: ``lambda.Start(h)`` is the binding, and the trigger comes from the
``events.*`` type of ``h``'s event parameter, with the same records as the C# and TypeScript
Lambda detectors::

    func handler(ctx context.Context, e events.SQSEvent) error { … }
    func main() { lambda.Start(handler) }
        → eventbus_consumer, framework aws-sqs, handler handler

    func(req events.APIGatewayProxyRequest) (events.APIGatewayProxyResponse, error)
        → route, routeKind route, framework aws-apigw (verb and path live in deployment config)

    any other parameter (an authorizer request, a custom struct, none)
        → eventbus_consumer, framework aws-lambda (trigger unknown from the code)

``endpoint`` stays empty: the queue, topic or path is set in deployment config, and a Go
function's config names a compiled binary (``handler: bootstrap``), not the function, so it
can't be linked by name."""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..go.imports import GoIndex
from ..go.routing import _locate, file_analyzer, types_by_function
from ..treesitter import node_text

_LAMBDA = "github.com/aws/aws-lambda-go/lambda"
_EVENTS = "github.com/aws/aws-lambda-go/events"
_STARTS = frozenset({"Start", "StartWithOptions", "StartHandler", "StartHandlerFunc",
                     "StartWithContext"})

#: events type → framework of an event-source trigger (eventbus_consumer).
_CONSUMERS = {
    "SQSEvent": "aws-sqs", "SNSEvent": "aws-sns", "S3Event": "aws-s3",
    "DynamoDBEvent": "aws-dynamodb", "KinesisEvent": "aws-kinesis",
    "KinesisFirehoseEvent": "aws-kinesis", "CloudWatchEvent": "aws-eventbridge",
    "EventBridgeEvent": "aws-eventbridge", "SimpleEmailEvent": "aws-ses",
}
#: events type → framework of an HTTP trigger (route).
_ROUTES = {
    "APIGatewayProxyRequest": "aws-apigw", "APIGatewayV2HTTPRequest": "aws-apigw",
    "ALBTargetGroupRequest": "aws-apigw", "LambdaFunctionURLRequest": "aws-apigw",
}


def _alias_of(aliases: dict[str, str], import_path: str) -> str | None:
    return next((a for a, ip in aliases.items() if ip == import_path), None)


def _functions(root: Node, source: bytes) -> dict[str, Node]:
    return {node_text(c.child_by_field_name("name"), source): c for c in root.named_children
            if c.type == "function_declaration" and c.child_by_field_name("name") is not None}


def _event_type(fn: Node | None, source: bytes, events: str | None) -> str | None:
    """The ``events.X`` type of the handler's event parameter (its last), pointer dropped."""
    params = fn.child_by_field_name("parameters") if fn is not None else None
    if params is None or events is None:
        return None
    decls = [p for p in params.named_children if p.type == "parameter_declaration"]
    tnode = decls[-1].child_by_field_name("type") if decls else None
    alias, dot, name = (node_text(tnode, source).lstrip("*") if tnode is not None else "").partition(".")
    return name.split("[", 1)[0] if dot and alias == events else None


def _handler(arg: Node | None, functions: dict[str, Node], source: bytes) -> Node | None:
    """The handler node: as written, or the innermost argument of a wrapper when that names a
    function of this file (``lambda.Start(otel.Wrap(handler))``). ``newHandler(cfg)`` builds the
    handler at run time: no handler node."""
    node = arg
    while node is not None and node.type == "call_expression":
        args = node.child_by_field_name("arguments")
        inner = args.named_children[-1] if args is not None and args.named_children else None
        if inner is None or inner.type != "identifier" or node_text(inner, source) not in functions:
            return None
        node = inner
    return node


def detect_lambda(dc: DetectContext) -> Detection | None:
    index = dc.ctx.resolution_index if isinstance(dc.ctx.resolution_index, GoIndex) else None
    a = file_analyzer(dc.root, dc.source, dc.path, index)
    lam, events = _alias_of(a.aliases, _LAMBDA), _alias_of(a.aliases, _EVENTS)
    if lam is None:
        return None
    functions = _functions(dc.root, dc.source)
    types = types_by_function(a)
    found: list[Emit] = []
    stack = [dc.root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "selector_expression" or \
                node_text(fn.child_by_field_name("operand"), dc.source) != lam or \
                node_text(fn.child_by_field_name("field"), dc.source) not in _STARTS:
            continue
        args = node.child_by_field_name("arguments")
        handler = _handler(args.named_children[0] if args is not None and args.named_children else None,
                           functions, dc.source)
        decl = None
        if handler is not None and handler.type == "func_literal":
            decl = handler
        elif handler is not None and handler.type == "identifier":
            decl = functions.get(node_text(handler, dc.source))
        event = _event_type(decl, dc.source, events)
        if event in _ROUTES:
            fields = {"semanticType": "route", "routeKind": "route", "framework": _ROUTES[event]}
        else:
            fields = {"semanticType": "eventbus_consumer", "framework": _CONSUMERS.get(event or "", "aws-lambda")}
        fill = {}
        if handler is not None:
            name, line, path = _locate(a, handler, types(node))
            fill = {"handler": name, "handlerLine": line, "handlerPath": path}
        found.append(Emit(node, fields, fill=fill, enrich="any"))
    return Detection(found, label="aws-lambda") if found else None


register_detector(Detector(
    name="go-lambda", gate=Gate("go", order=120, guard=b"aws-lambda-go/lambda", skip_fixtures=True),
    detect=detect_lambda, frameworks=("aws-lambda", "aws-apigw", "aws-sqs", "aws-sns", "aws-s3",
                                      "aws-dynamodb", "aws-kinesis", "aws-eventbridge", "aws-ses"),
))
