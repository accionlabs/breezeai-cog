# AWS Lambda (C#) — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp/lambda_events.py` (additive detector) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_lambda_csharp.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| Handler method taking an event-source type (`SQSEvent`, `SNSEvent`, `S3Event`, `DynamoDBEvent`, `KinesisEvent`, `CloudWatchEvent`, `SimpleEmailEvent`) and `ILambdaContext` | Binding | `eventbus_consumer`, `framework` = the trigger's service (`aws-sqs`, `aws-s3`, …), `endpoint` = the queue / topic / bucket from deployment config, else empty, `handler` = the method | One handler, started by an event |
| Handler taking `APIGatewayProxyRequest` / `APIGatewayHttpApiV2ProxyRequest` / `ApplicationLoadBalancerRequest` | Binding | `route`, `routeKind: route`, `framework: aws-apigw`, path from deployment config, else empty | One handler, inbound HTTP |
| Handler taking `ScheduledEvent` | Binding | `timer`, schedule from deployment config, else empty | One handler, started by a schedule |
| Deployment config trigger (`serverless.template`, `serverless.yml`, SAM / CDK event source) | Wiring | — the handler named `Assembly::Namespace.Type::Method` gets one record per event, as for TypeScript (see aws-lambda-typescript.md); a method the config names is a handler even without `ILambdaContext`. Read: `serverless.yml`, SAM templates (`template.yaml`, JSON `serverless.template`); CDK not | Gives the handler its address; no handler of its own |
| Method taking `ILambdaContext` without an event type | None | — | A helper the handler calls |
