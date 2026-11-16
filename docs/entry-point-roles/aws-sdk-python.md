# AWS SDK for Python (boto3) — entry-point roles

Parser: `src/breezeai_cog/parsers/python_aws/sdk_calls.py` (additive detector) · Addresses: `src/breezeai_cog/parsers/aws_sdk_common.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_python_aws_sdk_calls.py`

A call counts only on a receiver bound to a boto3 client or resource, and only in a file that
imports `boto3`. The verb alone proves nothing: `publish` is also Redis's, and `send_message`
belongs to any chat client. The receiver can be:

- a local, a module variable, or `self.<attr>` (assigned in the class's methods or body), or the
  factory call itself;
- bound to `boto3.client("<service>")`, `boto3.resource("<service>")`, or `.client(…)` /
  `.resource(…)` of a `boto3.Session` (constructed, or a parameter annotated `Session`);
- for resources, an S3 `Bucket` / `Object`, an SQS `Queue(url)` / `get_queue_by_name(…)`, or an
  SNS `Topic(arn)`, followed from the resource.

A name bound in its scope to anything else is not resolved, even once: a parameter, a loop
variable, or a second value.

A queue, topic or bus is written by name (`…amazonaws.com/123456789012/orders` → `orders`), as
the consumers and senders in other languages write it. The name comes only from a literal or a
module constant. It can be in a keyword argument, a dictionary key (EventBridge `Entries`) or
the URL / ARN a resource was created with. Anything else leaves `endpoint` empty.

Mock and fixture files are skipped.

DynamoDB is data access, not an entry point, and is recorded as the C# AWS detector records
it: `db_method_call`, `method` = the operation, `dataAccessHint: dynamodb`. That covers item and
query operations on the client (`put_item`, `query`, `transact_write_items`,
`execute_statement`, …) and on a resource `Table`, plus the resource's `batch_get_item` /
`batch_write_item`. Table management (`create_table`, `describe_table`) is not recorded.

| Construct | Role | Record | Why |
|---|---|---|---|
| `sqs.receive_message(QueueUrl=…)` / `Queue.receive_messages()` | Binding | `eventbus_consumer`, `CONSUMER`, `framework: aws-sqs`, `endpoint` = the queue, `handler` = the function the call is in | That function takes the messages off the queue. A worker's polling loop is the queue's entry point, as in the C# and Go Kafka detectors |
| A receive whose queue URL is set at run time (a parameter, configuration) | Binding | as above, `endpoint` empty | The queue is not visible in the code |
| A receive inside a `lambda` | Binding | as above, no `handler`, `handlerLine` = the lambda's line | An inline handler is anonymous |
| A receive in module-level code | Binding | as above, no `handler` | There is no function to name |
| `sqs.send_message(…)` / `send_message_batch`, `Queue.send_message(s)` | Outbound | `eventbus_send`, `method` = the boto3 method, `endpoint` = the queue | |
| `sns.publish(TopicArn=…)` / `publish_batch`, `Topic.publish(…)`; `events.put_events(…)` | Outbound | `eventbus_publish` (`aws-sns` / `aws-eventbridge`), `endpoint` = the topic / bus | |
| `sns.publish(PhoneNumber=…)` | Outbound | `eventbus_send` | One recipient; nothing subscribes |
| S3 (client, resource, `Bucket`, `Object`, `meta.client`), Systems Manager, Cognito user-pool (`cognito-idp`) and Lambda client operations | Outbound | `api_call`, `framework` = the service, `endpoint` = the boto3 method | |
| `generate_presigned_url`, `get_paginator`, `get_waiter`, `close`; `create_queue`, `delete_message`, `create_topic` … | None | — | Local calls and management, not traffic |
