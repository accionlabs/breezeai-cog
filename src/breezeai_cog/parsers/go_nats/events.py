"""NATS (``github.com/nats-io/nats.go``), core and JetStream::

    nc, _ := nats.Connect(url)
    nc.Subscribe("orders.created", onOrder)        → eventbus_consumer MESSAGE orders.created, handler onOrder
    nc.QueueSubscribe("orders.*", "workers", h)    → eventbus_consumer MESSAGE orders.*
    sub, _ := nc.SubscribeSync("audit")            → eventbus_consumer, handler = the enclosing function
    nc.Publish("orders.created", data)             → eventbus_publish orders.created
    nc.Request("pricing.quote", data, time.Second) → eventbus_send pricing.quote
    js, _ := nc.JetStream(); js.Subscribe(…) / js.Publish(…)  → the same

A subject that is not a literal or a constant of the file leaves ``endpoint`` empty."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..go.imports import GoIndex
from ..go.routing import file_analyzer, string_value
from ..go.values import ValueSpec, handler_fill, typed_calls

SPEC = ValueSpec(
    import_path="github.com/nats-io/nats.go",
    types=frozenset({"Conn", "EncodedConn", "JetStreamContext", "JetStream"}),
    constructors={"Connect": "Conn", "NewEncodedConn": "EncodedConn"},
    derived={("Conn", "JetStream"): "JetStreamContext"},
)

#: method → (handler argument index or None)
_CONSUMERS = {"Subscribe": 1, "QueueSubscribe": 2, "ChanSubscribe": None, "ChanQueueSubscribe": None,
              "SubscribeSync": None, "QueueSubscribeSync": None, "PullSubscribe": None,
              "BindStream": None}
_PUBLISH = {"Publish": "eventbus_publish", "PublishMsg": "eventbus_publish",
            "PublishAsync": "eventbus_publish", "Request": "eventbus_send",
            "RequestMsg": "eventbus_send"}


def _subject(args, analyzer, method: str) -> str | None:
    if not args:
        return None
    if method.endswith("Msg"):  # PublishMsg(&nats.Msg{Subject: "x"})
        node = args[0]
        node = node.child_by_field_name("operand") if node.type == "unary_expression" else node
        body = node.child_by_field_name("body") if node is not None and node.type == "composite_literal" else None
        for el in body.named_children if body is not None else []:
            kids = [k.named_children[0] if k.type == "literal_element" and k.named_children else k
                    for k in el.named_children]
            if len(kids) == 2 and kids[0].text == b"Subject":
                return string_value(kids[1], analyzer.source, analyzer.consts)
        return None
    return string_value(args[0], analyzer.source, analyzer.consts)


def detect_nats(dc: DetectContext) -> Detection | None:
    index = dc.ctx.resolution_index if isinstance(dc.ctx.resolution_index, GoIndex) else None
    analyzer = file_analyzer(dc.root, dc.source, dc.path, index)
    found: list[Emit] = []
    for tc in typed_calls(analyzer, SPEC):
        if tc.method in _CONSUMERS:
            idx = _CONSUMERS[tc.method]
            handler = tc.args[idx] if idx is not None and len(tc.args) > idx else None
            fill = handler_fill(analyzer, tc.call, handler, tc.function)
            found.append(Emit(tc.call, {"semanticType": "eventbus_consumer", "framework": "nats",
                                        "method": "MESSAGE",
                                        "endpoint": _subject(tc.args, analyzer, tc.method)},
                              fill=fill, enrich="any"))
        elif tc.method in _PUBLISH:
            found.append(Emit(tc.call, {"semanticType": _PUBLISH[tc.method], "framework": "nats",
                                        "endpoint": _subject(tc.args, analyzer, tc.method)},
                              enrich="any"))
    return Detection(found, label="nats") if found else None


register_detector(Detector(
    name="go-nats", gate=Gate("go", order=80, guard=b"nats-io/nats.go", skip_fixtures=True),
    detect=detect_nats, frameworks=("nats",),
))
