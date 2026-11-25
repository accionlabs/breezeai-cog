"""AWS SDK for Python (boto3) calls: request/response services as ``api_call``, messaging as
event-bus statements.

::

    s3 = boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=key, Body=data)
        → api_call, framework aws-s3, endpoint put_object

    self.sns = session.client("sns")            # session = boto3.Session()
    self.sns.publish(TopicArn="arn:aws:sns:eu-west-1:1:order-created", Message=m)
        → eventbus_publish, framework aws-sns, method publish, endpoint order-created

    queue = boto3.resource("sqs").Queue(QUEUE_URL)
    queue.send_message(MessageBody=body)
        → eventbus_send, framework aws-sqs, method send_message, endpoint <the queue>

**Evidence is the receiver, never the verb.** ``publish`` is every pub/sub library's and
``send_message`` every chat client's; only a call on a name bound to ``boto3.client("<service>")``,
``boto3.resource("<service>")`` or ``.client`` / ``.resource`` of a ``boto3.Session`` counts —
a local, a module variable, ``self.<attr>`` (assigned in the class's methods or body), or the
factory call in place. A name bound anywhere in its scope to something else (a parameter, a
loop variable, a second value) is not resolved. The file must import ``boto3``.

* **S3, Systems Manager, Cognito user pools, Lambda** — every client operation is an outbound
  call to the service: ``api_call``, ``framework`` the service, ``endpoint`` the boto3 method as
  written. A method that never leaves the process (``generate_presigned_url``,
  ``get_paginator``, ``close``) is not an operation. The S3 resource and its ``Bucket`` /
  ``Object`` are recorded the same way.
* **SNS publish, SQS send, EventBridge put-events** — ``eventbus_publish`` / ``eventbus_send``
  as the TypeScript and C# AWS detectors record them; an SNS publish to a ``PhoneNumber``
  reaches one recipient, so it is a send. On resources: ``Queue.send_message(s)`` and
  ``Topic.publish``. Management calls (creating a queue, deleting a message) are not traffic.
* **SQS receive** — ``receive_message`` (``Queue.receive_messages``) takes messages off the
  queue: a consumer binding (``eventbus_consumer``, ``method: CONSUMER``) whose handler is the
  function the call is in.
* **DynamoDB** — data access, as the C# AWS detector records it: ``db_method_call``,
  ``method`` the operation, ``dataAccessHint: dynamodb``. Item and query operations of the
  client (``put_item``, ``query``, ``execute_statement`` …), of a resource ``Table``
  (``table.put_item``) and the resource's batch operations; table management
  (``create_table``, ``describe_table``) is not data access.
* **Endpoint only from a literal or a module constant** — ``QueueUrl`` / ``TopicArn`` /
  ``EventBusName`` / ``PhoneNumber`` keyword (or dictionary key, as in EventBridge ``Entries``),
  or the queue URL / topic ARN a resource was created with, written by name as a consumer
  writes it (``aws_sdk_common.address_name``). An injected address stays empty.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from tree_sitter import Node

from ...schemas import SemanticType
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..aws_sdk_common import ADDRESS_KEYS, DIRECT_ADDRESS_KEYS, address_name
from ..emits import Emit
from ..python_common import Origins, argument, calls, keyword, literal, module_constants
from ..treesitter import first_line, node_text

#: boto3 service name → ``framework``.
_SERVICES = {
    "s3": "aws-s3", "ssm": "aws-ssm", "cognito-idp": "aws-cognito", "lambda": "aws-lambda",
    "sns": "aws-sns", "sqs": "aws-sqs", "events": "aws-eventbridge",
}
#: Services whose every operation is a request/response call.
_API_SERVICES = frozenset({"s3", "ssm", "cognito-idp", "lambda"})
#: Services with a boto3 resource interface this detector follows.
_RESOURCE_SERVICES = frozenset({"s3", "sqs", "sns", "dynamodb"})

#: DynamoDB data access operations, per receiver: the client, the service resource, a ``Table``.
#: The C# detector's list (``detection/db_queries.py``) in boto3's spelling.
_DYNAMODB_ITEMS = frozenset({"get_item", "put_item", "update_item", "delete_item", "query", "scan"})
_DYNAMODB_BATCH = frozenset({"batch_get_item", "batch_write_item"})
_DYNAMODB_ACCESS: dict[tuple[str, str], frozenset[str]] = {
    ("client", ""): _DYNAMODB_ITEMS | _DYNAMODB_BATCH | frozenset({
        "transact_get_items", "transact_write_items",
        "execute_statement", "batch_execute_statement", "execute_transaction",
    }),
    ("resource", ""): _DYNAMODB_BATCH,
    ("sub", "Table"): _DYNAMODB_ITEMS,
}

#: Client messaging operations → semantic type, per service.
_MESSAGING: dict[str, dict[str, SemanticType]] = {
    "sns": {"publish": "eventbus_publish", "publish_batch": "eventbus_publish"},
    "sqs": {"send_message": "eventbus_send", "send_message_batch": "eventbus_send",
            "receive_message": "eventbus_consumer"},
    "events": {"put_events": "eventbus_publish"},
}
#: Resource messaging operations → semantic type, per (service, sub-resource).
_RESOURCE_MESSAGING: dict[tuple[str, str], dict[str, SemanticType]] = {
    ("sqs", "Queue"): {"send_message": "eventbus_send", "send_messages": "eventbus_send",
                       "receive_messages": "eventbus_consumer"},
    ("sns", "Topic"): {"publish": "eventbus_publish"},
}

#: Client / resource methods that are not service operations: local signing, paginator and
#: waiter factories, lifetime.
_NOT_OPERATIONS = frozenset({
    "generate_presigned_url", "generate_presigned_post", "get_paginator", "get_waiter",
    "can_paginate", "close", "get_available_subresources",
    "wait_until_exists", "wait_until_not_exists",
})

_FACTORIES = {"boto3.client": "client", "boto3.resource": "resource"}
_SESSIONS = frozenset({"boto3.Session", "boto3.session.Session"})
_SCOPES = ("module", "class_definition", "function_definition", "lambda")
_FUNCTIONS = ("function_definition", "lambda")


@dataclass(frozen=True, slots=True)
class _Aws:
    """What a name holds: a ``session``, a ``client`` / ``resource`` of a service, a ``sub``
    resource (``Queue``, ``Topic``, ``Bucket``, ``Object``) with its address, or a resource's
    ``meta`` (whose ``client`` is the service's client)."""

    kind: str
    service: str = ""
    sub: str = ""
    address: str | None = None


_SESSION = _Aws("session")
_UNKNOWN = object()  # a binding whose value this detector does not follow
_Entry = tuple[object, Node]  # (value node, "annot" type node, or _UNKNOWN; scope)


def _key(node: Node) -> tuple[int, int, str]:
    return (node.start_byte, node.end_byte, node.type)


def _scope_of(node: Node) -> Node:
    parent = node.parent
    while parent is not None and parent.type not in _SCOPES:
        parent = parent.parent
    return parent if parent is not None else node


def _chain(scope: Node) -> Iterator[Node]:
    """The scope, then the scopes its names are looked up in — a class body is skipped."""
    yield scope
    node = scope.parent
    while node is not None:
        if node.type in ("module", *_FUNCTIONS):
            yield node
        node = node.parent


def _first_param(fn: Node, source: bytes) -> str | None:
    params = fn.child_by_field_name("parameters")
    first = params.named_children[0] if params is not None and params.named_children else None
    if first is None:
        return None
    if first.type == "identifier":
        return node_text(first, source)
    if first.type == "typed_parameter" and first.named_children:
        return node_text(first.named_children[0], source)
    return None


def _method_class(fn: Node, source: bytes) -> Node | None:
    """The class ``fn`` is a method of (not a ``staticmethod``), else None."""
    parent = fn.parent
    if parent is not None and parent.type == "decorated_definition":
        decorators = [node_text(d, source) for d in parent.named_children if d.type == "decorator"]
        if any(d.lstrip("@").strip() == "staticmethod" for d in decorators):
            return None
        parent = parent.parent
    if parent is not None and parent.type == "block" and parent.parent is not None \
            and parent.parent.type == "class_definition":
        return parent.parent
    return None


def _target_names(node: Node) -> Iterator[Node]:
    """Identifiers and attributes bound by an assignment / loop / ``as`` target."""
    if node.type in ("identifier", "attribute"):
        yield node
    elif node.type in ("pattern_list", "tuple_pattern", "list_pattern", "tuple", "list",
                       "list_splat_pattern", "parenthesized_expression", "as_pattern_target"):
        for child in node.named_children:
            yield from _target_names(child)


class _Bindings:
    """Every name bound in each scope of one file, and every ``self.<attr>`` of each class,
    evaluated lazily to an :class:`_Aws` value when all its bindings agree."""

    def __init__(self, root: Node, source: bytes, origins: Origins) -> None:
        self.source = source
        self.origins = origins
        self.root = root
        self.names: dict[tuple[int, int, str], dict[str, list[_Entry]]] = {}
        self.attrs: dict[tuple[int, int, str], dict[str, list[_Entry]]] = {}
        self.constants = module_constants(root, source)
        self._memo: dict[tuple[object, ...], _Aws | None] = {}
        self._collect(root, root)

    # --- collection -------------------------------------------------------------------------

    def _bind(self, scope: Node, name: str, entry: _Entry) -> None:
        self.names.setdefault(_key(scope), {}).setdefault(name, []).append(entry)

    def _bind_target(self, target: Node, scope: Node, value: object) -> None:
        if target.type == "identifier":
            name = node_text(target, self.source)
            self._bind(scope, name, (value, scope))
            if scope.type == "class_definition":
                self.attrs.setdefault(_key(scope), {}).setdefault(name, []).append((value, scope))
        elif target.type == "attribute":
            owner = self._self_attr(target, scope)
            if owner is not None:
                cls, attr = owner
                self.attrs.setdefault(_key(cls), {}).setdefault(attr, []).append((value, scope))

    def _params(self, fn: Node) -> None:
        params = fn.child_by_field_name("parameters")
        for param in params.named_children if params is not None else []:
            if param.type == "identifier":
                self._bind(fn, node_text(param, self.source), (_UNKNOWN, fn))
                continue
            name = param.child_by_field_name("name") or next(
                (c for c in param.named_children if c.type == "identifier"), None)
            if name is None:
                continue
            typ = param.child_by_field_name("type") \
                if param.type in ("typed_parameter", "typed_default_parameter") else None
            self._bind(fn, node_text(name, self.source), (("annot", typ) if typ else _UNKNOWN, fn))

    def _collect(self, node: Node, scope: Node) -> None:
        for child in node.named_children:
            kind = child.type
            if kind in ("function_definition", "class_definition"):
                name = child.child_by_field_name("name")
                if name is not None:
                    self._bind(scope, node_text(name, self.source), (_UNKNOWN, scope))
                if kind == "function_definition":
                    self._params(child)
                self._collect(child, child)
                continue
            if kind == "lambda":
                self._params(child)
                self._collect(child, child)
                continue
            if kind == "assignment":
                left, right = child.child_by_field_name("left"), child.child_by_field_name("right")
                typ = child.child_by_field_name("type")
                value: object = right if right is not None else (("annot", typ) if typ else _UNKNOWN)
                if left is not None and left.type in ("identifier", "attribute"):
                    self._bind_target(left, scope, value)
                elif left is not None:
                    for target in _target_names(left):
                        self._bind_target(target, scope, _UNKNOWN)
            elif kind in ("augmented_assignment", "for_statement", "for_in_clause"):
                left = child.child_by_field_name("left")
                for target in _target_names(left) if left is not None else ():
                    self._bind_target(target, scope, _UNKNOWN)
            elif kind == "as_pattern":
                alias = child.child_by_field_name("alias")
                for target in _target_names(alias) if alias is not None else ():
                    self._bind_target(target, scope, _UNKNOWN)
            elif kind == "named_expression":
                name = child.child_by_field_name("name")
                if name is not None:
                    self._bind_target(name, scope, _UNKNOWN)
            elif kind in ("global_statement", "nonlocal_statement"):
                # The name is rebound in another scope from here: not followed anywhere.
                for ident in child.named_children:
                    self._bind(self.root, node_text(ident, self.source), (_UNKNOWN, scope))
                    self._bind(scope, node_text(ident, self.source), (_UNKNOWN, scope))
            self._collect(child, scope)

    # --- lookup -----------------------------------------------------------------------------

    def _binding_scope(self, name: str, scope: Node) -> Node | None:
        for s in _chain(scope):
            if name in self.names.get(_key(s), {}):
                return s
        return None

    def _self_attr(self, attr: Node, scope: Node) -> tuple[Node, str] | None:
        """``(class, attr)`` for ``self.attr`` where ``self`` is a method's first parameter."""
        obj, name = attr.child_by_field_name("object"), attr.child_by_field_name("attribute")
        if obj is None or name is None or obj.type != "identifier":
            return None
        owner = self._binding_scope(node_text(obj, self.source), scope)
        if owner is None or owner.type != "function_definition" \
                or _first_param(owner, self.source) != node_text(obj, self.source):
            return None
        cls = _method_class(owner, self.source)
        return (cls, node_text(name, self.source)) if cls is not None else None

    def _agree(self, key: tuple[object, ...], entries: list[_Entry]) -> _Aws | None:
        if key in self._memo:
            return self._memo[key]
        self._memo[key] = None  # a cycle resolves to nothing
        values = {self._entry(entry) for entry in entries}
        result = values.pop() if len(values) == 1 else None
        self._memo[key] = result
        return result

    def _entry(self, entry: _Entry) -> _Aws | None:
        value, scope = entry
        if value is _UNKNOWN:
            return None
        if isinstance(value, tuple):  # ("annot", type node)
            written = node_text(value[1], self.source).strip("\"'")
            return _SESSION if self.origins.qualified(written) in _SESSIONS else None
        assert isinstance(value, Node)
        return self.eval(value, scope)

    def lookup(self, name: str, scope: Node) -> _Aws | None:
        owner = self._binding_scope(name, scope)
        if owner is None:
            return None
        return self._agree(("name", _key(owner), name), self.names[_key(owner)][name])

    def string(self, node: Node | None, scope: Node) -> str | None:
        """A plain string literal, or a module constant holding one."""
        if node is None:
            return None
        if node.type == "identifier":
            name = node_text(node, self.source)
            owner = self._binding_scope(name, scope)
            return self.constants.get(name) if owner is not None and owner.type == "module" else None
        return literal(node, self.source) or None

    # --- evaluation -------------------------------------------------------------------------

    def eval(self, node: Node, scope: Node) -> _Aws | None:
        kind = node.type
        if kind == "parenthesized_expression" and node.named_children:
            return self.eval(node.named_children[0], scope)
        if kind == "assignment":  # a = b = boto3.client(…)
            right = node.child_by_field_name("right")
            return self.eval(right, scope) if right is not None else None
        if kind == "identifier":
            return self.lookup(node_text(node, self.source), scope)
        if kind == "attribute":
            owner = self._self_attr(node, scope)
            if owner is not None:
                cls, attr = owner
                entries = self.attrs.get(_key(cls), {}).get(attr)
                return self._agree(("attr", _key(cls), attr), entries) if entries else None
            obj, attr_node = node.child_by_field_name("object"), node.child_by_field_name("attribute")
            value = self.eval(obj, scope) if obj is not None else None
            attr_name = node_text(attr_node, self.source) if attr_node is not None else ""
            if value is not None and value.kind in ("resource", "sub") and attr_name == "meta":
                return _Aws("meta", value.service)
            if value is not None and value.kind == "meta" and attr_name == "client":
                return _Aws("client", value.service)
            return None
        if kind == "call":
            return self._call(node, scope)
        return None

    def _imported(self, fn: Node, scope: Node) -> str | None:
        """The dotted origin of a callee written as an imported name (``boto3.client``,
        ``client`` from ``from boto3 import client``), unless a nearer scope rebinds it."""
        if fn.type not in ("identifier", "attribute"):
            return None
        written = node_text(fn, self.source)
        if not all(part.isidentifier() for part in written.split(".")):
            return None
        owner = self._binding_scope(written.split(".", 1)[0], scope)
        if owner is not None and owner.type != "module":
            return None
        return self.origins.qualified(written)

    def _factory(self, call: Node, kind: str, scope: Node) -> _Aws | None:
        service = self.string(argument(call, self.source, 0, "service_name"), scope)
        if service is None:
            return None
        if kind == "client" and (service in _SERVICES or service == "dynamodb"):
            return _Aws("client", service)
        if kind == "resource" and service in _RESOURCE_SERVICES:
            return _Aws("resource", service)
        return None

    def _call(self, call: Node, scope: Node) -> _Aws | None:
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        origin = self._imported(fn, scope)
        if origin in _FACTORIES:
            return self._factory(call, _FACTORIES[origin], scope)
        if origin in _SESSIONS:
            return _SESSION
        if fn.type != "attribute":
            return None
        obj, name_node = fn.child_by_field_name("object"), fn.child_by_field_name("attribute")
        value = self.eval(obj, scope) if obj is not None else None
        if value is None or name_node is None:
            return None
        name = node_text(name_node, self.source)
        if value.kind == "session" and name in ("client", "resource"):
            return self._factory(call, name, scope)
        if value.kind == "resource":
            return self._sub_resource(value.service, name, call, scope)
        if value == _Aws("sub", "s3", "Bucket") and name == "Object":
            return _Aws("sub", "s3", "Object")
        return None

    def _sub_resource(self, service: str, name: str, call: Node, scope: Node) -> _Aws | None:
        """``Bucket`` / ``Object`` of S3, ``Queue`` of SQS, ``Topic`` of SNS, with its address."""
        if service == "s3" and name in ("Bucket", "Object"):
            return _Aws("sub", "s3", name)
        if service == "sqs" and name == "Queue":
            url = self.string(argument(call, self.source, 0, "url"), scope)
            return _Aws("sub", "sqs", "Queue", address_name("QueueUrl", url) if url else None)
        if service == "sqs" and name == "get_queue_by_name":
            return _Aws("sub", "sqs", "Queue",
                        self.string(keyword(call, self.source, "QueueName"), scope))
        if service == "dynamodb" and name == "Table":
            return _Aws("sub", "dynamodb", "Table")
        if service == "sns" and name == "Topic":
            arn = self.string(argument(call, self.source, 0, "arn"), scope)
            return _Aws("sub", "sns", "Topic", address_name("TopicArn", arn) if arn else None)
        return None


def _address_values(call: Node, source: bytes) -> Iterator[tuple[str, Node]]:
    """``(key, value node)`` for every address keyword of the call, and every address key of a
    dictionary in its arguments (EventBridge puts ``EventBusName`` on each entry)."""
    args = call.child_by_field_name("arguments")
    stack = list(args.named_children) if args is not None else []
    while stack:
        node = stack.pop()
        if node.type == "keyword_argument":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name is not None and value is not None:
                if node_text(name, source) in ADDRESS_KEYS:
                    yield node_text(name, source), value
                stack.append(value)
        elif node.type == "pair":
            key, value = node.child_by_field_name("key"), node.child_by_field_name("value")
            written = literal(key, source)
            if written in ADDRESS_KEYS and value is not None:
                yield written, value
            if value is not None:
                stack.append(value)
        elif node.type in ("list", "tuple", "dictionary"):
            stack.extend(node.named_children)


def _address(call: Node, bindings: _Bindings, scope: Node) -> tuple[str | None, str | None]:
    """(endpoint, address key present) of a client messaging call."""
    key_found: str | None = None
    names: set[str] = set()
    for key, value in _address_values(call, bindings.source):
        key_found = key if key in DIRECT_ADDRESS_KEYS else key_found or key
        written = bindings.string(value, scope)
        names.add(address_name(key, written) if written is not None else "")
    if len(names) != 1 or "" in names:
        return None, key_found  # absent, injected, or several different addresses
    return names.pop(), key_found


def _handler(call: Node, source: bytes) -> dict[str, object]:
    """``handler`` / ``handlerLine`` of the function the call is in: a ``lambda`` is anonymous
    (its line only); none for module-level code."""
    node = call.parent
    while node is not None and node.type not in _FUNCTIONS:
        node = node.parent
    if node is None:
        return {}
    if node.type == "lambda":
        return {"handlerLine": node.start_point[0] + 1}
    name = node.child_by_field_name("name")
    return {"handler": node_text(name, source), "handlerLine": node.start_point[0] + 1} \
        if name is not None else {}


def _classify(value: _Aws, method: str) -> SemanticType | None:
    if value.kind == "client":
        return "api_call" if value.service in _API_SERVICES \
            else _MESSAGING.get(value.service, {}).get(method)
    if value.service == "s3" and value.kind in ("resource", "sub"):
        return "api_call"
    if value.kind == "sub":
        return _RESOURCE_MESSAGING.get((value.service, value.sub), {}).get(method)
    return None  # SQS / SNS service resource: queue and topic management


def detect_sdk_calls(root: Node, source: bytes) -> list[Emit]:
    origins = Origins(root, source)
    if not origins.imports_package("boto3"):
        return []
    bindings = _Bindings(root, source, origins)
    found: list[Emit] = []
    for call in calls(root):
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "attribute":
            continue
        obj, name_node = fn.child_by_field_name("object"), fn.child_by_field_name("attribute")
        if obj is None or name_node is None:
            continue
        method = node_text(name_node, source)
        if method in _NOT_OPERATIONS or not method[:1].islower():
            continue
        scope = _scope_of(call)
        value = bindings.eval(obj, scope)
        if value is not None and value.service == "dynamodb":
            if method in _DYNAMODB_ACCESS.get((value.kind, value.sub), ()):
                found.append(Emit(
                    call, {"semanticType": "db_method_call", "method": method,
                           "dataAccessHint": "dynamodb"},
                    enrich="unclassified", reclassify=frozenset({"db_method_call", "query_statement"}),
                    text=first_line(node_text(call, source))[:120],
                ))
            continue
        semantic = _classify(value, method) if value is not None else None
        if value is None or semantic is None:
            continue
        framework = _SERVICES[value.service]
        fields: dict[str, object]
        fill: dict[str, object] = {}
        if semantic == "api_call":
            fields = {"semanticType": "api_call", "framework": framework, "endpoint": method}
        else:
            consumer = semantic == "eventbus_consumer"
            if value.kind == "sub":
                endpoint, key = value.address, None
            else:
                endpoint, key = _address(call, bindings, scope)
            if value.service == "sns" and key in DIRECT_ADDRESS_KEYS:
                semantic = "eventbus_send"  # an SMS reaches one recipient; nothing subscribes
            fields = {"semanticType": semantic, "framework": framework,
                      "method": "CONSUMER" if consumer else method}
            if consumer:
                fields.update(_handler(call, source))
            fill = {"endpoint": endpoint}
        found.append(Emit(
            call, fields, fill=fill, enrich="unclassified",
            text=first_line(node_text(call, source))[:120],
        ))
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = detect_sdk_calls(dc.root, dc.source)
    return Detection(found) if found else None


# Calls in test / fixture files go to a stub (moto, botocore Stubber), so those files are skipped.
register_detector(Detector(
    name="aws-sdk-calls", gate=Gate("python", order=80, guard=b"boto3", skip_fixtures=True),
    detect=_detect,
    frameworks=("aws-s3", "aws-ssm", "aws-cognito", "aws-lambda", "aws-sns", "aws-sqs",
                "aws-eventbridge"),
))
