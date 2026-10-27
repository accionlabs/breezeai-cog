"""Kafka for Go — segmentio/kafka-go, IBM (Shopify) sarama and confluent-kafka-go::

    r := kafka.NewReader(kafka.ReaderConfig{Topic: "orders"})
    m, _ := r.ReadMessage(ctx)                          → eventbus_consumer EVENT orders, handler = the enclosing function
    w := &kafka.Writer{Topic: "audit"}
    w.WriteMessages(ctx, kafka.Message{Value: v})       → eventbus_send audit
    group.Consume(ctx, []string{"orders"}, handler)     → eventbus_consumer orders (sarama), handler = handler
    producer.SendMessage(&sarama.ProducerMessage{Topic: "audit"})  → eventbus_send audit
    c.SubscribeTopics([]string{"orders"}, nil)          → eventbus_consumer orders (confluent)

A topic that is not a literal or a constant of the file — or a reader built where this function
can't see — leaves ``endpoint`` empty. ``EVENT`` is the method Spring's ``@KafkaListener`` uses."""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..go.imports import GoIndex
from ..go.routing import file_analyzer, string_value
from ..go.values import TypedCall, ValueSpec, handler_fill, typed_calls
from ..treesitter import node_text

SEGMENTIO = ValueSpec(
    import_path="github.com/segmentio/kafka-go",
    types=frozenset({"Reader", "Writer"}),
    constructors={"NewReader": "Reader", "NewWriter": "Writer"},
)
SARAMA = [ValueSpec(
    import_path=path,
    types=frozenset({"ConsumerGroup", "Consumer", "SyncProducer", "AsyncProducer"}),
    constructors={"NewConsumerGroup": "ConsumerGroup", "NewConsumerGroupFromClient": "ConsumerGroup",
                  "NewConsumer": "Consumer", "NewSyncProducer": "SyncProducer",
                  "NewSyncProducerFromClient": "SyncProducer", "NewAsyncProducer": "AsyncProducer"},
) for path in ("github.com/IBM/sarama", "github.com/Shopify/sarama")]
CONFLUENT = ValueSpec(
    import_path="github.com/confluentinc/confluent-kafka-go/kafka",
    types=frozenset({"Consumer", "Producer"}),
    constructors={"NewConsumer": "Consumer", "NewProducer": "Producer"},
)
CONFLUENT_V2 = ValueSpec(
    import_path="github.com/confluentinc/confluent-kafka-go/v2/kafka",
    types=CONFLUENT.types, constructors=CONFLUENT.constructors,
)


def _field(literal: Node | None, key: str, analyzer) -> Node | None:
    """``T{Key: value}`` / ``&T{…}`` / ``f(T{…})`` → the value node of ``Key``."""
    node = literal
    while node is not None and node.type in {"unary_expression", "call_expression"}:
        if node.type == "unary_expression":
            node = node.child_by_field_name("operand")
        else:
            args = node.child_by_field_name("arguments")
            node = args.named_children[0] if args is not None and args.named_children else None
    body = node.child_by_field_name("body") if node is not None and node.type == "composite_literal" else None
    for el in body.named_children if body is not None else []:
        kids = [k.named_children[0] if k.type == "literal_element" and k.named_children else k
                for k in el.named_children]
        if el.type == "keyed_element" and len(kids) == 2 and node_text(kids[0], analyzer.source) == key:
            return kids[1]
    return None


def _topics(node: Node | None, analyzer) -> list[str | None]:
    """``[]string{"a", "b"}`` / ``"a"`` → ``["a", "b"]``; unreadable → ``[None]``."""
    if node is not None and node.type == "composite_literal":
        body = node.child_by_field_name("body")
        out = [string_value(el.named_children[0] if el.type == "literal_element" and el.named_children else el,
                            analyzer.source, analyzer.consts)
               for el in (body.named_children if body is not None else [])]
        return out or [None]
    return [string_value(node, analyzer.source, analyzer.consts)]


def _emit(tc: TypedCall, semantic: str, topic: str | None, fill: dict | None = None) -> Emit:
    fields = {"semanticType": semantic, "framework": "kafka", "endpoint": topic}
    if semantic == "eventbus_consumer":
        fields["method"] = "EVENT"
    return Emit(tc.call, fields, fill=fill or {}, enrich="any")


#: The detector's own byte guard (three libraries, so no single ``Gate.guard``).
_MARKERS = (b"segmentio/kafka-go", b"/sarama", b"confluent-kafka-go")


def detect_kafka(dc: DetectContext) -> Detection | None:
    if not any(m in dc.source for m in _MARKERS):
        return None
    index = dc.ctx.resolution_index if isinstance(dc.ctx.resolution_index, GoIndex) else None
    a = file_analyzer(dc.root, dc.source, dc.path, index)
    found: list[Emit] = []
    for tc in typed_calls(a, SEGMENTIO):
        made = tc.value.made_by
        if tc.value.type == "Reader" and tc.method in {"ReadMessage", "FetchMessage"}:
            topic = string_value(_field(made, "Topic", a), a.source, a.consts)
            found.append(_emit(tc, "eventbus_consumer", topic, handler_fill(a, tc.call, None, tc.function)))
        elif tc.value.type == "Writer" and tc.method == "WriteMessages":
            topics = {string_value(_field(m, "Topic", a), a.source, a.consts) for m in tc.args[1:]} - {None}
            topic = string_value(_field(made, "Topic", a), a.source, a.consts)
            if topic is None and len(topics) == 1:
                topic = topics.pop()
            found.append(_emit(tc, "eventbus_send", topic))
    for spec in SARAMA:
        for tc in typed_calls(a, spec):
            if tc.value.type == "ConsumerGroup" and tc.method == "Consume" and len(tc.args) >= 2:
                handler = tc.args[2] if len(tc.args) > 2 else None
                fill = handler_fill(a, tc.call, handler, tc.function)
                found.extend(_emit(tc, "eventbus_consumer", t, fill) for t in _topics(tc.args[1], a))
            elif tc.value.type == "Consumer" and tc.method == "ConsumePartition" and tc.args:
                found.append(_emit(tc, "eventbus_consumer", _topics(tc.args[0], a)[0],
                                   handler_fill(a, tc.call, None, tc.function)))
            elif tc.value.type == "SyncProducer" and tc.method in {"SendMessage", "SendMessages"} and tc.args:
                topic = string_value(_field(tc.args[0], "Topic", a), a.source, a.consts)
                found.append(_emit(tc, "eventbus_send", topic))
    for spec in (CONFLUENT, CONFLUENT_V2):
        for tc in typed_calls(a, spec):
            if tc.value.type == "Consumer" and tc.method in {"Subscribe", "SubscribeTopics"} and tc.args:
                fill = handler_fill(a, tc.call, None, tc.function)
                found.extend(_emit(tc, "eventbus_consumer", t, fill) for t in _topics(tc.args[0], a))
            elif tc.value.type == "Producer" and tc.method == "Produce" and tc.args:
                partition = _field(tc.args[0], "TopicPartition", a)
                topic_ref = _field(partition, "Topic", a)
                if topic_ref is not None and topic_ref.type == "unary_expression":
                    topic_ref = topic_ref.child_by_field_name("operand")
                found.append(_emit(tc, "eventbus_send", string_value(topic_ref, a.source, a.consts)))
    return Detection(found, label="kafka") if found else None


register_detector(Detector(
    name="go-kafka", gate=Gate("go", order=90, skip_fixtures=True),
    detect=detect_kafka, frameworks=("kafka",),
))
