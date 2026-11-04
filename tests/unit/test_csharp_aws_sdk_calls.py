"""AWS SDK for .NET calls in C#: request/response services as api_call, SNS / SQS / EventBridge
as event-bus statements, gated on the receiver's declared client type."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.schemas import FileRecord

_AWS = ("api_call", "eventbus_publish", "eventbus_send")

SERVICE = b'''using Amazon.S3;
using Amazon.S3.Transfer;
using Amazon.SimpleNotificationService;
using Amazon.SimpleNotificationService.Model;
using Amazon.SQS;
using Amazon.EventBridge;
using Amazon.Lambda;
namespace Shop {
  public class OrderEvents {
    private readonly IAmazonSimpleNotificationService _sns;
    private readonly IAmazonEventBridge _events;
    private readonly string _topicArn;
    public IAmazonS3 Storage { get; }

    public async Task Placed(Order o, IAmazonSQS sqs, IAmazonLambda lambda) {
      await Storage.PutObjectAsync(new PutObjectRequest { BucketName = "orders", Key = o.Id });
      var req = new PublishRequest { TopicArn = "arn:aws:sns:eu-west-1:123456789012:order-placed" };
      await _sns.PublishAsync(req);
      await this._sns.PublishAsync(new PublishRequest { TopicArn = _topicArn, Message = "m" });
      await _sns.PublishAsync(new PublishRequest { PhoneNumber = "+15555550100", Message = "m" });
      await sqs.SendMessageAsync("https://sqs.eu-west-1.amazonaws.com/123456789012/billing", "b");
      await _events.PutEventsAsync(new PutEventsRequest { Entries = new List<PutEventsRequestEntry> {
        new PutEventsRequestEntry { EventBusName = "arn:aws:events:eu-west-1:123456789012:event-bus/shop" } } });
      await lambda.InvokeAsync(new InvokeRequest { FunctionName = "notify" });
      var client = new AmazonS3Client();
      await new TransferUtility(client).UploadAsync("f", "orders");
    }
  }
}
'''


def _parse(src: bytes, name: str = "OrderEvents.cs", *, capture: bool = True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None,
                       capture_statements=capture)
    return CSharpParser().parse_file(ctx)


def _calls(rec: FileRecord) -> list[tuple[str | None, str | None, str | None, str | None]]:
    return [(s.semanticType, s.framework, s.method, s.endpoint) for s in rec.statements
            if s.semanticType in _AWS]


def test_aws_calls_by_service() -> None:
    assert _calls(_parse(SERVICE)) == [
        ("api_call", "aws-s3", None, "PutObjectAsync"),
        ("eventbus_publish", "aws-sns", "PublishAsync", "order-placed"),
        ("eventbus_publish", "aws-sns", "PublishAsync", None),
        ("eventbus_send", "aws-sns", "PublishAsync", "+15555550100"),
        ("eventbus_send", "aws-sqs", "SendMessageAsync", "billing"),
        ("eventbus_publish", "aws-eventbridge", "PutEventsAsync", "shop"),
        ("api_call", "aws-lambda", None, "InvokeAsync"),
        ("api_call", "aws-s3", None, "UploadAsync"),
    ]


def test_the_call_enriches_its_own_statement() -> None:
    rec = _parse(SERVICE)
    put = next(s for s in rec.statements if s.endpoint == "PutObjectAsync")
    assert put.nodeType == "expression_statement"
    assert put.text.startswith("await Storage.PutObjectAsync(")


def test_same_verbs_on_other_types_are_not_aws() -> None:
    src = b'''using Amazon.SimpleNotificationService;
using MediatR;
class Handler {
  private readonly IMediator _bus;
  private readonly IChatClient _chat;
  async Task Run(IAmazonSimpleNotificationService sns) {
    await _bus.PublishAsync(new OrderPlaced());
    await _chat.SendMessageAsync("hello");
    await publisher.PublishAsync(new PublishRequest { TopicArn = "arn:aws:sns:eu-west-1:1:t" });
  }
}
'''
    assert _calls(_parse(src)) == []


def test_non_operations_and_unrecorded_operations() -> None:
    src = b'''using Amazon.S3;
using Amazon.SQS;
using Amazon.SimpleNotificationService;
class Jobs {
  async Task Run(IAmazonS3 s3, IAmazonSQS sqs, IAmazonSimpleNotificationService sns) {
    var url = s3.GetPreSignedURL(new GetPreSignedUrlRequest());
    s3.Dispose();
    var batch = await sqs.ReceiveMessageAsync(new ReceiveMessageRequest { QueueUrl = "q" });
    await sqs.DeleteMessageAsync("q", "h");
    await sns.CreateTopicAsync("t");
  }
}
'''
    assert _calls(_parse(src)) == []


def test_address_only_from_one_literal() -> None:
    src = b'''using Amazon.SimpleNotificationService;
class N {
  async Task Run(IAmazonSimpleNotificationService sns, string arn) {
    await sns.PublishAsync(arn, "m");
    await sns.PublishAsync($"arn:aws:sns:eu-west-1:1:{arn}", "m");
  }
  async Task Twice(IAmazonSimpleNotificationService sns, bool a) {
    var r = new PublishRequest { TopicArn = "arn:aws:sns:eu-west-1:1:a" };
    if (a) { var r = new PublishRequest { TopicArn = "arn:aws:sns:eu-west-1:1:b" }; }
    await sns.PublishAsync(r);
  }
}
'''
    assert [c[3] for c in _calls(_parse(src))] == [None, None, None]


def test_fixture_files_and_capture_off_are_skipped() -> None:
    assert _calls(_parse(SERVICE, "__mocks__/OrderEvents.cs")) == []
    assert _calls(_parse(SERVICE, capture=False)) == []
