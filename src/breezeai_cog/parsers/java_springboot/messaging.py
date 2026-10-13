"""Spring messaging: listener annotations (inbound) and template sends (outbound).

**Bindings** — a method annotated with a broker listener is the handler of the messages on
that topic or queue: one ``eventbus_consumer`` record per address.

  @KafkaListener(topics = "orders")        → eventbus_consumer  kafka    EVENT    orders
  @RabbitListener(queues = "billing")      → eventbus_consumer  rabbitmq MESSAGE  billing
  @JmsListener(destination = "audit")      → eventbus_consumer  jms      MESSAGE  audit
  @SqsListener("jobs")                     → eventbus_consumer  aws-sqs  MESSAGE  jobs

A class-level ``@KafkaListener`` / ``@RabbitListener`` with ``@KafkaHandler`` /
``@RabbitHandler`` methods: each handler method is a binding on the class's addresses.

**Outbound** — a send through a broker template whose declared type is known:

  kafkaTemplate.send("orders", …)               → eventbus_send kafka    orders
  rabbitTemplate.convertAndSend("billing", m)   → eventbus_send rabbitmq billing
  rabbitTemplate.convertAndSend("ex", "rk", m)  → eventbus_send rabbitmq ex (the exchange;
                                                   the default exchange "" → the routing key)
  jmsTemplate.convertAndSend("audit", m)        → eventbus_send jms      audit
  sqsTemplate.send("jobs", m)                   → eventbus_send aws-sqs  jobs

**Addresses** are the literal, a ``static final`` constant (this file or ``Class.FIELD``
through the repo index), or a ``${property}`` placeholder resolved from the module's base
config. Anything else — a ``topicPattern``, a runtime variable, a SpEL ``#{…}``, a template's
default destination — leaves ``endpoint`` empty, on the listener and the sender alike (the
contract: an address is resolved or empty, never a code name).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Function, Statement
from ..emits import Emit
from ..enclosing import Enclosing
from ..java.config import resolve_placeholders
from ..java.constants import fold_arg
from ..treesitter import first_line, node_text

#: listener annotation → (framework, method, address attributes; "" = the positional value)
_LISTENERS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "KafkaListener": ("kafka", "EVENT", ("topics",)),
    "RabbitListener": ("rabbitmq", "MESSAGE", ("queues", "")),
    "JmsListener": ("jms", "MESSAGE", ("destination",)),
    "SqsListener": ("aws-sqs", "MESSAGE", ("", "value", "queueNames")),
}
#: class-level listener annotation → the method annotation that marks its handlers
_CLASS_HANDLERS = {"KafkaListener": "KafkaHandler", "RabbitListener": "RabbitHandler"}

#: template type (simple name) → (framework, send methods)
_TEMPLATES: dict[str, tuple[str, frozenset[str]]] = {
    "KafkaTemplate": ("kafka", frozenset({"send", "sendDefault"})),
    "KafkaOperations": ("kafka", frozenset({"send", "sendDefault"})),
    "RabbitTemplate": ("rabbitmq", frozenset({"convertAndSend", "send", "convertSendAndReceive",
                                              "sendAndReceive"})),
    "AmqpTemplate": ("rabbitmq", frozenset({"convertAndSend", "send", "convertSendAndReceive",
                                            "sendAndReceive"})),
    "RabbitOperations": ("rabbitmq", frozenset({"convertAndSend", "send",
                                                "convertSendAndReceive", "sendAndReceive"})),
    "JmsTemplate": ("jms", frozenset({"convertAndSend", "send", "convertSendAndReceive",
                                      "sendAndReceive"})),
    "JmsOperations": ("jms", frozenset({"convertAndSend", "send"})),
    "JmsMessagingTemplate": ("jms", frozenset({"convertAndSend", "send",
                                               "convertSendAndReceive"})),
    "SqsTemplate": ("aws-sqs", frozenset({"send", "sendAsync"})),
    "SqsOperations": ("aws-sqs", frozenset({"send", "sendAsync"})),
    "QueueMessagingTemplate": ("aws-sqs", frozenset({"convertAndSend", "send"})),
}


def has_messaging(source: bytes) -> bool:
    """Cheap gate: the file names a listener annotation or a broker template."""
    return any(n.encode() in source for n in (*_LISTENERS, *_TEMPLATES))


# ── addresses ───────────────────────────────────────────────────────────────────────────────

def _address(node: Node, source: bytes, consts: dict[str, str],
             props: dict[str, str | None]) -> str | None:
    """A literal / constant / ``${property}`` address → its string, else None."""
    value = fold_arg(node, source, consts)
    if value is None:
        return None
    return resolve_placeholders(value, props) if ("${" in value or "#{" in value) else value


def _element_values(node: Node) -> list[Node]:
    """``"a"`` → [a]; ``{"a", "b"}`` → [a, b]."""
    if node.type == "element_value_array_initializer":
        return list(node.named_children)
    return [node]


def _annotation_addresses(
    ann: Node, source: bytes, attrs: tuple[str, ...], consts: dict[str, str],
    props: dict[str, str | None],
) -> list[str | None]:
    """The addresses an annotation declares — one entry per topic / queue; ``[None]`` when it
    declares none that resolves (``topicPattern``, a runtime expression)."""
    args = ann.child_by_field_name("arguments")
    kids = list(args.named_children) if args is not None else []
    values: list[Node] = []
    for kid in kids:
        if kid.type == "element_value_pair":
            key = kid.child_by_field_name("key")
            value = kid.child_by_field_name("value")
            if key is not None and value is not None and node_text(key, source) in attrs:
                values += _element_values(value)
        elif "" in attrs:  # a single positional value
            values += _element_values(kid)
    out = [_address(v, source, consts, props) for v in values]
    return out or [None]


def _annotations(decl: Node) -> list[Node]:
    mods = next((c for c in decl.named_children if c.type == "modifiers"), None)
    return [a for a in (mods.named_children if mods is not None else [])
            if a.type in ("annotation", "marker_annotation")]


def _ann_name(ann: Node, source: bytes) -> str:
    name = ann.child_by_field_name("name")
    return node_text(name, source).rsplit(".", 1)[-1] if name is not None else ""


# ── listeners ───────────────────────────────────────────────────────────────────────────────

def _classes(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "class_declaration":
                out.append(c)
            walk(c)

    walk(root)
    return out


def detect_spring_listeners(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str],
    props: dict[str, str | None],
) -> list[Emit]:
    """One ``eventbus_consumer`` per listener method and address, for :func:`~..emits.apply`."""
    functions: dict[tuple[str, int], Function] = {(f.name, f.startLine): f for f in record.functions}
    out: list[Emit] = []

    def emit(fn: Function, ann: Node, framework: str, method: str,
             addresses: list[str | None]) -> None:
        for address in addresses:
            out.append(Emit(ann, dict(
                semanticType="eventbus_consumer",
                method=method,
                endpoint=address,
                framework=framework,
                handler=fn.name,
                handlerLine=fn.startLine,
                routeKind="message",
            ), parent=fn.id, node_type="synthetic",
                text=first_line(node_text(ann, source))[:200],
                line=fn.startLine, col=0, end_line=fn.endLine))

    for cls in _classes(root):
        body = cls.child_by_field_name("body")
        if body is None:
            continue
        # class-level listener → its @KafkaHandler / @RabbitHandler methods
        class_level: tuple[Node, str, str, list[str | None], str] | None = None
        for ann in _annotations(cls):
            name = _ann_name(ann, source)
            if name in _CLASS_HANDLERS:
                fw, method, attrs = _LISTENERS[name]
                class_level = (ann, fw, method,
                               _annotation_addresses(ann, source, attrs, consts, props),
                               _CLASS_HANDLERS[name])
        for member in body.named_children:
            if member.type != "method_declaration":
                continue
            name_node = member.child_by_field_name("name")
            fn = functions.get((node_text(name_node, source), member.start_point[0] + 1)) \
                if name_node is not None else None
            if fn is None:
                continue
            for ann in _annotations(member):
                name = _ann_name(ann, source)
                if name in _LISTENERS:
                    fw, method, attrs = _LISTENERS[name]
                    emit(fn, ann, fw, method,
                         _annotation_addresses(ann, source, attrs, consts, props))
                elif class_level is not None and name == class_level[4]:
                    c_ann, fw, method, addresses, _ = class_level
                    emit(fn, c_ann, fw, method, addresses)
    return out


# ── sends ───────────────────────────────────────────────────────────────────────────────────

def _base_type(type_node: Node | None, source: bytes) -> str:
    if type_node is None:
        return ""
    return node_text(type_node, source).split("<", 1)[0].rsplit(".", 1)[-1].strip()


def _template_vars(root: Node, source: bytes) -> dict[str, str]:
    """Variable / field / parameter name → template type, for names declared once in the file
    with a broker template type (a name declared with two types is dropped: unsure)."""
    out: dict[str, str | None] = {}

    def walk(n: Node) -> None:
        if n.type in ("field_declaration", "local_variable_declaration"):
            typ = _base_type(n.child_by_field_name("type"), source)
            for d in n.named_children:
                if d.type == "variable_declarator":
                    nm = d.child_by_field_name("name")
                    if nm is not None:
                        key = node_text(nm, source)
                        out[key] = typ if out.get(key, typ) == typ else None
        elif n.type == "formal_parameter":
            typ = _base_type(n.child_by_field_name("type"), source)
            nm = n.child_by_field_name("name")
            if nm is not None:
                key = node_text(nm, source)
                out[key] = typ if out.get(key, typ) == typ else None
        for c in n.named_children:
            walk(c)

    walk(root)
    return {k: v for k, v in out.items() if v in _TEMPLATES}


def _send_address(method: str, args: list[Node], source: bytes, consts: dict[str, str],
                  props: dict[str, str | None], framework: str) -> str | None:
    """The address a send call targets, or None (template default, runtime value, unsure)."""
    if method == "sendDefault" or len(args) < 2:  # the template's default destination
        return None
    if framework == "rabbitmq" and len(args) >= 3 and method in ("convertAndSend", "send",
                                                                 "convertSendAndReceive",
                                                                 "sendAndReceive"):
        exchange = _address(args[0], source, consts, props)
        if exchange == "":  # the default exchange routes by queue name
            return _address(args[1], source, consts, props)
        return exchange
    return _address(args[0], source, consts, props)


def detect_spring_sends(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str],
    props: dict[str, str | None],
) -> bool:
    """Enrich (or add) an ``eventbus_send`` statement for each broker-template send."""
    templates = _template_vars(root, source)
    if not templates:
        return False
    enclosing = Enclosing(record)
    fid = file_id(path)
    seen = {s.id for s in record.statements}
    matched = False

    def walk(n: Node) -> None:
        nonlocal matched
        if n.type == "method_invocation":
            obj = n.child_by_field_name("object")
            name = n.child_by_field_name("name")
            receiver = node_text(obj, source).removeprefix("this.") if obj is not None else ""
            typ = templates.get(receiver)
            method = node_text(name, source) if name is not None else ""
            if typ is not None and method in _TEMPLATES[typ][1]:
                framework = _TEMPLATES[typ][0]
                arg_list = n.child_by_field_name("arguments")
                args = list(arg_list.named_children) if arg_list is not None else []
                endpoint = _send_address(method, args, source, consts, props, framework)
                line = n.start_point[0] + 1
                stmt = enclosing.statement(line)
                # the call's own statement (not an enclosing block) → enrich in place
                if stmt is not None and stmt.semanticType is None and stmt.startLine == line:
                    stmt.semanticType = "eventbus_send"
                    stmt.framework = framework
                    stmt.endpoint = endpoint
                else:
                    new_id = disambiguate(statement_id(path, line, n.start_point[1]), seen)
                    record.statements.append(Statement(
                        id=new_id,
                        parentId=enclosing.function_id(line, fid),
                        nodeType=n.type,
                        semanticType="eventbus_send",
                        text=first_line(node_text(n, source)),
                        endpoint=endpoint,
                        framework=framework,
                        startLine=line,
                        endLine=n.end_point[0] + 1,
                        path=path,
                    ))
                matched = True
        for c in n.named_children:
            walk(c)

    walk(root)
    return matched
