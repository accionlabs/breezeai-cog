"""Kafka (Go) role table (docs/entry-point-roles/kafka-go.md): one test per row."""

from __future__ import annotations

MOD = "module example.com/app\n\ngo 1.22\n"


def _records(analyze_files, imp: str, body: str):
    st = analyze_files({"go.mod": MOD, "main.go": f'package main\n\nimport {imp}\n\n' + body})
    return {(s.semanticType, s.method, s.endpoint, s.handler) for s in st if s.framework == "kafka"}


SEG = '"github.com/segmentio/kafka-go"'
SARAMA = '"github.com/IBM/sarama"'
CONF = '"github.com/confluentinc/confluent-kafka-go/v2/kafka"'


def test_segmentio_reader(analyze_files) -> None:
    assert _records(analyze_files, SEG, 'func consume() {\n\tr := kafka.NewReader(kafka.ReaderConfig{Topic: "orders"})\n'
                                        '\tfor {\n\t\tm, _ := r.ReadMessage(ctx)\n\t\t_ = m\n\t}\n}\n') \
        == {("eventbus_consumer", "EVENT", "orders", "consume")}


def test_segmentio_reader_from_elsewhere_has_no_endpoint(analyze_files) -> None:
    assert _records(analyze_files, SEG, 'func consume(r *kafka.Reader) {\n\tr.FetchMessage(ctx)\n}\n') \
        == {("eventbus_consumer", "EVENT", None, "consume")}


def test_segmentio_writer(analyze_files) -> None:
    assert _records(analyze_files, SEG, 'func send() {\n\tw := &kafka.Writer{Topic: "audit"}\n'
                                        '\tw.WriteMessages(ctx, kafka.Message{Value: nil})\n'
                                        '\tw2 := kafka.NewWriter(kafka.WriterConfig{})\n'
                                        '\tw2.WriteMessages(ctx, kafka.Message{Topic: "billing"})\n}\n') \
        == {("eventbus_send", None, "audit", None), ("eventbus_send", None, "billing", None)}


def test_sarama_consumer_group(analyze_files) -> None:
    assert _records(analyze_files, SARAMA, 'func run(group sarama.ConsumerGroup) {\n'
                                           '\tgroup.Consume(ctx, []string{"orders", "refunds"}, handler)\n}\n') \
        == {("eventbus_consumer", "EVENT", "orders", "handler"), ("eventbus_consumer", "EVENT", "refunds", "handler")}


def test_sarama_partition_consumer(analyze_files) -> None:
    assert _records(analyze_files, SARAMA, 'func run(c sarama.Consumer) {\n\tc.ConsumePartition("orders", 0, 0)\n}\n') \
        == {("eventbus_consumer", "EVENT", "orders", "run")}


def test_sarama_producer(analyze_files) -> None:
    assert _records(analyze_files, SARAMA, 'func send() {\n\tp, _ := sarama.NewSyncProducer(nil, nil)\n'
                                           '\tp.SendMessage(&sarama.ProducerMessage{Topic: "audit"})\n}\n') \
        == {("eventbus_send", None, "audit", None)}


def test_confluent_subscribe(analyze_files) -> None:
    assert _records(analyze_files, CONF, 'func poll() {\n\tc, _ := kafka.NewConsumer(nil)\n'
                                         '\tc.SubscribeTopics([]string{"orders"}, nil)\n}\n') \
        == {("eventbus_consumer", "EVENT", "orders", "poll")}


def test_confluent_produce(analyze_files) -> None:
    assert _records(analyze_files, CONF, 'const topic = "audit"\n\nfunc send() {\n\tp, _ := kafka.NewProducer(nil)\n'
                                         '\tp.Produce(&kafka.Message{TopicPartition: kafka.TopicPartition{Topic: &topic}}, nil)\n}\n') \
        == {("eventbus_send", None, "audit", None)}
