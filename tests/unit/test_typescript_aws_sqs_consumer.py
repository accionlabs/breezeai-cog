from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.schemas import FileRecord


def _parse(tmp_path: Path, source: str) -> FileRecord:
    path = tmp_path / "worker.ts"
    data = source.encode()
    path.write_bytes(data)
    return TypeScriptParser().parse_file(
        ParseContext(
            path="worker.ts",
            abs_path=path,
            source=data,
            repo_root=tmp_path,
            capture_statements=True,
        )
    )


def _consumers(record: FileRecord):
    return [s for s in record.statements if s.semanticType == "eventbus_consumer"]


def test_sdk_v3_receive_is_consumer(tmp_path: Path) -> None:
    record = _parse(
        tmp_path,
        """
import { SQSClient, ReceiveMessageCommand } from '@aws-sdk/client-sqs';
const sqs = new SQSClient({});
const QUEUE = 'https://sqs.us-east-1.amazonaws.com/123/orders';
async function poll() {
  await sqs.send(new ReceiveMessageCommand({ QueueUrl: QUEUE }));
}
""",
    )
    [consumer] = _consumers(record)
    assert (consumer.framework, consumer.method, consumer.endpoint, consumer.handler) == (
        "aws-sqs",
        "CONSUMER",
        "orders",
        "poll",
    )


def test_sdk_v3_class_client_field_is_scoped(tmp_path: Path) -> None:
    record = _parse(
        tmp_path,
        """
import { SQSClient, ReceiveMessageCommand } from '@aws-sdk/client-sqs';
class SqsReceiver {
  private readonly sqsClient: SQSClient;
  async receiveMessages() {
    await this.sqsClient.send(new ReceiveMessageCommand({
      QueueUrl: 'https://sqs.us-east-1.amazonaws.com/123/orders'
    }));
  }
}
""",
    )
    [consumer] = _consumers(record)
    assert consumer.handler == "receiveMessages"
    assert consumer.endpoint == "orders"


def test_sdk_v2_receive_is_consumer(tmp_path: Path) -> None:
    record = _parse(
        tmp_path,
        """
import { SQS } from 'aws-sdk';
const sqs = new SQS();
async function poll() {
  await sqs.receiveMessage({ QueueUrl: 'https://sqs.us-east-1.amazonaws.com/123/jobs' }).promise();
}
""",
    )
    [consumer] = _consumers(record)
    assert (consumer.framework, consumer.method, consumer.endpoint, consumer.handler) == (
        "aws-sqs",
        "CONSUMER",
        "jobs",
        "poll",
    )


def test_sdk_v2_namespace_parameter_property_is_consumer(tmp_path: Path) -> None:
    record = _parse(
        tmp_path,
        """
import * as AWS from 'aws-sdk';
export class QueueConsumer {
  constructor(protected sqs: AWS.SQS, protected request: AWS.SQS.Types.ReceiveMessageRequest) {}
  protected async poll() {
    await this.sqs.receiveMessage(this.request).promise();
  }
}
""",
    )
    [consumer] = _consumers(record)
    assert (consumer.framework, consumer.method, consumer.endpoint, consumer.handler) == (
        "aws-sqs",
        "CONSUMER",
        None,
        "poll",
    )


def test_dynamic_queue_url_is_honest_null(tmp_path: Path) -> None:
    record = _parse(
        tmp_path,
        """
import { SQSClient, ReceiveMessageCommand } from '@aws-sdk/client-sqs';
const sqs = new SQSClient({});
async function poll(queueUrl: string) {
  await sqs.send(new ReceiveMessageCommand({ QueueUrl: queueUrl }));
}
""",
    )
    [consumer] = _consumers(record)
    assert consumer.endpoint is None


def test_receive_message_on_non_sqs_object_is_ignored(tmp_path: Path) -> None:
    record = _parse(
        tmp_path,
        """
class OtherClient { receiveMessage(options: object) { return options; } }
const client = new OtherClient();
async function poll() {
  await client.receiveMessage({ QueueUrl: 'https://example.test/not-sqs' });
}
""",
    )
    assert _consumers(record) == []
