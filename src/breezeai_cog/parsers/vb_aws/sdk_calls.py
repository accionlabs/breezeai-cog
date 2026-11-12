"""AWS SDK for .NET calls in VB: request/response services as ``api_call``, messaging as event-bus
statements — the C# detector's rules (``csharp_aws/sdk_calls.py``) and service tables
(``aws_sdk_common``), read from the VB tree.

::

    Private ReadOnly _s3 As IAmazonS3
    Await _s3.PutObjectAsync(request)
        → api_call, framework aws-s3, endpoint PutObjectAsync

    Private ReadOnly _sns As IAmazonSimpleNotificationService
    Await _sns.PublishAsync(New PublishRequest With {.TopicArn = "arn:aws:sns:eu-west-1:1:order-created"})
        → eventbus_publish, framework aws-sns, method PublishAsync, endpoint order-created

**Evidence is the receiver's declared type, never the verb** (``PublishAsync`` is also MediatR's):
a name declared as an AWS client type where the call is (``vb/scopes.py``), ``Me.`` one, or a
client created in place. SQS ``ReceiveMessage(Async)`` is a consumer binding whose handler is the
method the call is in. **Endpoint only from a string literal** in the request — a ``With { … }``
initializer on the call or on a local the call passes, or the positional overload's first
argument; a ``Const`` or an injected address stays empty.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..aws_sdk_common import (
    ADDRESS_KEYS, DIRECT_ADDRESS_KEYS, DOTNET_API_SERVICES, DOTNET_CLIENT_TYPES, DOTNET_MESSAGING,
    DOTNET_NOT_OPERATIONS, DOTNET_POSITIONAL_KEY, address_name,
)
from ..emits import Emit
from ..treesitter import first_line, node_text
from ..vb.functions import method_name
from ..vb.scopes import (
    Call, enclosing_member, initializer_pairs, iter_calls, local_creation, receiver_type,
    string_value,
)

#: Client type (lower-cased: VB names are case-insensitive) → service.
_CLIENT_TYPES = {k.lower(): v for k, v in DOTNET_CLIENT_TYPES.items()}
_NOT_OPERATIONS = frozenset(m.lower() for m in DOTNET_NOT_OPERATIONS)


def _service(call: Call, source: bytes) -> str | None:
    typ = receiver_type(call.receiver, source)
    return _CLIENT_TYPES.get(typ.lower()) if typ is not None else None


def _creation(arg: Node, source: bytes) -> Node | None:
    if arg.type == "new_expression":
        return arg
    return local_creation(arg, source) if arg.type == "identifier" else None


def _address(call: Call, source: bytes, service: str,
             positional_min: int) -> tuple[str | None, str | None]:
    """(endpoint, address key present) for a messaging call, from string literals only."""
    if not call.args:
        return None, None
    first = call.args[0]
    creation = _creation(first, source)
    if creation is None:
        key = DOTNET_POSITIONAL_KEY.get(service)
        literal = string_value(first, source)
        if key is None or len(call.args) < positional_min or literal is None:
            return None, None
        return address_name(key, literal), key
    key_found: str | None = None
    names: set[str] = set()
    for key, value in initializer_pairs(creation, source):
        if key not in ADDRESS_KEYS:
            continue
        key_found = key_found or key
        if key in DIRECT_ADDRESS_KEYS:
            key_found = key
        literal = string_value(value, source)
        names.add(address_name(key, literal) if literal is not None else "")
    if len(names) != 1 or "" in names:
        return None, key_found  # absent, injected, or several different addresses
    return names.pop(), key_found


def _handler(call: Call, source: bytes) -> dict[str, object]:
    """``handler`` / ``handlerLine`` of the method the call is in."""
    member = enclosing_member(call.node)
    if member is None:
        return {}
    name, _ = method_name(member, source)
    name = name or ("New" if member.type == "constructor_declaration" else None)
    return {"handler": name, "handlerLine": member.start_point[0] + 1} if name else {}


def detect_sdk_calls(root: Node, source: bytes) -> list[Emit]:
    found: list[Emit] = []
    for call in iter_calls(root, source):
        method = call.method
        if method.lower() in _NOT_OPERATIONS or not method[:1].isupper():
            continue
        service = _service(call, source)
        if service is None:
            continue
        fill: dict[str, object] = {}
        if service in DOTNET_API_SERVICES:
            if method.startswith("End") and method[3:4].isupper():
                continue  # the APM pattern's completion half; ``BeginX`` is the call
            fields: dict[str, object] = {"semanticType": "api_call", "framework": service,
                                         "endpoint": method}
        else:
            operation = method[:-5] if method.endswith("Async") else method
            semantic = DOTNET_MESSAGING[service].get(operation)
            if semantic is None:
                continue
            consumer = semantic == "eventbus_consumer"
            endpoint, key = _address(call, source, service, 1 if consumer else 2)
            if service == "aws-sns" and key in DIRECT_ADDRESS_KEYS:
                semantic = "eventbus_send"  # an SMS reaches one recipient; nothing subscribes
            fields = {"semanticType": semantic, "framework": service,
                      "method": "CONSUMER" if consumer else method}
            if consumer:
                fields.update(_handler(call, source))
            fill = {"endpoint": endpoint}
        found.append(Emit(
            call.node, fields, fill=fill, enrich="unclassified",
            text=first_line(node_text(call.node, source))[:120],
        ))
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = detect_sdk_calls(dc.root, dc.source)
    return Detection(found) if found else None


# Mock / fixture files are skipped, as for C#. Every client type name or its namespace
# (Amazon.S3.Transfer for TransferUtility) contains "Amazon".
register_detector(Detector(
    name="aws-sdk-calls", gate=Gate("vb", order=10, guard=b"Amazon", skip_fixtures=True),
    detect=_detect,
    frameworks=("aws-s3", "aws-ssm", "aws-cognito", "aws-lambda", "aws-sns", "aws-sqs",
                "aws-eventbridge"),
))
