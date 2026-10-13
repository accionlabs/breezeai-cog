# AWS Lambda and messaging in TypeScript / JavaScript — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript/aws_events.py` (additive detector, every TS / JS file that touches the AWS SDK or Lambda types) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_aws_lambda_typescript.py`

A Lambda handler is a binding; its trigger is classified by the handler's type. The queue,
topic or HTTP path usually lives in deployment config (wiring), so `endpoint` stays empty
unless that config is read.

| Construct | Role | Record | Why |
|---|---|---|---|
| `const h: SQSHandler` / `SNSHandler` / `EventBridgeHandler` / `KinesisStreamHandler` / `DynamoDBStreamHandler` / `S3Handler` / … | Binding | `eventbus_consumer`, `framework` = the service (`aws-sqs`, `aws-sns`, …), `handler` = the name, `handlerLine` = its declaration | One handler per event source |
| Exported `handler` whose first parameter is an event type (`(event: SQSEvent) => …`) | Binding | as above | Same, untyped form |
| `const h: ScheduledHandler` / `(event: ScheduledEvent) => …` | Binding | `timer`, `framework: aws-eventbridge` | Scheduled trigger |
| `const h: APIGatewayProxyHandler` / `APIGatewayProxyHandlerV2` / `ALBHandler`, or an `APIGatewayProxyEvent` parameter | Binding | `route`, `framework: aws-apigw`, `routeKind: route` | HTTP trigger |
| Untyped `exports.handler = …` in a file using the AWS SDK | Binding | `framework: aws-lambda`, `handler` = the name | A Lambda entry; trigger unknown from the code |
| Function events in `serverless.yml` (`sqs: arn:…:orders`, `http: GET /users`, `schedule: rate(1 day)`) or a SAM template (`AWS::Serverless::Function` `Events`: `SQS`, `SNS`, `Kinesis`, `DynamoDB`, `Schedule`, `ScheduleV2`, `EventBridgeRule`, `Api`, `HttpApi`) | Wiring | — the handler (`handler: src/handler.onOrder` → export `onOrder` of `src/handler.*`) gets one record per event: `sqs` / `sns` / `stream` / `eventBridge` → `eventbus_consumer` with the queue / topic / stream / bus name from a literal ARN or name; `schedule` → `timer` with the expression; `http` / `httpApi` → `route` with verb and `/path`. A variable or reference (`${…}`, `!GetAtt`) → empty; an untyped export the config names is a handler too. SAM: `Handler` relative to `CodeUri` (or `Globals.Function`); a `CodeUri` on S3 → nothing; `HttpApi` without a path → `ANY`, empty path. CDK: not read | Deployment config, names no handler of its own |
| `sqs.sendMessage(...)` / `new SendMessageCommand(...)` | Outbound | `eventbus_send`, `framework: aws-sqs`, literal `QueueUrl` as `endpoint` | Our code sending a message |
| `sns.publish({ TopicArn })` / `new PublishCommand({ TopicArn })` / `PutEventsCommand` | Outbound | `eventbus_publish` | Our code publishing |
| `new PublishCommand({ PhoneNumber })` | Outbound | `eventbus_send`, `framework: aws-sns` | One recipient |
