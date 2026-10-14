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
  ``client.send(new PublishCommand({PhoneNumber}))``  → eventbus_send     (aws-sns, SMS)
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
not the code — so ``endpoint`` is set only from a *string literal*, else left ``None``. A literal
queue URL / topic ARN / bus ARN is written by name (:func:`address_name`), the form a consumer
records, so the two meet on one address. A
symbol is never passed off as a resolved address (the producer→consumer edge is a backend
enrichment concern, not this parser's). Mutates ``record``; returns a file framework label."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import SemanticType
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..aws_deploy import deployed_handlers
from ..emits import Emit
from ..treesitter import node_text

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
    "DynamoDBStreamHandler": "aws-dynamodb",
    "KinesisStreamHandler": "aws-kinesis",
    "S3Handler": "aws-s3",
    "SESHandler": "aws-ses",
    "CloudFrontRequestHandler": "aws-cloudfront",
    "CloudFrontResponseHandler": "aws-cloudfront",
}
# HTTP-facing Lambda handler types → route (not an event consumer).
_ROUTE_HANDLERS = {"APIGatewayProxyHandler", "APIGatewayProxyHandlerV2", "ALBHandler"}
# A scheduled-event handler is triggered by a schedule (an EventBridge rule): a timer. Its
# schedule lives in deployment config, so the endpoint stays empty.
_TIMER_HANDLERS = {"ScheduledHandler"}
_TIMER_PARAM_TYPES = {"ScheduledEvent"}
# Keys in an SDK argument object that name the destination address.
_ADDRESS_KEYS = {"TopicArn", "TargetArn", "QueueUrl", "EventBusName", "PhoneNumber"}
# Subset of the above that also *proves* the call is AWS — needed by the ``publish`` guard
# below. ``PhoneNumber`` is excluded: every SMS vendor (Twilio, Vonage, MessageBird) names
# that field the same way, so it identifies a destination but not a provider. The remaining
# keys are AWS-specific strings, so for them naming a destination and proving AWS coincide.
_AWS_EVIDENCE_KEYS = _ADDRESS_KEYS - {"PhoneNumber"}
# Address keys that reach exactly one recipient. SNS names its API ``Publish`` for every
# destination, but only a *topic* fans out, so an SMS is point-to-point — the distinction
# Vert.x draws between ``publish()`` and ``send()``. Nothing can subscribe to a phone
# number, so calling it a publish would assert a one-to-many relationship that cannot exist.
#
# ``TargetArn`` is deliberately absent: the Publish API accepts *either* a platform-endpoint
# ARN (one device) *or* a topic ARN (fan-out) there, and the value is usually an injected
# symbol, so its cardinality is not knowable from the call site. It keeps the fan-out
# default rather than being guessed — a coarse classification beats a wrong one.
_DIRECT_ADDRESS_KEYS = {"PhoneNumber"}

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
# Names accepted for the UNTYPED CommonJS fallback (``exports.handler = () => …``). Narrower
# than ``_HANDLER_NAMES``: ``main`` is dropped — without a typed event it is too generic (a CLI
# entry, not necessarily a Lambda) to claim on the CJS form alone.
_CJS_HANDLER_NAMES = {"handler", "lambdaHandler"}
# CommonJS export receivers that make ``<recv>.handler = …`` a module entry point (not an
# arbitrary ``someObj.handler = fn`` assignment).
_CJS_EXPORT_RECEIVERS = {"exports", "module.exports"}


def _has_aws(source: bytes) -> bool:
    """Cheap byte guard so the walk only runs on files touching the AWS SDK / Lambda, OR
    declaring a CommonJS ``exports.handler`` / ``module.exports.handler`` — a Lambda entry-point
    idiom that need not import the SDK (the deployment config wires it), which the untyped-CJS
    handler path (:func:`_untyped_cjs_handler`) relies on. The substring covers both receivers;
    the precise structural check lives in that function, so this only decides whether to walk."""
    return b"aws-sdk" in source or b"aws-lambda" in source or b"exports.handler" in source


def _walk(root: Node, types: frozenset[str]) -> list[Node]:
    out: list[Node] = []

    def go(n: Node) -> None:
        if n.type in types:
            out.append(n)
        for c in n.named_children:
            go(c)

    go(root)
    return out


#: A literal SQS queue URL — ``https://sqs.<region>.amazonaws.com/<account>/<queue>`` or the legacy
#: ``https://<region>.queue.amazonaws.com/<account>/<queue>``.
_QUEUE_URL = re.compile(
    r"^https?://(?:sqs\.[\w-]+|[\w-]+\.queue)\.amazonaws\.com(?:\.cn)?/\d+/(?P<name>[\w.-]+)/?$"
)
#: A literal SNS topic / EventBridge bus ARN.
_TOPIC_ARN = re.compile(r"^arn:aws[\w-]*:sns:[\w-]+:\d+:(?P<name>[\w.-]+)$")
_BUS_ARN = re.compile(r"^arn:aws[\w-]*:events:[\w-]+:\d+:event-bus/(?P<name>[\w.-]+)$")
_NAME_FORMS = {"QueueUrl": _QUEUE_URL, "TopicArn": _TOPIC_ARN, "EventBusName": _BUS_ARN}


def address_name(key: str, value: str) -> str:
    """The name a literal address stands for, written as its consumer writes it: a queue URL →
    the queue (``…amazonaws.com/123/orders`` → ``orders``), a topic or bus ARN → the topic /
    bus. Anything not exactly in that form is kept as written."""
    form = _NAME_FORMS.get(key)
    m = form.match(value) if form is not None else None
    return m["name"] if m is not None else value


def _address(args: Node | None, source: bytes) -> tuple[str | None, str | None]:
    """(literal endpoint, matched address key) from an SDK call's first object argument.
    Endpoint is set only from a plain string literal — a symbol (``this.topicArn``) stays
    ``None`` (honest); the second element is the address key that matched (e.g.
    ``"PhoneNumber"``), else ``None`` when no address key is present.

    Only ``pair`` nodes are inspected, so a shorthand property (``{ PhoneNumber, Message }``)
    is not seen and yields ``(None, None)`` — conservative, and the same answer the caller
    would reach for an unresolved symbol."""
    if args is None:
        return None, None
    for a in args.named_children:
        if a.type != "object":
            continue
        for pair in a.named_children:
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            if key is None or node_text(key, source) not in _ADDRESS_KEYS:
                continue
            matched_key = node_text(key, source)
            value = pair.child_by_field_name("value")
            if value is not None and value.type == "string":
                frag = next((c for c in value.named_children if c.type == "string_fragment"), None)
                text = node_text(frag, source) if frag is not None else ""
                return address_name(matched_key, text), matched_key
            return None, matched_key  # address present but an unresolved symbol
    return None, None


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
        endpoint, key = _address(first.child_by_field_name("arguments"), source)
        # framework stays aws-sns either way — the transport is unchanged, and an SMS send
        # must remain visible to "what talks to SNS?"; only the delivery shape differs.
        if cname == "PublishCommand" and key in _DIRECT_ADDRESS_KEYS:
            sem = "eventbus_send"
        return sem, cname, endpoint, fw

    info2 = _V2_METHODS.get(method)
    if info2 is not None:
        sem, fw, needs_hint = info2
        endpoint, key = _address(args, source)
        if needs_hint and key not in _AWS_EVIDENCE_KEYS and "sns" not in receiver:
            return None
        if method == "publish" and key in _DIRECT_ADDRESS_KEYS:
            sem = "eventbus_send"
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
    if tname in _TIMER_HANDLERS:
        return "timer", "aws-eventbridge", None, hname
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
    if ptype in _TIMER_PARAM_TYPES:
        return "timer", "aws-eventbridge", None, hname
    if ptype in _EVENT_PARAM_TYPES:
        return "eventbus_consumer", _EVENT_PARAM_TYPES[ptype], None, hname
    if ptype in _ROUTE_PARAM_TYPES:
        return "route", _ROUTE_PARAM_TYPES[ptype], "route", hname
    return None


def _untyped_cjs_handler(
    node: Node, source: bytes
) -> tuple[SemanticType, str, str | None, str | None] | None:
    """A Lambda entry point in the CommonJS ``exports.handler = () => …`` /
    ``module.exports.handler = …`` form that carries NO typed AWS event to key on (the nullary /
    untyped shape — a real Lambda idiom that the typed-parameter path above cannot see).

    Precision: recognised ONLY for a ``exports``/``module.exports`` receiver (not an arbitrary
    ``obj.handler = fn``) and the narrow ``handler``/``lambdaHandler`` names, in a file that
    already imports the AWS SDK / Lambda types (:func:`_has_aws`). An ESM ``export const handler``
    is deliberately NOT accepted here — that shape is common in non-Lambda code, so it stays
    gated on the event type. The trigger is unknowable without the event type, so the framework
    is the generic ``aws-lambda`` (honest-null on the specific transport)."""
    if node.type != "assignment_expression":
        return None
    left = node.child_by_field_name("left")
    if left is None or left.type != "member_expression":
        return None
    obj = left.child_by_field_name("object")
    prop = left.child_by_field_name("property")
    if obj is None or node_text(obj, source) not in _CJS_EXPORT_RECEIVERS:
        return None
    hname = node_text(prop, source) if prop is not None else None
    if hname not in _CJS_HANDLER_NAMES:
        return None
    if _fn_value(node) is None:  # must assign an arrow/function, not a re-export of a value
        return None
    return "eventbus_consumer", "aws-lambda", None, hname


def detect_aws_events(
    root: Node, source: bytes, path: str, *, is_fixture: bool = False
) -> Detection | None:
    """AWS event statements, with a file-level framework label (``aws-lambda`` for a handler
    file, else the first producer transport).

    ``is_fixture`` gates only the untyped-CommonJS handler fallback (:func:`_untyped_cjs_handler`),
    whose looser signature would otherwise match a mocked ``exports.handler`` in a test file — the
    detector is not fixture-guarded at the call site, so the guard is applied here."""
    if not _has_aws(source):
        return None
    found: list[Emit] = []
    file_fw: str | None = None
    is_lambda = False

    for call in _walk(root, frozenset({"call_expression"})):
        info = _producer(call, source)
        if info is None:
            continue
        sem, method, endpoint, fw = info
        # Enrich the base statement in place when it is unclassified; inside a lambda the base
        # skipped, or on a span already classified → its own statement.
        found.append(Emit(
            call, {"semanticType": sem, "framework": fw, "method": method},
            fill={"endpoint": endpoint}, enrich="unclassified",
        ))
        file_fw = file_fw or fw

    # Handler entry points, most-precise first: a typed const (``const h: SQSHandler = …``), an
    # untyped but AWS-event-parameterised ``handler`` export (``export const handler = (e:
    # S3Event) => …`` / ``exports.handler = …``), and — last resort — the untyped CommonJS
    # ``exports.handler = () => …`` shape (generic ``aws-lambda``, non-fixture only). One
    # statement per handler; the earliest (most specific) match wins.
    for decl in _walk(root, frozenset({"variable_declarator", "assignment_expression"})):
        hinfo = _handler(decl, source) if decl.type == "variable_declarator" else None
        if hinfo is None:
            hinfo = _untyped_handler(decl, source)
        if hinfo is None and not is_fixture:  # last resort: the untyped CommonJS handler shape
            hinfo = _untyped_cjs_handler(decl, source)
        if hinfo is None:
            continue
        sem, fw, route_kind, hname = hinfo
        line = decl.start_point[0] + 1
        found.append(Emit(decl, {
            "semanticType": sem,
            # The handler is declared by this declarator/assignment: its line is the handler's.
            "framework": fw, "handler": hname, "handlerLine": line if hname else None,
            "routeKind": route_kind, "method": "PERIODIC" if sem == "timer" else None,
        }))
        is_lambda = True

    if not found:
        return None
    return Detection(found, label="aws-lambda" if is_lambda else file_fw)


_ENTRY_TYPES = frozenset({"route", "eventbus_consumer", "timer"})


def _exported_functions(root: Node, source: bytes) -> dict[str, Node]:
    """Exported functions by name — ``export const n = () => …``, ``export function n``,
    ``exports.n = …`` / ``module.exports.n = …`` — at the node a handler record sits on."""
    out: dict[str, Node] = {}
    for child in root.named_children:
        if child.type == "export_statement":
            for decl in child.named_children:
                if decl.type == "function_declaration":
                    name = decl.child_by_field_name("name")
                    if name is not None:
                        out.setdefault(node_text(name, source), decl)
                elif decl.type in ("lexical_declaration", "variable_declaration"):
                    for v in decl.named_children:
                        name = v.child_by_field_name("name") if v.type == "variable_declarator" else None
                        if name is not None and _fn_value(v) is not None:
                            out.setdefault(node_text(name, source), v)
        elif child.type == "expression_statement" and child.named_children:
            assign = child.named_children[0]
            left = assign.child_by_field_name("left") if assign.type == "assignment_expression" else None
            obj = left.child_by_field_name("object") if left is not None and \
                left.type == "member_expression" else None
            prop = left.child_by_field_name("property") if left is not None and \
                left.type == "member_expression" else None
            if obj is not None and prop is not None and node_text(obj, source) in \
                    _CJS_EXPORT_RECEIVERS and _fn_value(assign) is not None:
                out.setdefault(node_text(prop, source), assign)
    return out


def _apply_deployed(found: list[Emit], root: Node, source: bytes, path: str,
                    index: object) -> list[Emit]:
    """Copy the deployment config's triggers (:mod:`..aws_deploy`) onto this file's handlers:
    the first trigger onto the handler's record, one more record per further trigger. A handler
    the config names but the code does not type gets its records at its declaration."""
    fact = deployed_handlers(index)
    stem = path.rsplit(".", 1)[0]
    handlers = fact.modules.get(stem, {}) if fact is not None else {}
    if not handlers:
        return found
    out: list[Emit] = []
    seen: set[str] = set()
    for emit in found:
        name = emit.fields.get("handler")
        triggers = handlers.get(name, ()) if isinstance(name, str) and \
            emit.fields.get("semanticType") in _ENTRY_TYPES else ()
        if not isinstance(name, str) or not triggers or name in seen:
            out.append(emit)
            continue
        seen.add(name)
        for t in triggers:
            out.append(Emit(emit.node, {**emit.fields, **t.fields()}))
    exported = None
    for name in sorted(set(handlers) - seen):
        exported = exported if exported is not None else _exported_functions(root, source)
        node = exported.get(name)
        if node is None:
            continue
        for t in handlers[name]:
            out.append(Emit(node, {"handler": name, "handlerLine": node.start_point[0] + 1,
                                   **t.fields()}))
    return out


def _detect(dc: DetectContext) -> Detection | None:
    detection = detect_aws_events(dc.root, dc.source, dc.path, is_fixture=dc.is_fixture)
    found = list(detection.emits) if detection is not None else []
    if not dc.is_fixture:
        found = _apply_deployed(found, dc.root, dc.source, dc.path, dc.ctx.resolution_index)
    if not found:
        return None
    lambda_handler = any(e.fields.get("semanticType") in _ENTRY_TYPES for e in found)
    return Detection(found, label="aws-lambda" if lambda_handler else
                     (detection.label if detection is not None else None))


# Not fixture-skipped: only the loose CommonJS handler fallback is fixture-gated, inside.
register_detector(Detector(
    name="aws-events", gate=Gate("typescript", order=20), detect=_detect,
    frameworks=(
        "aws-lambda", "aws-sdk", "aws-sns", "aws-sqs", "aws-eventbridge", "aws-kinesis",
        "aws-dynamodb", "aws-ses", "aws-cloudfront", "aws-apigw",
    ),
))
