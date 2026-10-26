# Kafka (Go) — entry-point roles

Parser: `src/breezeai_cog/parsers/go_kafka/` (additive detector, Go files importing segmentio/kafka-go, IBM or Shopify sarama, or confluent-kafka-go) · Typed values: `src/breezeai_cog/parsers/go/values.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_kafka_go.py`

Consumers use `method: EVENT`, as Spring's `@KafkaListener`. A value is a reader, writer,
consumer or producer when it is bound from the library's constructor or composite literal, or
declared with its type.

| Construct | Role | Record | Why |
|---|---|---|---|
| segmentio: `r.ReadMessage(ctx)` / `r.FetchMessage(ctx)` on `kafka.NewReader(kafka.ReaderConfig{Topic: …})` | Binding | `eventbus_consumer`, `EVENT`, `framework: kafka`, `endpoint` = the reader's topic, `handler` = the function the call is in | That function receives the messages |
| segmentio: a reader built in another function (a parameter or field) | Binding | `endpoint` empty | The topic is not visible here |
| segmentio: `w.WriteMessages(ctx, msgs…)` on `&kafka.Writer{Topic: …}` / `kafka.NewWriter(…)` | Outbound | `eventbus_send`, the writer's topic, or the messages' one `Topic` | Our code producing |
| sarama: `group.Consume(ctx, []string{…}, handler)` | Binding per topic | one `eventbus_consumer` per topic, `handler` = the handler argument | The handler's `ConsumeClaim` receives |
| sarama: `consumer.ConsumePartition(topic, …)` | Binding | `eventbus_consumer`, `handler` = the function the call is in | |
| sarama: `producer.SendMessage(&sarama.ProducerMessage{Topic: …})` | Outbound | `eventbus_send` | |
| confluent: `c.Subscribe(topic, nil)` / `c.SubscribeTopics([]string{…}, nil)` | Binding per topic | `eventbus_consumer`, `handler` = the function the call is in (the poll loop) | |
| confluent: `p.Produce(&kafka.Message{TopicPartition: kafka.TopicPartition{Topic: &topic}}, nil)` | Outbound | `eventbus_send`; `endpoint` when `topic` is a constant of the file | |
