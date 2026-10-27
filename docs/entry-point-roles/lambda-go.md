# AWS Lambda (Go) — entry-point roles

Parser: `src/breezeai_cog/parsers/go_lambda/` (additive detector, Go files importing `github.com/aws/aws-lambda-go/lambda`) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_lambda_go.py`

The same records as the [C#](lambda-csharp.md) and [TypeScript](aws-lambda-typescript.md) Lambda
detectors: the trigger comes from the handler's event type (`github.com/aws/aws-lambda-go/events`,
the handler's last parameter). `endpoint` stays empty — the queue, topic or path is in deployment
config, and a Go function's config names a compiled binary (`handler: bootstrap`), not the
function, so `serverless.yml` / SAM events are not linked to Go handlers.

| Construct | Role | Record | Why |
|---|---|---|---|
| `lambda.Start(h)` (also `StartWithOptions`, `StartHandler`, `StartHandlerFunc`, `StartWithContext`) with `h(ctx, e events.SQSEvent)` — `SQSEvent`, `SNSEvent`, `S3Event`, `DynamoDBEvent`, `KinesisEvent`, `KinesisFirehoseEvent`, `CloudWatchEvent`, `EventBridgeEvent`, `SimpleEmailEvent` | Binding | `eventbus_consumer`, `framework` = the trigger's service (`aws-sqs`, `aws-s3`, …), `handler` = `h`, located | One handler, started by an event |
| Same with `APIGatewayProxyRequest`, `APIGatewayV2HTTPRequest`, `ALBTargetGroupRequest`, `LambdaFunctionURLRequest` | Binding | `route`, `routeKind: route`, `framework: aws-apigw`, no `method` / `endpoint` | One handler, inbound HTTP |
| Any other event parameter (an authorizer request, a custom struct, none) | Binding | `eventbus_consumer`, `framework: aws-lambda` | A Lambda entry; trigger unknown from the code |
| `lambda.Start(func(ctx context.Context, e events.SNSEvent) error {…})` | Binding | as above, no `handler`; `handlerLine` = where the literal starts | Anonymous handler |
| `lambda.Start(wrap(handler))` where `handler` is a function of the file | Binding | `handler` = `handler`, trigger from its event type | The wrapper only forwards |
| `lambda.Start(newHandler(cfg))` | Binding | `eventbus_consumer`, `aws-lambda`, no handler | The handler is built at run time |
