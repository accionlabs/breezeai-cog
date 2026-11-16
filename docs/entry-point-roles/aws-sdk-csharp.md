# AWS SDK for .NET (C# and VB) — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_aws/sdk_calls.py` and `src/breezeai_cog/parsers/vb_aws/sdk_calls.py` (additive detectors) · Service tables and addresses: `src/breezeai_cog/parsers/aws_sdk_common.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_csharp_aws_sdk_calls.py`, `tests/unit/test_vb_aws_sdk_calls.py`

A call counts only on a name declared as an AWS client type (`IAmazonSQS`, `AmazonSQSClient`,
`IAmazonSimpleNotificationService`, …): a field, property, parameter, local or `new`, bound as
C# binds it where the call is — the member's locals and parameters, then the class's fields and
properties, its same-file base classes, then outer classes — so a same-named field of another
type in the next class, or a local that shadows the field, is not a client. The verb
alone proves nothing — `PublishAsync` is also MediatR's, `ReceiveMessageAsync` any chat client's.
A queue, topic or bus is written by name (`…amazonaws.com/123456789012/orders` → `orders`), as
the deploy config's consumers and the senders write it, from a literal or a `const` /
`static readonly` string (the `csharp.string-constants` fact); anything else leaves `endpoint`
empty. Mock and fixture files are skipped.

**VB** follows the same table with one SDK service table. A name binds where the call is — the
enclosing lambda, the member's parameters and every declaration in its body, then each
enclosing type's fields and properties (`vb/scopes.py`); names are case-insensitive, and a
declaration whose type can't be read (an untyped lambda parameter, a `For Each` without `As`)
hides the field rather than guessing. `Me._s3` is the field. The address comes from string
literals only — a VB `Const` leaves `endpoint` empty.

| Construct | Role | Record | Why |
|---|---|---|---|
| `sqs.ReceiveMessageAsync(new ReceiveMessageRequest { QueueUrl = … })` / `ReceiveMessageAsync(queueUrl)` (also sync `ReceiveMessage`) | Binding | `eventbus_consumer`, `CONSUMER`, `framework: aws-sqs`, `endpoint` = the queue, `handler` = the method the call is in | That method takes the messages off the queue — a worker's `ExecuteAsync` loop is the queue's entry point; as a polling read is for Go Kafka |
| A receive whose queue URL is set at run time (configuration, a parameter) | Binding | as above, `endpoint` empty | The queue is not visible in the code |
| A receive in top-level statements (`Program.cs`) | Binding | as above, no `handler` | There is no method to name |
| `sqs.SendMessageAsync(…)` / `SendMessageBatchAsync` | Outbound | `eventbus_send`, `method` = the SDK method, `endpoint` = the queue | |
| `sns.PublishAsync(…)` to a `TopicArn` / `PublishBatchAsync`; `events.PutEventsAsync(…)` | Outbound | `eventbus_publish` (`aws-sns` / `aws-eventbridge`), `endpoint` = the topic / bus | |
| `sns.PublishAsync(…)` to a `PhoneNumber` | Outbound | `eventbus_send` | One recipient; nothing subscribes |
| S3, Systems Manager, Cognito user-pool and Lambda client operations | Outbound | `api_call`, `framework` = the service, `endpoint` = the SDK method | |
| `DeleteMessageAsync`, `CreateTopicAsync`, `GetPreSignedURL`, `Dispose` … | None | — | Management and local calls, not traffic |
