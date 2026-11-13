"""AWS SDK for .NET calls in VB: the C# detector's rules on the VB tree — request/response services
as api_call, SNS / SQS / EventBridge as event-bus statements, an SQS receive as a consumer
binding, gated on the receiver's declared client type."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.vb.parser import VbParser
from breezeai_cog.schemas import FileRecord

_AWS = ("api_call", "eventbus_publish", "eventbus_send", "eventbus_consumer")

SERVICE = b'''Imports Amazon.S3
Imports Amazon.S3.Transfer
Imports Amazon.SimpleNotificationService
Imports Amazon.SQS
Imports Amazon.EventBridge
Imports Amazon.Lambda

Namespace Shop
    Public Class OrderEvents
        Private ReadOnly _sns As IAmazonSimpleNotificationService
        Private ReadOnly _events As Amazon.EventBridge.IAmazonEventBridge
        Private ReadOnly _topicArn As String
        Public ReadOnly Property Storage As IAmazonS3

        Public Async Function Placed(o As Order, sqs As IAmazonSQS, lambda As IAmazonLambda) As Task
            Await Storage.PutObjectAsync(New PutObjectRequest With {.BucketName = "orders", .Key = o.Id})
            Dim req = New PublishRequest With {.TopicArn = "arn:aws:sns:eu-west-1:123456789012:order-placed"}
            Await _sns.PublishAsync(req)
            Await Me._sns.PublishAsync(New PublishRequest With {.TopicArn = _topicArn, .Message = "m"})
            Await _sns.PublishAsync(New PublishRequest With {.PhoneNumber = "+15555550100", .Message = "m"})
            Await sqs.SendMessageAsync("https://sqs.eu-west-1.amazonaws.com/123456789012/billing", "b")
            Dim entry = New PutEventsRequestEntry With {.EventBusName = "arn:aws:events:eu-west-1:123456789012:event-bus/shop"}
            Await _events.PutEventsAsync(New PutEventsRequest With {.Entries = New List(Of PutEventsRequestEntry)})
            Await lambda.InvokeAsync(New InvokeRequest With {.FunctionName = "notify"})
            Dim client = New AmazonS3Client()
            Dim upload = New TransferUtility(client).UploadAsync("f", "orders")
        End Function
    End Class
End Namespace
'''


def _parse(src: bytes, name: str = "OrderEvents.vb", *, capture: bool = True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None,
                       capture_statements=capture)
    return VbParser().parse_file(ctx)


def _calls(rec: FileRecord) -> list[tuple]:
    return [(s.startLine, s.semanticType, s.framework, s.method, s.endpoint) for s in rec.statements
            if s.semanticType in _AWS]


def test_aws_calls_by_service() -> None:
    assert _calls(_parse(SERVICE)) == [
        (16, "api_call", "aws-s3", None, "PutObjectAsync"),
        (18, "eventbus_publish", "aws-sns", "PublishAsync", "order-placed"),  # a local's request
        (19, "eventbus_publish", "aws-sns", "PublishAsync", None),  # injected topic: no address
        (20, "eventbus_send", "aws-sns", "PublishAsync", "+15555550100"),  # an SMS
        (21, "eventbus_send", "aws-sqs", "SendMessageAsync", "billing"),  # positional queue URL
        (23, "eventbus_publish", "aws-eventbridge", "PutEventsAsync", None),  # bus not on the call
        (24, "api_call", "aws-lambda", None, "InvokeAsync"),
        (26, "api_call", "aws-s3", None, "UploadAsync"),  # a client created in place
    ]


def test_the_call_enriches_its_own_statement() -> None:
    put = next(s for s in _parse(SERVICE).statements if s.endpoint == "PutObjectAsync")
    assert put.nodeType == "call_statement" and put.id == "OrderEvents.vb:16:12"
    assert put.text.startswith("Await Storage.PutObjectAsync(")


def test_same_verbs_on_other_types_are_not_aws() -> None:
    src = b'''Imports Amazon.SimpleNotificationService
Imports MediatR
Class Handler
    Private ReadOnly _bus As IMediator
    Private ReadOnly _chat As IChatClient
    Async Function Run(sns As IAmazonSimpleNotificationService) As Task
        Await _bus.PublishAsync(New OrderPlaced())
        Await _chat.SendMessageAsync("hello")
        Await publisher.PublishAsync(New PublishRequest With {.TopicArn = "arn:aws:sns:eu-west-1:1:t"})
    End Function
End Class
'''
    assert _calls(_parse(src)) == []


def test_a_shadowing_declaration_hides_the_client() -> None:
    # A local, a lambda parameter or a For Each variable named like the client field is not it.
    src = b'''Imports Amazon.SQS
Class Q
    Private ReadOnly _sqs As IAmazonSQS
    Sub A()
        Dim _sqs As IMyQueue = Nothing
        _sqs.SendMessageAsync("orders", "x")
    End Sub
    Sub B()
        Dim f = Function(_sqs) _sqs.SendMessageAsync("orders", "x")
    End Sub
    Sub C(queues As List(Of IMyQueue))
        For Each _sqs In queues
            _sqs.SendMessageAsync("orders", "x")
        Next
    End Sub
    Sub D()
        _SQS.SendMessageAsync("orders", "x")
    End Sub
End Class
'''
    # Only D reaches the field (VB names are case-insensitive).
    assert _calls(_parse(src)) == [(17, "eventbus_send", "aws-sqs", "SendMessageAsync", "orders")]


def test_non_operations_and_unrecorded_operations() -> None:
    src = b'''Imports Amazon.S3
Imports Amazon.SQS
Imports Amazon.SimpleNotificationService
Class Jobs
    Async Function Run(s3 As IAmazonS3, sqs As IAmazonSQS, sns As IAmazonSimpleNotificationService) As Task
        Dim url = s3.GetPreSignedURL(New GetPreSignedUrlRequest())
        s3.Dispose()
        Await sqs.DeleteMessageAsync("q", "h")
        Await sns.CreateTopicAsync("t")
    End Function
End Class
'''
    assert _calls(_parse(src)) == []


def test_address_only_from_one_literal() -> None:
    src = b'''Imports Amazon.SimpleNotificationService
Class N
    Private Const Topic As String = "arn:aws:sns:eu-west-1:1:c"
    Async Function Run(sns As IAmazonSimpleNotificationService, arn As String) As Task
        Await sns.PublishAsync(arn, "m")
        Await sns.PublishAsync($"arn:aws:sns:eu-west-1:1:{arn}", "m")
        Await sns.PublishAsync(Topic, "m")
    End Function
    Async Function Twice(sns As IAmazonSimpleNotificationService, a As Boolean) As Task
        Dim r = New PublishRequest With {.TopicArn = "arn:aws:sns:eu-west-1:1:a"}
        If a Then
            Dim r = New PublishRequest With {.TopicArn = "arn:aws:sns:eu-west-1:1:b"}
        End If
        Await sns.PublishAsync(r)
    End Function
End Class
'''
    assert [c[4] for c in _calls(_parse(src))] == [None, None, None, None]


def test_fixture_files_and_capture_off_are_skipped() -> None:
    assert _calls(_parse(SERVICE, "__mocks__/OrderEvents.vb")) == []
    assert _calls(_parse(SERVICE, capture=False)) == []


WORKER = b'''Imports Amazon.SQS
Imports Amazon.SQS.Model

Public Class OrderWorker
    Inherits BackgroundService
    Private ReadOnly _sqs As IAmazonSQS

    Protected Overrides Async Function ExecuteAsync(ct As CancellationToken) As Task
        While Not ct.IsCancellationRequested
            Dim batch = Await _sqs.ReceiveMessageAsync(New ReceiveMessageRequest With {
                .QueueUrl = "https://sqs.eu-west-1.amazonaws.com/123456789012/orders"}, ct)
            Await _sqs.DeleteMessageAsync("orders", batch.Messages(0).ReceiptHandle, ct)
        End While
    End Function

    Public Async Function Drain(url As String) As Task
        Await _sqs.ReceiveMessageAsync(url)
    End Function

    Public Sub New(sqs As IAmazonSQS)
        sqs.ReceiveMessageAsync("https://sqs.eu-west-1.amazonaws.com/1/startup")
    End Sub
End Class
'''


def test_sqs_receive_is_a_consumer_of_the_method_it_is_in() -> None:
    rec = _parse(WORKER, "OrderWorker.vb")
    got = [(s.startLine, s.semanticType, s.method, s.endpoint, s.handler, s.handlerLine)
           for s in rec.statements if s.semanticType in _AWS]
    assert got == [
        (10, "eventbus_consumer", "CONSUMER", "orders", "ExecuteAsync", 8),
        (17, "eventbus_consumer", "CONSUMER", None, "Drain", 16),  # address set at run time
        (21, "eventbus_consumer", "CONSUMER", "startup", "New", 20),
    ]


def test_a_client_name_is_bound_in_its_own_class() -> None:
    other = b'''Public Class OtherSender
    Private ReadOnly _sqs As IMyQueue
    Public Async Function Run() As Task
        Await _sqs.SendMessageAsync("local-queue", "x")
    End Function
End Class
'''
    aws = b'''Public Class AwsSender
    Private ReadOnly _sqs As IAmazonSQS
    Public Async Function Run() As Task
        Await _sqs.SendMessageAsync("https://sqs.us-east-1.amazonaws.com/111/aws-queue", "x")
    End Function
End Class
'''
    for body in (other + aws, aws + other):
        src = b"Imports Amazon.SQS\n" + body
        assert [c[1:] for c in _calls(_parse(src))] == [
            ("eventbus_send", "aws-sqs", "SendMessageAsync", "aws-queue")]


def test_a_nested_type_has_its_own_clients() -> None:
    src = b'''Imports Amazon.S3
Public Class Outer
    Private ReadOnly _s3 As IMyStore
    Public Class Uploader
        Private ReadOnly _s3 As IAmazonS3
        Public Sub Go()
            _s3.PutObjectAsync(Nothing)
        End Sub
    End Class
    Public Sub Go()
        _s3.PutObjectAsync(Nothing)
    End Sub
End Class
'''
    assert _calls(_parse(src)) == [(7, "api_call", "aws-s3", None, "PutObjectAsync")]
