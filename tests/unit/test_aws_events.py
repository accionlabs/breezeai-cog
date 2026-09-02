"""AWS messaging / Lambda event detection (TypeScript): SNS/SQS/EventBridge producers
and Lambda consumers, reusing the ``eventbus_*`` semantics with the AWS transport on
``framework``. Gated by --capture-statements; additive on top of base/NestJS extraction."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.parsers.typescript_nestjs.parser import NestJSParser
from breezeai_cog.emit import to_line
from breezeai_cog.schemas import FileRecord

# Producer shapes (SDK v3 command + v2 method).
PRODUCER = b'''
import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';
import { SQS } from 'aws-sdk';

export class Dispatch {
  private snsClient: SNSClient;
  private sqs: SQS;
  private topicArn: string;

  async publish(message: unknown): Promise<void> {
    const result = await this.snsClient.send(
      new PublishCommand({ TopicArn: this.topicArn, Message: JSON.stringify(message) }));
    await this.sqs.sendMessageBatch({ QueueUrl: 'https://sqs/queue', Entries: [] });
    await this.sqs.sendMessage({ QueueUrl: this.queueUrl });
  }
}
'''

# Consumer + HTTP-entry shapes.
CONSUMER = b'''
import { SQSEvent, SQSHandler, APIGatewayProxyHandlerV2 } from 'aws-lambda';

export const handler: SQSHandler = async (event: SQSEvent) => {
  for (const record of event.Records) { process(record.body); }
};

export const ingest: APIGatewayProxyHandlerV2 = async (e) => { return { statusCode: 200 }; };
'''


def _parse(tmp_path, rel: str, src: bytes, parser=None, capture=True) -> FileRecord:
    p = tmp_path / rel
    p.write_bytes(src)
    ctx = ParseContext(path=rel, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return (parser or TypeScriptParser()).parse_file(ctx)


def _by_semantic(rec: FileRecord) -> dict[str, list]:
    out: dict[str, list] = {}
    for s in rec.statements:
        if s.semanticType:
            out.setdefault(s.semanticType, []).append(s)
    return out


def test_producers_detected(tmp_path) -> None:
    rec = _parse(tmp_path, "dispatch.service.ts", PRODUCER)
    sem = _by_semantic(rec)
    assert "eventbus_publish" in sem and "eventbus_send" in sem

    pub = sem["eventbus_publish"][0]
    assert pub.framework == "aws-sns"
    assert pub.method == "PublishCommand"
    # TopicArn is a symbol (this.topicArn) → honest null, never the symbol text.
    assert pub.endpoint is None

    sends = {(s.framework, s.endpoint) for s in sem["eventbus_send"]}
    assert ("aws-sqs", "https://sqs/queue") in sends  # string literal QueueUrl resolved
    assert ("aws-sqs", None) in sends                 # symbol QueueUrl → honest null


def test_consumers_and_route_entry(tmp_path) -> None:
    rec = _parse(tmp_path, "main.ts", CONSUMER)
    sem = _by_semantic(rec)
    assert "eventbus_consumer" in sem
    consumer = sem["eventbus_consumer"][0]
    assert consumer.framework == "aws-sqs"
    assert consumer.handler == "handler"
    # APIGateway handler is a route, NOT an event consumer.
    routes = {(s.framework, s.routeKind, s.handler) for s in sem["route"]}
    assert ("aws-apigw", "route", "ingest") in routes
    assert rec.framework == "aws-lambda"


# Untyped handler shapes — the dominant real-world form: no `: SQSHandler` annotation, the AWS
# event type is on the handler's first PARAMETER (`export const handler = async (e: S3Event) => …`).
UNTYPED = b'''
import { S3Event, Context } from 'aws-lambda';

export const handler = async (event: S3Event, context: Context): Promise<void> => {
  for (const record of event.Records) { await ingest(record); }
};
'''

# CommonJS export + EventBridge, plus a CloudFront handler type not previously in the maps.
UNTYPED_CJS = b'''
import { EventBridgeEvent } from 'aws-lambda';
exports.handler = async (event: EventBridgeEvent<'trigger', unknown>) => { await run(event); };
export const edge: CloudFrontRequestHandler = async (e) => { return e.Records[0].cf.request; };
'''


def test_untyped_handler_detected_by_event_param(tmp_path) -> None:
    rec = _parse(tmp_path, "index.ts", UNTYPED)
    sem = _by_semantic(rec)
    assert "eventbus_consumer" in sem
    c = sem["eventbus_consumer"][0]
    assert c.framework == "aws-s3"      # from the S3Event parameter type
    assert c.handler == "handler"
    assert rec.framework == "aws-lambda"


def test_untyped_handler_cjs_and_cloudfront(tmp_path) -> None:
    rec = _parse(tmp_path, "index.ts", UNTYPED_CJS)
    frameworks = {(s.framework, s.handler) for s in _by_semantic(rec).get("eventbus_consumer", [])}
    assert ("aws-eventbridge", "handler") in frameworks   # exports.handler = async (e: EventBridgeEvent)
    assert ("aws-cloudfront", "edge") in frameworks       # newly-mapped CloudFrontRequestHandler type


def test_untyped_handler_requires_aws_event_param(tmp_path) -> None:
    # An ordinary exported `handler` whose param is NOT an AWS event type must NOT match
    # (precision gate: the AWS event parameter is what distinguishes a Lambda entry point).
    src = b"export const handler = async (event: MyDomainEvent) => { doThing(event); };\n"
    rec = _parse(tmp_path, "not-lambda.ts", src)
    assert [s for s in rec.statements if s.semanticType] == []


def test_generic_publish_not_misdetected(tmp_path) -> None:
    # A GraphQL/pubsub .publish(name, payload) with no AWS SDK import must not match.
    src = b"export class S { emit() { this.pubsub.publish('topic', payload); } }\n"
    rec = _parse(tmp_path, "pubsub.service.ts", src)
    assert [s for s in rec.statements if s.semanticType] == []


def test_requires_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, "dispatch.service.ts", PRODUCER, capture=False)
    assert [s for s in rec.statements if s.semanticType] == []


def test_additive_under_nestjs(tmp_path) -> None:
    # A NestJS @Injectable that publishes to SNS: NestJS parser still owns the file,
    # and AWS detection layers on (producer captured).
    src = b'''
import { Injectable } from '@nestjs/common';
import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';

@Injectable()
export class Notifier {
  async send(): Promise<void> {
    await this.snsClient.send(new PublishCommand({ TopicArn: 'arn:aws:sns:x', Message: 'm' }));
  }
}
'''
    rec = _parse(tmp_path, "notifier.service.ts", src, parser=NestJSParser())
    sem = _by_semantic(rec)
    assert sem["eventbus_publish"][0].endpoint == "arn:aws:sns:x"
    assert sem["eventbus_publish"][0].framework == "aws-sns"


def test_sns_topic_publish_characterization(tmp_path) -> None:
    # Locks in pre-change SNS behavior before Task 3 touches _producer/_address.
    src = b"""import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';

export async function notify(sns: SNSClient) {
  await sns.send(new PublishCommand({ TopicArn: 'arn:aws:sns:x', Message: 'm' }));
}
"""
    rec = _parse(tmp_path, "notify.ts", src)
    sem = _by_semantic(rec)
    assert len(sem["eventbus_publish"]) == 1
    pub = sem["eventbus_publish"][0]
    assert pub.framework == "aws-sns"
    assert pub.method == "PublishCommand"
    assert pub.endpoint == "arn:aws:sns:x"


def test_publish_command_without_address_key_stays_sns(tmp_path) -> None:
    # Locks in the no-address-key default: today _producer discards _address's presence
    # flag on the v3 branch, so a keyless PublishCommand still resolves aws-sns/null, not
    # "undetected". Both ends of the address-key spectrum: this test is the empty end,
    # test_sns_topic_publish_characterization above is the present end.
    src = b"""import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';

export async function notify(sns: SNSClient) {
  await sns.send(new PublishCommand({ Message: 'm' }));
}
"""
    rec = _parse(tmp_path, "notify.ts", src)
    sem = _by_semantic(rec)
    assert len(sem["eventbus_publish"]) == 1
    pub = sem["eventbus_publish"][0]
    assert pub.framework == "aws-sns"
    assert pub.endpoint is None


SMS_LITERAL = b"""import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';

export async function sendCode(sns: SNSClient, code: string) {
  await sns.send(new PublishCommand({ PhoneNumber: '+14155550100', Message: code }));
}
"""

SMS_VARIABLE = b"""import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';

export async function sendCode(sns: SNSClient, phone: string, code: string) {
  await sns.send(new PublishCommand({ PhoneNumber: phone, Message: code }));
}
"""

SMS_AND_SNS = b"""import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';

export async function fanout(sns: SNSClient) {
  await sns.send(new PublishCommand({ PhoneNumber: '+14155550100', Message: 'm' }));
  await sns.send(new PublishCommand({ TopicArn: 'arn:aws:sns:x', Message: 'm' }));
}
"""


def test_sms_publish_phone_number_literal_detected(tmp_path) -> None:
    rec = _parse(tmp_path, "sms.ts", SMS_LITERAL)
    sem = _by_semantic(rec)
    assert len(sem["eventbus_publish"]) == 1
    pub = sem["eventbus_publish"][0]
    assert pub.framework == "aws-sms"
    assert pub.method == "PublishCommand"
    assert pub.endpoint == "+14155550100"


def test_sms_publish_phone_number_variable_is_honest_null(tmp_path) -> None:
    rec = _parse(tmp_path, "sms.ts", SMS_VARIABLE)
    sem = _by_semantic(rec)
    pub = sem["eventbus_publish"][0]
    assert pub.framework == "aws-sms"
    assert pub.endpoint is None  # PhoneNumber is a symbol, never the symbol text


def test_sms_and_sns_in_same_file_split_correctly(tmp_path) -> None:
    rec = _parse(tmp_path, "fanout.ts", SMS_AND_SNS)
    sem = _by_semantic(rec)
    got = {(s.framework, s.endpoint) for s in sem["eventbus_publish"]}
    assert got == {("aws-sms", "+14155550100"), ("aws-sns", "arn:aws:sns:x")}
    assert all(s.method == "PublishCommand" for s in sem["eventbus_publish"])


def test_sms_file_framework_rollup(tmp_path) -> None:
    rec = _parse(tmp_path, "sms.ts", SMS_LITERAL)
    assert rec.framework == "aws-sms"


def test_v2_publish_with_phone_number_endpoint_now_resolved(tmp_path) -> None:
    # Accepted side-effect of widening _ADDRESS_KEYS: the v2 `.publish()` method is
    # deliberately NOT branched to aws-sms (issue #77 scopes Task 3 to PublishCommand), but
    # the endpoint is now resolved instead of honest-null — strictly more informative.
    src = b"""import { SNS } from 'aws-sdk';

export class Notifier {
  private sns: SNS;
  async sendCode() {
    await this.sns.publish({ PhoneNumber: '+14155550100', Message: 'm' });
  }
}
"""
    rec = _parse(tmp_path, "notifier.ts", src)
    sem = _by_semantic(rec)
    pub = sem["eventbus_publish"][0]
    assert pub.framework == "aws-sns"
    assert pub.endpoint == "+14155550100"


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, "dispatch.service.ts", PRODUCER)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors

    rec = _parse(tmp_path, "sms.ts", SMS_LITERAL)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


# --- Untyped CommonJS Lambda handler -------------------------------------------------------
# `exports.handler = async () => {}` carries no typed AWS event, so the typed-parameter path
# can't see it. In an AWS-touching file the CommonJS `exports.handler`/`module.exports.handler`
# shape is a Lambda entry point on its own — captured as a generic `aws-lambda` consumer.
UNTYPED_CJS_NULLARY = b'''
import { S3 } from 'aws-sdk';
exports.handler = async () => {
  await check();
  await new Handler().handle();
};
'''


def test_untyped_cjs_nullary_handler_detected(tmp_path) -> None:
    rec = _parse(tmp_path, "index.ts", UNTYPED_CJS_NULLARY)
    consumers = _by_semantic(rec).get("eventbus_consumer", [])
    assert [(c.framework, c.handler) for c in consumers] == [("aws-lambda", "handler")]
    assert rec.framework == "aws-lambda"


def test_module_exports_handler_detected(tmp_path) -> None:
    src = b"import 'aws-sdk';\nmodule.exports.handler = async () => { await run(); };\n"
    rec = _parse(tmp_path, "index.ts", src)
    fw = {(c.framework, c.handler) for c in _by_semantic(rec).get("eventbus_consumer", [])}
    assert ("aws-lambda", "handler") in fw


def test_cjs_main_not_matched_without_event(tmp_path) -> None:
    # `main` is too generic to claim as a Lambda without a typed event (could be a CLI entry).
    src = b"import 'aws-sdk';\nexports.main = async () => { await run(); };\n"
    rec = _parse(tmp_path, "index.ts", src)
    assert [s for s in rec.statements if s.semanticType] == []


def test_arbitrary_object_handler_not_matched(tmp_path) -> None:
    # Only `exports`/`module.exports` receivers — not an arbitrary `obj.handler = fn`.
    src = b"import 'aws-sdk';\nconst app: any = {};\napp.handler = async () => { await run(); };\n"
    rec = _parse(tmp_path, "index.ts", src)
    assert [s for s in rec.statements if s.semanticType] == []


def test_esm_export_const_nullary_still_gated(tmp_path) -> None:
    # The ESM `export const handler = () => {}` shape (common in non-Lambda code) stays gated on
    # a typed AWS event — a nullary one is NOT matched.
    src = b"import 'aws-sdk';\nexport const handler = async () => { await run(); };\n"
    rec = _parse(tmp_path, "index.ts", src)
    assert [s for s in rec.statements if s.semanticType] == []


def test_cjs_nullary_handler_skipped_in_fixture(tmp_path) -> None:
    # A mocked `exports.handler` in a test/spec file must not be captured (fixture guard).
    rec = _parse(tmp_path, "handler.spec.ts", UNTYPED_CJS_NULLARY)
    assert [s for s in rec.statements if s.semanticType] == []


def test_untyped_cjs_handler_without_aws_import(tmp_path) -> None:
    # The real-world case: a CJS `exports.handler` Lambda whose file imports no AWS SDK (it is a
    # Lambda by deployment config). The CommonJS handler form is itself the entry-point signal.
    src = b"import { check } from './checker';\nexports.handler = async () => { await check(); };\n"
    rec = _parse(tmp_path, "index.ts", src)
    fw = {(c.framework, c.handler) for c in _by_semantic(rec).get("eventbus_consumer", [])}
    assert ("aws-lambda", "handler") in fw
