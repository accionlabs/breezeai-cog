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

# Producer shapes (SDK v3 command + v2 method), modeled on notifications/libs/sns + apps/api.
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

# Consumer + HTTP-entry shapes, modeled on notifications/apps/*-lambda/src/main.ts.
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


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, "dispatch.service.ts", PRODUCER)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
