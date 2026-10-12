"""AWS Lambda / messaging in TS / JS role table (docs/entry-point-roles/aws-lambda-typescript.md):
one test per row."""

from __future__ import annotations

import pytest

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


@pytest.mark.xfail(strict=True, reason="Lambda handler records have no handlerLine")
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


@pytest.mark.xfail(
    strict=True, reason="scheduled Lambda handlers are recorded as eventbus_consumer, not timer"
)
def test_scheduled_handler_is_a_timer(analyze_files) -> None:
    st = _run(
        analyze_files,
        "import { ScheduledHandler } from 'aws-lambda';\n"
        "export const nightly: ScheduledHandler = async () => {\n  return;\n};\n",
    )
    [r] = _eps(st)
    assert (r.semanticType, r.framework, r.handler) == ("timer", "aws-eventbridge", "nightly")


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


@pytest.mark.xfail(
    strict=True, reason="queue names and paths in serverless.yml are not copied to the handler"
)
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
