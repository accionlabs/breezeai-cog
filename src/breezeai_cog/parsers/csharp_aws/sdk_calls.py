"""AWS SDK for .NET calls: request/response services as ``api_call``, messaging as event-bus
statements.

::

    private readonly IAmazonS3 _s3;
    await _s3.PutObjectAsync(request);
        → api_call, framework aws-s3, endpoint PutObjectAsync

    private readonly IAmazonSimpleNotificationService _sns;
    await _sns.PublishAsync(new PublishRequest { TopicArn = "arn:aws:sns:eu-west-1:1:order-created" });
        → eventbus_publish, framework aws-sns, method PublishAsync, endpoint order-created

**Evidence is the receiver's declared type, never the verb.** ``PublishAsync`` is also MediatR's
and MassTransit's, ``SendMessageAsync`` is every chat client's; only a call on a name declared
(field, property, parameter, local, or ``new``) as an AWS client type counts.

* **S3, Systems Manager, Cognito user pools, Lambda** — every operation is an outbound call to
  the service: ``api_call``, ``framework`` the service, ``endpoint`` the SDK method name as
  written (the HTTP verb and URL live inside the SDK, so ``method`` stays empty). A method that
  never leaves the process (``GetPreSignedURL``, ``Dispose``) is not an operation.
* **SNS publish, SQS send, EventBridge put-events** — ``eventbus_publish`` / ``eventbus_send``
  as the TypeScript AWS detector records them; an SNS publish to a ``PhoneNumber`` reaches one
  recipient, so it is a send. Other operations on these clients (creating a topic, receiving a
  message) are not recorded: a receive loop is not a consumer binding, and the management calls
  are not traffic.
* **Endpoint only from a literal** address in the request (an object initializer on the call or
  on a local the call passes, or the positional overload's first argument), written by name as
  a consumer writes it (``aws_sdk_common.address_name``); an injected address stays empty.

DynamoDB is data access and is recorded by the shared classifier (``detection/db_queries.py``)
from the same declared-type evidence, not here.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from ...schemas import SemanticType
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..aws_sdk_common import ADDRESS_KEYS, DIRECT_ADDRESS_KEYS, address_name
from ..csharp.functions import type_map
from ..csharp.statements import collect_typed_names
from ..emits import Emit
from ..treesitter import first_line, node_text

#: Client types → service (the ``framework`` value).
_CLIENT_TYPES = {
    "IAmazonS3": "aws-s3", "AmazonS3Client": "aws-s3",
    "ITransferUtility": "aws-s3", "TransferUtility": "aws-s3",
    "IAmazonSimpleSystemsManagement": "aws-ssm", "AmazonSimpleSystemsManagementClient": "aws-ssm",
    "IAmazonCognitoIdentityProvider": "aws-cognito",
    "AmazonCognitoIdentityProviderClient": "aws-cognito",
    "IAmazonLambda": "aws-lambda", "AmazonLambdaClient": "aws-lambda",
    "IAmazonSimpleNotificationService": "aws-sns", "AmazonSimpleNotificationServiceClient": "aws-sns",
    "IAmazonSQS": "aws-sqs", "AmazonSQSClient": "aws-sqs",
    "IAmazonEventBridge": "aws-eventbridge", "AmazonEventBridgeClient": "aws-eventbridge",
    "IAmazonCloudWatchEvents": "aws-eventbridge", "AmazonCloudWatchEventsClient": "aws-eventbridge",
}

#: Services whose every operation is a request/response call.
_API_SERVICES = frozenset({"aws-s3", "aws-ssm", "aws-cognito", "aws-lambda"})

#: Messaging operations (sync and ``Async``) → semantic type, per service.
_MESSAGING: dict[str, dict[str, SemanticType]] = {
    "aws-sns": {"Publish": "eventbus_publish", "PublishBatch": "eventbus_publish"},
    "aws-sqs": {"SendMessage": "eventbus_send", "SendMessageBatch": "eventbus_send"},
    "aws-eventbridge": {"PutEvents": "eventbus_publish"},
}

#: The address the positional overload's first argument stands for
#: (``PublishAsync(topicArn, message)``, ``SendMessageAsync(queueUrl, body)``).
_POSITIONAL_KEY = {"aws-sns": "TopicArn", "aws-sqs": "QueueUrl"}

#: Client methods that are not service operations: local signing, lifetime, ``object``.
_NOT_OPERATIONS = frozenset({
    "Dispose", "DetermineServiceOperationEndpoint", "GetPreSignedURL", "GetPreSignedURLAsync",
    "Equals", "GetHashCode", "ToString", "GetType",
})

#: A regular or verbatim C# string literal without interpolation.
_LITERAL = re.compile(r'^@?"([^"{}\\]*)"$')


def _iter(root: Node, kind: str) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == kind:
            yield node
        stack.extend(reversed(node.named_children))


def _literal(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type not in ("string_literal", "verbatim_string_literal"):
        return None
    m = _LITERAL.match(node_text(node, source))
    return m.group(1) if m else None


def _receiver_and_method(call: Node, source: bytes) -> tuple[Node, str] | None:
    """``(receiver node, method name)`` for ``x.Method(…)`` / ``x.Method<T>(…)``."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_access_expression":
        return None
    name = fn.child_by_field_name("name")
    receiver = fn.child_by_field_name("expression")
    if name is None or receiver is None:
        return None
    if name.type == "generic_name":
        name = next((c for c in name.named_children if c.type == "identifier"), None)
        if name is None:
            return None
    return receiver, node_text(name, source)


def _service(receiver: Node, source: bytes, clients: dict[str, str]) -> str | None:
    """The service of a receiver: a name declared as a client type (``_s3``, ``this._s3``), or a
    client created in place (``new TransferUtility(s3).UploadAsync(…)``)."""
    if receiver.type == "identifier":
        return clients.get(node_text(receiver, source))
    if receiver.type == "member_access_expression":
        obj = receiver.child_by_field_name("expression")
        name = receiver.child_by_field_name("name")
        if obj is not None and obj.type in ("this", "this_expression") and name is not None:
            return clients.get(node_text(name, source))
        return None
    if receiver.type == "parenthesized_expression" and receiver.named_children:
        return _service(receiver.named_children[0], source, clients)
    if receiver.type == "object_creation_expression":
        typ = receiver.child_by_field_name("type")
        return _CLIENT_TYPES.get(node_text(typ, source).rsplit(".", 1)[-1]) if typ else None
    return None


def _initializer_pairs(creation: Node, source: bytes) -> Iterator[tuple[str, Node]]:
    """``(key, value node)`` for every ``Key = value`` in an object initializer, nested ones
    included (EventBridge puts ``EventBusName`` on each entry)."""
    for assign in _iter(creation, "assignment_expression"):
        left = assign.child_by_field_name("left")
        right = assign.child_by_field_name("right")
        if left is not None and right is not None and left.type == "identifier":
            yield node_text(left, source), right


def _local_creation(name: str, call: Node, source: bytes) -> Node | None:
    """The ``new …Request { … }`` a local in the call's enclosing method was initialised with."""
    scope = call.parent
    while scope is not None and scope.type not in (
        "method_declaration", "constructor_declaration", "local_function_statement",
        "lambda_expression", "accessor_declaration",
    ):
        scope = scope.parent
    if scope is None:
        return None
    found: Node | None = None
    for decl in _iter(scope, "variable_declarator"):
        kids = decl.named_children
        if len(kids) > 1 and node_text(kids[0], source) == name:
            if found is not None:
                return None  # declared twice in the method: which one reaches the call is unclear
            found = kids[1] if kids[1].type == "object_creation_expression" else None
    return found


def _address(call: Node, source: bytes, service: str) -> tuple[str | None, str | None]:
    """(literal endpoint, address key present) for a messaging call."""
    args = call.child_by_field_name("arguments")
    values = [a.named_children[-1] for a in args.named_children if a.named_children] \
        if args is not None else []
    if not values:
        return None, None
    first = values[0]
    creation = first if first.type == "object_creation_expression" else (
        _local_creation(node_text(first, source), call, source) if first.type == "identifier"
        else None)
    if creation is None:
        key = _POSITIONAL_KEY.get(service)
        literal = _literal(first, source)
        if key is None or len(values) < 2 or literal is None:
            return None, None
        return address_name(key, literal), key
    key_found: str | None = None
    names: set[str] = set()
    for key, value in _initializer_pairs(creation, source):
        if key not in ADDRESS_KEYS:
            continue
        key_found = key_found or key
        if key in DIRECT_ADDRESS_KEYS:
            key_found = key
        literal = _literal(value, source)
        names.add(address_name(key, literal) if literal is not None else "")
    if len(names) != 1 or "" in names:
        return None, key_found  # absent, injected, or several different addresses
    return names.pop(), key_found


def _operation(method: str) -> str:
    return method[:-5] if method.endswith("Async") else method


def detect_sdk_calls(root: Node, source: bytes) -> list[Emit]:
    clients = collect_typed_names(root, source, type_map(root, source), _CLIENT_TYPES)
    found: list[Emit] = []
    for call in _iter(root, "invocation_expression"):
        parts = _receiver_and_method(call, source)
        if parts is None:
            continue
        receiver, method = parts
        if method in _NOT_OPERATIONS or not method[:1].isupper():
            continue
        service = _service(receiver, source, clients)
        if service is None:
            continue
        fields: dict[str, object]
        fill: dict[str, object] = {}
        if service in _API_SERVICES:
            if method.startswith("End") and method[3:4].isupper():
                continue  # the APM pattern's completion half; ``BeginX`` is the call
            fields = {"semanticType": "api_call", "framework": service, "endpoint": method}
        else:
            semantic = _MESSAGING[service].get(_operation(method))
            if semantic is None:
                continue
            endpoint, key = _address(call, source, service)
            if service == "aws-sns" and key in DIRECT_ADDRESS_KEYS:
                semantic = "eventbus_send"  # an SMS reaches one recipient; nothing subscribes
            fields = {"semanticType": semantic, "framework": service, "method": method}
            fill = {"endpoint": endpoint}
        found.append(Emit(
            call, fields, fill=fill, enrich="unclassified",
            text=first_line(node_text(call, source))[:120],
        ))
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = detect_sdk_calls(dc.root, dc.source)
    return Detection(found) if found else None


# Calls in mock / fixture files go to a fake, so those files are skipped. The client type names
# all contain "Amazon" except TransferUtility, whose namespace (Amazon.S3.Transfer) does.
register_detector(Detector(
    name="aws-sdk-calls", gate=Gate("csharp", order=70, guard=b"Amazon", skip_fixtures=True),
    detect=_detect,
    frameworks=("aws-s3", "aws-ssm", "aws-cognito", "aws-lambda", "aws-sns", "aws-sqs",
                "aws-eventbridge"),
))
