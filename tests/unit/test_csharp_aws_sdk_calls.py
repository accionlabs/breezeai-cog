"""AWS SDK for .NET calls in C#: request/response services as api_call, SNS / SQS / EventBridge
as event-bus statements, an SQS receive as a consumer binding, gated on the receiver's declared
client type."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.imports import build_csharp_index
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.schemas import FileRecord

_AWS = ("api_call", "eventbus_publish", "eventbus_send", "eventbus_consumer")

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


WORKER = b'''using Amazon.SQS;
using Amazon.SQS.Model;
namespace Shop {
  public class OrderWorker : BackgroundService {
    private const string QueueUrl = "https://sqs.eu-west-1.amazonaws.com/123456789012/orders";
    private readonly IAmazonSQS _sqs;
    private readonly string _billingUrl;

    protected override async Task ExecuteAsync(CancellationToken ct) {
      while (!ct.IsCancellationRequested) {
        var batch = await _sqs.ReceiveMessageAsync(new ReceiveMessageRequest { QueueUrl = QueueUrl }, ct);
        await _sqs.DeleteMessageAsync(QueueUrl, batch.Messages[0].ReceiptHandle, ct);
      }
    }
    public async Task DrainBilling() { var r = await _sqs.ReceiveMessageAsync(_billingUrl); }
    public async Task Audit(string QueueUrl) { await _sqs.ReceiveMessageAsync(QueueUrl); }
    public async Task Forward(string body) {
      await _sqs.SendMessageAsync(Queues.Invoices, body);
      await _sqs.ReceiveMessageAsync(Queues.Invoices);
    }
  }
}
'''

QUEUES = b'''namespace Shop {
  public static class Queues {
    public static readonly string Invoices = "https://sqs.eu-west-1.amazonaws.com/123456789012/invoices";
  }
}
'''


def _parse_repo(tmp_path, files: dict[str, bytes], target: str) -> FileRecord:
    paths = []
    for rel, src in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(src)
        paths.append(f)
    ctx = ParseContext(path=target, abs_path=tmp_path / target, source=files[target],
                       repo_root=tmp_path, capture_statements=True,
                       resolution_index=build_csharp_index(tmp_path, paths))
    return CSharpParser().parse_file(ctx)


def test_sqs_receive_is_a_consumer_of_the_method_it_is_in(tmp_path) -> None:
    rec = _parse_repo(tmp_path, {"OrderWorker.cs": WORKER, "Queues.cs": QUEUES}, "OrderWorker.cs")
    got = [(s.startLine, s.semanticType, s.method, s.endpoint, s.handler, s.handlerLine)
           for s in rec.statements if s.semanticType in _AWS]
    assert got == [
        # the queue URL is a const of the class: the worker loop consumes `orders`
        (11, "eventbus_consumer", "CONSUMER", "orders", "ExecuteAsync", 9),
        # set at run time: still the queue's consumer, address unknown
        (15, "eventbus_consumer", "CONSUMER", None, "DrainBilling", 15),
        # a parameter named like the const is the parameter
        (16, "eventbus_consumer", "CONSUMER", None, "Audit", 16),
        # a static readonly in another file, for the send and the receive alike
        (18, "eventbus_send", "SendMessageAsync", "invoices", None, None),
        (19, "eventbus_consumer", "CONSUMER", "invoices", "Forward", 17),
    ]
    assert all(s.framework == "aws-sqs" for s in rec.statements if s.semanticType in _AWS)


def test_receive_on_another_type_is_not_a_consumer() -> None:
    src = b'''using Amazon.SQS;
class Chat {
  private readonly IChatClient _chat;
  async Task Run(IAmazonSQS sqs) { await _chat.ReceiveMessageAsync("room"); }
}
'''
    assert _calls(_parse(src)) == []


def test_a_client_name_is_bound_in_its_own_class(tmp_path) -> None:
    # Two classes in one file with the same field names: only the one typed as an AWS client
    # calls AWS, whichever comes first, and its own constant is the address.
    other = b'''public class OtherSender {
  private const string QueueUrl = "local-queue";
  private readonly IMyQueue _sqs;
  public async Task Run() { await _sqs.SendMessageAsync(QueueUrl, "x"); }
}
'''
    aws = b'''public class AwsSender {
  private const string QueueUrl = "https://sqs.us-east-1.amazonaws.com/111/aws-queue";
  private readonly IAmazonSQS _sqs;
  public async Task Run() { await _sqs.SendMessageAsync(QueueUrl, "x"); }
}
'''
    expected = [("eventbus_send", "aws-sqs", "SendMessageAsync", "aws-queue")]
    for body in (other + aws, aws + other):
        src = b"using Amazon.SQS;\n" + body
        assert _calls(_parse_repo(tmp_path, {"Senders.cs": src}, "Senders.cs")) == expected
