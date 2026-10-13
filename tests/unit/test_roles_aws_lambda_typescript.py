"""AWS Lambda / messaging in TS / JS role table (docs/entry-point-roles/aws-lambda-typescript.md):
one test per row."""

from __future__ import annotations

EP = {"route", "eventbus_consumer", "timer"}
PKG = (
    '{"dependencies": {"@aws-sdk/client-sqs": "3"}, "devDependencies": {"@types/aws-lambda": "8"}}'
)


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def _run(analyze_files, src: str, **extra: str):
    return analyze_files({"package.json": PKG, "src/handler.ts": src, **extra})


def test_typed_event_handler_is_a_consumer(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { SQSHandler } from 'aws-lambda';\n"
        "export const onOrder: SQSHandler = async (event) => {\n  return;\n};\n",
    )
    [r] = _eps(st)
    assert (r.semanticType, r.framework, r.handler, r.endpoint) == (
        "eventbus_consumer",
        "aws-sqs",
        "onOrder",
        None,
    )


def test_typed_event_handler_has_handler_line(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { SQSHandler } from 'aws-lambda';\n"
        "export const onOrder: SQSHandler = async (event) => {\n  return;\n};\n",
    )
    [r] = _eps(st)
    assert r.handlerLine == 2


def test_event_parameter_handler_is_a_consumer(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { S3Event } from 'aws-lambda';\n"
        "export const handler = async (event: S3Event) => {\n  return;\n};\n",
    )
    [r] = _eps(st)
    assert (r.semanticType, r.framework, r.handler) == ("eventbus_consumer", "aws-s3", "handler")


def test_scheduled_handler_is_a_timer(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { ScheduledHandler } from 'aws-lambda';\n"
        "export const nightly: ScheduledHandler = async () => {\n  return;\n};\n",
    )
    [r] = _eps(st)
    assert (r.semanticType, r.framework, r.handler) == ("timer", "aws-eventbridge", "nightly")


def test_untyped_scheduled_event_handler_is_a_timer(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { ScheduledEvent } from 'aws-lambda';\n"
        "export const handler = async (event: ScheduledEvent) => {\n  return;\n};\n",
    )
    [r] = _eps(st)
    # The schedule is in deployment config, not the code: empty.
    assert (r.semanticType, r.method, r.endpoint, r.handler) == ("timer", "PERIODIC", None, "handler")


def test_api_gateway_handler_is_a_route(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { APIGatewayProxyHandler } from 'aws-lambda';\n"
        "export const getUser: APIGatewayProxyHandler = async (event) => {\n"
        "  return { statusCode: 200, body: '' };\n};\n",
    )
    [r] = _eps(st)
    assert (r.semanticType, r.framework, r.routeKind, r.handler) == (
        "route",
        "aws-apigw",
        "route",
        "getUser",
    )


def test_untyped_cjs_handler_is_a_binding(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/handler.js": "const AWS = require('aws-sdk');\nexports.handler = async () => {\n  return;\n};\n",
        }
    )
    [r] = _eps(st)
    assert (r.framework, r.handler) == ("aws-lambda", "handler")


def test_deployment_config_is_wiring(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { SQSHandler } from 'aws-lambda';\nexport const onOrder: SQSHandler = async (event) => {};\n",
        **{
            "serverless.yml": "service: shop\nfunctions:\n  onOrder:\n    handler: src/handler.onOrder\n"
            "    events:\n      - sqs: arn:aws:sqs:eu-west-1:123456789012:orders\n"
        },
    )
    [r] = _eps(st)
    assert r.endpoint == "orders"


def test_sqs_send_is_outbound(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { SQSClient, SendMessageCommand } from '@aws-sdk/client-sqs';\n"
        "const sqs = new SQSClient({});\n"
        "export async function enqueue() {\n"
        "  await sqs.send(new SendMessageCommand({ QueueUrl: 'orders', MessageBody: '' }));\n}\n",
    )
    assert _eps(st) == []
    assert [
        (s.semanticType, s.framework, s.endpoint) for s in st if s.semanticType == "eventbus_send"
    ] == [("eventbus_send", "aws-sqs", "orders")]


def test_sns_topic_publish_is_outbound(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';\n"
        "const sns = new SNSClient({});\n"
        "export async function notify() {\n"
        "  await sns.send(new PublishCommand({ TopicArn: 'arn:aws:sns:x:1:orders', Message: '' }));\n}\n",
    )
    assert [(s.semanticType, s.framework) for s in st if s.semanticType == "eventbus_publish"] == [
        ("eventbus_publish", "aws-sns")
    ]


def test_sms_publish_is_a_send(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';\n"
        "const sns = new SNSClient({});\n"
        "export async function sms() {\n"
        "  await sns.send(new PublishCommand({ PhoneNumber: '+10000000000', Message: '' }));\n}\n",
    )
    assert [
        (s.semanticType, s.framework)
        for s in st
        if s.semanticType and s.semanticType.startswith("eventbus")
    ] == [("eventbus_send", "aws-sns")]


HANDLERS = (
    "import { SQSHandler, APIGatewayProxyHandler } from 'aws-lambda';\n"
    "export const onOrder: SQSHandler = async (event) => {};\n"
    "export const getUser: APIGatewayProxyHandler = async (event) => ({ statusCode: 200, body: '' });\n"
    "export const plain = async (event) => {};\n"
)


def _deployed(analyze_files, functions: str):
    st = _run(analyze_files, HANDLERS, **{"serverless.yml": "service: shop\nfunctions:\n" + functions})
    return [(r.handler, r.semanticType, r.framework, r.method, r.endpoint) for r in _eps(st)]


def test_each_config_event_is_a_binding(analyze_files) -> None:
    got = _deployed(analyze_files, """  onOrder:
    handler: src/handler.onOrder
    events:
      - sqs: arn:aws:sqs:eu-west-1:1:orders
      - sqs:
          arn: arn:aws:sqs:eu-west-1:1:orders-retry
  getUser:
    handler: src/handler.getUser
    events:
      - http:
          path: users/{id}
          method: get
      - httpApi: 'POST /users'
""")
    assert got == [
        ("onOrder", "eventbus_consumer", "aws-sqs", None, "orders"),
        ("onOrder", "eventbus_consumer", "aws-sqs", None, "orders-retry"),
        ("getUser", "route", "aws-apigw", "GET", "/users/{id}"),
        ("getUser", "route", "aws-apigw", "POST", "/users"),
    ]


def test_config_types_an_untyped_handler(analyze_files) -> None:
    got = _deployed(analyze_files, """  plain:
    handler: src/handler.plain
    events:
      - schedule: rate(1 day)
      - sns: order-events
""")
    assert got == [
        ("onOrder", "eventbus_consumer", "aws-sqs", None, None),
        ("getUser", "route", "aws-apigw", None, None),
        ("plain", "timer", "aws-eventbridge", "PERIODIC", "rate(1 day)"),
        ("plain", "eventbus_consumer", "aws-sns", None, "order-events"),
    ]


def test_config_reference_leaves_the_address_empty(analyze_files) -> None:
    # A variable or a CloudFormation reference is resolved at deploy time: no address, never
    # its text. Two such queues cannot be told apart, so they are one record.
    got = _deployed(analyze_files, """  onOrder:
    handler: src/handler.onOrder
    events:
      - sqs: ${self:custom.queueArn}
      - sqs:
          arn: !GetAtt OrdersQueue.Arn
      - s3: uploads
""")
    assert got[0] == ("onOrder", "eventbus_consumer", "aws-sqs", None, None)
    assert len(got) == 2  # onOrder once (s3 adds nothing), getUser untouched


def test_config_naming_another_file_changes_nothing(analyze_files) -> None:
    # A handler in built output (dist/…) matches no source file.
    got = _deployed(analyze_files, """  onOrder:
    handler: dist/handler.onOrder
    events:
      - sqs: arn:aws:sqs:eu-west-1:1:orders
""")
    assert got == [("onOrder", "eventbus_consumer", "aws-sqs", None, None),
                   ("getUser", "route", "aws-apigw", None, None)]


SAM_HANDLERS = (
    "import { SQSHandler, APIGatewayProxyHandler } from 'aws-lambda';\n"
    "export const onOrder: SQSHandler = async (event) => {};\n"
    "export const api = async (event) => ({ statusCode: 200, body: '' });\n"
    "export const nightly = async () => {};\n"
)

SAM = """AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Globals:
  Function:
    CodeUri: functions/
    Runtime: nodejs20.x
Resources:
  OrderFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: app.onOrder
      Events:
        Orders:
          Type: SQS
          Properties:
            Queue: arn:aws:sqs:eu-west-1:1:orders
        Retry:
          Type: SQS
          Properties:
            Queue: !GetAtt RetryQueue.Arn
  ApiFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: app.api
      Events:
        Get:
          Type: Api
          Properties:
            Path: /users/{id}
            Method: get
        All:
          Type: HttpApi
  NightlyFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: app.nightly
      Events:
        Every:
          Type: Schedule
          Properties:
            Schedule: cron(0 2 * * ? *)
  Remote:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: s3://bucket/code.zip
      Handler: app.onOrder
"""


def test_sam_template_events_are_bindings(analyze_files) -> None:
    st = analyze_files({"package.json": PKG, "functions/app.ts": SAM_HANDLERS, "template.yaml": SAM})
    got = [(r.handler, r.semanticType, r.framework, r.method, r.endpoint) for r in _eps(st)]
    assert got == [
        ("onOrder", "eventbus_consumer", "aws-sqs", None, "orders"),
        ("onOrder", "eventbus_consumer", "aws-sqs", None, None),   # !GetAtt: not in the code
        ("api", "route", "aws-apigw", "GET", "/users/{id}"),
        ("api", "route", "aws-apigw", "ANY", None),                # HttpApi catch-all
        ("nightly", "timer", "aws-eventbridge", "PERIODIC", "cron(0 2 * * ? *)"),
    ]


SENDER = (
    "import { APIGatewayProxyHandler } from 'aws-lambda';\n"
    "const sender: APIGatewayProxyHandler = async (event) => ({ statusCode: 200, body: '' });\n"
    "export default sender;\n"
)
NAMED = (
    "import { APIGatewayProxyHandler } from 'aws-lambda';\n"
    "export const onOrder: APIGatewayProxyHandler = async (event) => ({ statusCode: 200, body: '' });\n"
)
CONFIG = "service: shop\nfunctions:\n  f:\n    handler: handler.{name}\n    events:\n      - http: 'POST /{name}'\n"


def _reexported(analyze_files, entry: str, name: str, **files: str):
    st = analyze_files({"package.json": PKG, "handler.ts": entry,
                        "serverless.yml": CONFIG.format(name=name), **files})
    return [(r.path, r.handler, r.method, r.endpoint) for r in _eps(st)]


def test_config_handler_followed_to_a_default_export(analyze_files) -> None:
    # export { default as sender } from './sqs/sender' — the handler is the target's default export
    assert _reexported(analyze_files, "export { default as sender } from './sqs/sender';\n",
                       "sender", **{"sqs/sender.ts": SENDER}) == [
        ("sqs/sender.ts", "sender", "POST", "/sender")]


def test_config_handler_followed_through_a_renamed_re_export_and_an_index(analyze_files) -> None:
    got = _reexported(analyze_files, "export { onOrder as order } from './api';\n", "order",
                      **{"api/index.ts": "export * from './orders';\n", "api/orders.ts": NAMED})
    assert got == [("api/orders.ts", "onOrder", "POST", "/order")]


def test_config_handler_followed_through_import_then_export(analyze_files) -> None:
    got = _reexported(analyze_files, "import { onOrder } from './api/orders';\nexport { onOrder };\n",
                      "onOrder", **{"api/orders.ts": NAMED})
    assert got == [("api/orders.ts", "onOrder", "POST", "/onOrder")]


def test_ambiguous_star_re_export_is_not_followed(analyze_files) -> None:
    # two `export *` targets both provide onOrder: ambiguous (TypeScript rejects it too)
    got = _reexported(analyze_files, "export * from './a';\nexport * from './b';\n", "onOrder",
                      **{"a.ts": NAMED, "b.ts": NAMED})
    assert [(p, e) for p, _, _, e in got] == [("a.ts", None), ("b.ts", None)]


def test_alias_re_export_is_not_followed(analyze_files) -> None:
    # only relative specifiers are followed, never a path alias
    got = _reexported(analyze_files, "export { onOrder } from '@api/orders';\n", "onOrder",
                      **{"api/orders.ts": NAMED,
                         "tsconfig.json": '{"compilerOptions": {"paths": {"@api/*": ["api/*"]}}}'})
    assert [(p, e) for p, _, _, e in got] == [("api/orders.ts", None)]


REF_HANDLERS = "".join(
    f"export const {n} = async (event) => {{}};\n" for n in ("q1", "q2", "q3", "q4", "t1", "s1")
)


def test_reference_to_a_resource_of_the_same_file_gives_its_literal_name(analyze_files) -> None:
    config = """service: shop
functions:
  q1: { handler: src/handler.q1, events: [ { sqs: { arn: { Fn::GetAtt: [Orders, Arn] } } } ] }
  q2: { handler: src/handler.q2, events: [ { sqs: { arn: { Fn::GetAtt: [Staged, Arn] } } } ] }
  q3: { handler: src/handler.q3, events: [ { sqs: { arn: { Fn::GetAtt: [Unnamed, Arn] } } } ] }
  q4: { handler: src/handler.q4, events: [ { sqs: { arn: { Fn::GetAtt: [Bucket, Arn] } } } ] }
  t1: { handler: src/handler.t1, events: [ { sns: { arn: { Ref: Alerts }, topicName: alerts } } ] }
  s1:
    handler: src/handler.s1
    events:
      - stream: { type: dynamodb, arn: { Fn::GetAtt: [Table, StreamArn] } }
resources:
  Resources:
    Orders: { Type: AWS::SQS::Queue, Properties: { QueueName: orders } }
    Staged: { Type: AWS::SQS::Queue, Properties: { QueueName: "${sls:stage}-jobs" } }
    Unnamed: { Type: AWS::SQS::Queue }
    Bucket: { Type: AWS::S3::Bucket, Properties: { BucketName: files } }
    Alerts: { Type: AWS::SNS::Topic }
    Table: { Type: AWS::DynamoDB::Table, Properties: { TableName: Orders } }
"""
    st = _run(analyze_files, REF_HANDLERS, **{"serverless.yml": config})
    got = {r.handler: (r.framework, r.endpoint) for r in _eps(st)}
    assert got == {
        "q1": ("aws-sqs", "orders"),
        "q2": ("aws-sqs", None),          # a variable: known at deploy time only
        "q3": ("aws-sqs", None),          # no name: AWS generates one
        "q4": ("aws-sqs", None),          # not a queue: never another resource's name
        "t1": ("aws-sns", "alerts"),      # topicName given beside the reference
        "s1": ("aws-dynamodb", "Orders"),
    }


def test_sam_reference_tags_are_followed_to_the_same_template(analyze_files) -> None:
    template = """Transform: AWS::Serverless-2016-10-31
Parameters:
  QueueArn: { Type: String }
Resources:
  Q1:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handler.q1
      Events: { E: { Type: SQS, Properties: { Queue: !GetAtt Orders.Arn } } }
  Q2:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handler.q2
      Events: { E: { Type: SQS, Properties: { Queue: !Ref QueueArn } } }
  T1:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handler.t1
      Events: { E: { Type: SNS, Properties: { Topic: !Ref Alerts } } }
  Orders: { Type: AWS::SQS::Queue, Properties: { QueueName: orders } }
  Alerts: { Type: AWS::SNS::Topic, Properties: { TopicName: alerts } }
"""
    st = _run(analyze_files, REF_HANDLERS, **{"template.yaml": template})
    got = {r.handler: r.endpoint for r in _eps(st)}
    # !Ref QueueArn is a template parameter, not a resource: unknown
    assert got == {"q1": "orders", "q2": None, "t1": "alerts"}
