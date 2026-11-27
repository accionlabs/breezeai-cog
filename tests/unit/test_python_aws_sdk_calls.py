"""boto3 calls in Python: request/response services as api_call, SNS / SQS / EventBridge as
event-bus statements, an SQS receive as a consumer binding — only on a receiver bound to a boto3
client or resource. Role table: docs/entry-point-roles/aws-sdk-python.md."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.python.parser import PythonParser
from breezeai_cog.schemas import FileRecord

_AWS = ("api_call", "eventbus_publish", "eventbus_send", "eventbus_consumer")
_QUEUE = "https://sqs.eu-west-1.amazonaws.com/123456789012/orders"


def _parse(src: str, name: str = "svc/store.py", *, capture: bool = True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=Path(name), source=src.encode(), repo_root=Path("."),
                       capture_statements=capture)
    return PythonParser().parse_file(ctx)


def _calls(rec: FileRecord) -> list[tuple[int, str | None, str | None, str | None, str | None]]:
    return [(s.startLine, s.semanticType, s.framework, s.method, s.endpoint)
            for s in rec.statements if s.semanticType in _AWS]


CLIENTS = f'''import boto3

QUEUE_URL = "{_QUEUE}"
s3 = boto3.client("s3")


def run(body, key):
    s3.put_object(Bucket="b", Key=key, Body=body)
    boto3.client("ssm").get_parameter(Name="/app/db")
    boto3.client(service_name="cognito-idp").admin_get_user(UserPoolId="p", Username="u")
    boto3.client("lambda").invoke(FunctionName="notify", Payload=b"{{}}")
    sns = boto3.client("sns")
    sns.publish(TopicArn="arn:aws:sns:eu-west-1:123456789012:order-created", Message=body)
    sns.publish(PhoneNumber="+15555550100", Message=body)
    sns.publish_batch(TopicArn="arn:aws:sns:eu-west-1:123456789012:audit", PublishBatchRequestEntries=[])
    sqs = boto3.client("sqs")
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=body)
    sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=[])
    events = boto3.client("events")
    events.put_events(Entries=[{{"EventBusName": "arn:aws:events:eu-west-1:123456789012:event-bus/shop"}}])
'''


def test_client_calls_by_service() -> None:
    assert _calls(_parse(CLIENTS)) == [
        (8, "api_call", "aws-s3", None, "put_object"),
        (9, "api_call", "aws-ssm", None, "get_parameter"),
        (10, "api_call", "aws-cognito", None, "admin_get_user"),
        (11, "api_call", "aws-lambda", None, "invoke"),
        (13, "eventbus_publish", "aws-sns", "publish", "order-created"),
        # an SMS reaches one recipient: nothing subscribes to a phone number
        (14, "eventbus_send", "aws-sns", "publish", "+15555550100"),
        (15, "eventbus_publish", "aws-sns", "publish_batch", "audit"),
        (17, "eventbus_send", "aws-sqs", "send_message", "orders"),
        (18, "eventbus_send", "aws-sqs", "send_message_batch", "orders"),
        (20, "eventbus_publish", "aws-eventbridge", "put_events", "shop"),
    ]


def test_the_call_enriches_its_own_statement() -> None:
    rec = _parse(CLIENTS)
    put = next(s for s in rec.statements if s.endpoint == "put_object")
    assert (put.nodeType, put.text) == ("call", 's3.put_object(Bucket="b", Key=key, Body=body)')
    assert sum(1 for s in rec.statements if s.startLine == 8) == 1


RESOURCES = f'''import boto3


def run(body):
    sqs = boto3.resource("sqs")
    queue = sqs.Queue("{_QUEUE}")
    queue.send_message(MessageBody=body)
    sqs.get_queue_by_name(QueueName="jobs").send_messages(Entries=[])
    sqs.Queue(url).send_message(MessageBody=body)
    topic = boto3.resource("sns").Topic("arn:aws:sns:eu-west-1:123456789012:alerts")
    topic.publish(Message=body)
    s3 = boto3.resource("s3")
    s3.Bucket("media").upload_file("a.png", "a.png")
    s3.Bucket("media").Object("k").put(Body=body)
    s3.meta.client.download_file("media", "k", "/tmp/k")
    sqs.create_queue(QueueName="new")
'''


def test_resource_style() -> None:
    assert _calls(_parse(RESOURCES)) == [
        (7, "eventbus_send", "aws-sqs", "send_message", "orders"),
        (8, "eventbus_send", "aws-sqs", "send_messages", "jobs"),
        # a queue whose URL is not in the code: still a send, address unknown
        (9, "eventbus_send", "aws-sqs", "send_message", None),
        (11, "eventbus_publish", "aws-sns", "publish", "alerts"),
        (13, "api_call", "aws-s3", None, "upload_file"),
        (14, "api_call", "aws-s3", None, "put"),
        (15, "api_call", "aws-s3", None, "download_file"),
        # create_queue (line 16) is management, not traffic
    ]


BINDINGS = '''import boto3
from boto3 import client as make_client
from boto3.session import Session


class Store:
    archive = boto3.client("s3")

    def __init__(self, session: Session):
        self.sqs = session.client("sqs")
        self.sns = boto3.Session(profile_name="p").client("sns")

    def save(self, body):
        self.archive.put_object(Bucket="b", Key="k", Body=body)
        self.sqs.send_message(QueueUrl=self.url, MessageBody=body)
        self.sns.publish(TopicArn=self.topic, Message=body)
        make_client("lambda").invoke(FunctionName="f")
        notify = lambda m: self.sns.publish(TopicArn=self.topic, Message=m)
'''


def test_receivers_from_sessions_aliases_and_self_attributes() -> None:
    assert _calls(_parse(BINDINGS)) == [
        (14, "api_call", "aws-s3", None, "put_object"),
        (15, "eventbus_send", "aws-sqs", "send_message", None),
        (16, "eventbus_publish", "aws-sns", "publish", None),
        (17, "api_call", "aws-lambda", None, "invoke"),
        (18, "eventbus_publish", "aws-sns", "publish", None),
    ]


def test_same_verbs_on_other_receivers_are_not_aws() -> None:
    src = '''import boto3
import redis

r = redis.Redis()
s3 = boto3.client("s3")


class Chat:
    def __init__(self, slack, sqs):
        self.slack = slack
        self.sqs = sqs

    def go(self, sqs, s3):
        r.publish("chan", "m")
        self.slack.send_message(channel="c", text="t")
        self.sqs.send_message(QueueUrl="q", MessageBody="b")
        sqs.send_message(QueueUrl="q", MessageBody="b")
        s3.put_object(Bucket="b", Key="k")

    @staticmethod
    def helper(self):
        self.sqs.send_message(QueueUrl="q", MessageBody="b")


def rebound(flag):
    c = boto3.client("sqs")
    if flag:
        c = FakeQueue()
    c.send_message(QueueUrl="q", MessageBody="b")
    other = boto3.client("kinesis")
    other.put_record(StreamName="s")
'''
    assert _calls(_parse(src)) == []


def test_no_boto3_import_no_aws_tags() -> None:
    src = '''from mylib import boto3

s3 = boto3.client("s3")
s3.put_object(Bucket="b", Key="k")
'''
    assert _calls(_parse(src)) == []
    assert _calls(_parse(CLIENTS.replace("import boto3", "import botocore"))) == []


def test_non_operations_and_unrecorded_services() -> None:
    src = '''import boto3

s3 = boto3.client("s3")
sqs = boto3.client("sqs")


def run():
    s3.generate_presigned_url("get_object", Params={})
    s3.get_paginator("list_objects_v2")
    s3.close()
    sqs.delete_message(QueueUrl="q", ReceiptHandle="h")
    sqs.create_queue(QueueName="q")
    boto3.client("kinesis").put_record(StreamName="s", Data=b"", PartitionKey="k")
'''
    assert _calls(_parse(src)) == []


DYNAMO = '''import boto3

TABLE = boto3.resource("dynamodb").Table("users")


class Users:
    def __init__(self):
        self.client = boto3.client("dynamodb")
        self.db = boto3.resource("dynamodb")

    def save(self, user, cache):
        TABLE.put_item(Item=user)
        self.client.get_item(TableName="users", Key={})
        self.client.execute_statement(Statement="SELECT * FROM users")
        self.db.batch_get_item(RequestItems={})
        self.db.Table("orders").query(KeyConditionExpression=k)
        cache.put_item(user)
        self.client.create_table(TableName="t")
        self.client.describe_table(TableName="t")
        self.db.Table("orders").batch_writer()
'''


def test_dynamodb_is_data_access_as_in_csharp() -> None:
    rec = _parse(DYNAMO)
    got = [(s.startLine, s.semanticType, s.method, s.dataAccessHint, s.framework, s.endpoint)
           for s in rec.statements if s.dataAccessHint == "dynamodb"]
    assert got == [
        (12, "db_method_call", "put_item", "dynamodb", None, None),
        (13, "db_method_call", "get_item", "dynamodb", None, None),
        (14, "db_method_call", "execute_statement", "dynamodb", None, None),
        (15, "db_method_call", "batch_get_item", "dynamodb", None, None),
        (16, "db_method_call", "query", "dynamodb", None, None),
        # a parameter's put_item, and table management, are not DynamoDB data access
    ]
    assert _calls(rec) == []


def test_address_only_from_one_literal_or_module_constant() -> None:
    src = f'''import boto3

QUEUE_URL = "{_QUEUE}"
sqs = boto3.client("sqs")


def local():
    url = "{_QUEUE}"
    sqs.send_message(QueueUrl=url, MessageBody="b")


def shadowed(QUEUE_URL):
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody="b")


def formatted(name):
    sqs.send_message(QueueUrl=f"https://sqs.eu-west-1.amazonaws.com/1/{{name}}", MessageBody="b")


def two_buses():
    boto3.client("events").put_events(Entries=[{{"EventBusName": "a"}}, {{"EventBusName": "b"}}])
'''
    assert [c[4] for c in _calls(_parse(src))] == [None, None, None, None]


WORKER = f'''import boto3

QUEUE_URL = "{_QUEUE}"
sqs = boto3.client("sqs")


class Worker:
    def poll(self):
        while True:
            batch = sqs.receive_message(QueueUrl=QUEUE_URL, WaitTimeSeconds=20)

    def drain(self, url):
        sqs.receive_message(QueueUrl=url)


def queue_worker():
    for m in boto3.resource("sqs").Queue(QUEUE_URL).receive_messages():
        pass


handle = lambda: sqs.receive_message(QueueUrl=QUEUE_URL)
sqs.receive_message(QueueUrl=QUEUE_URL)
'''


def test_sqs_receive_is_a_consumer_of_the_function_it_is_in() -> None:
    got = [(s.startLine, s.semanticType, s.method, s.endpoint, s.handler, s.handlerLine)
           for s in _parse(WORKER).statements if s.semanticType in _AWS]
    assert got == [
        (10, "eventbus_consumer", "CONSUMER", "orders", "poll", 8),
        # the queue URL is a parameter: still the queue's consumer, address unknown
        (13, "eventbus_consumer", "CONSUMER", None, "drain", 12),
        (17, "eventbus_consumer", "CONSUMER", "orders", "queue_worker", 16),
        # an inline lambda is anonymous: its line, no name
        (21, "eventbus_consumer", "CONSUMER", "orders", None, 21),
        # module-level code: no function to name
        (22, "eventbus_consumer", "CONSUMER", "orders", None, None),
    ]
    assert all(s.framework == "aws-sqs" for s in _parse(WORKER).statements if s.semanticType in _AWS)


def test_fixture_files_and_capture_off_are_skipped() -> None:
    assert _calls(_parse(CLIENTS, "tests/fixtures/store.py")) == []
    assert _calls(_parse(CLIENTS, capture=False)) == []


def test_records_meet_the_entry_point_contract(analyze_files) -> None:
    stmts = analyze_files({"svc/store.py": CLIENTS, "svc/worker.py": WORKER,
                           "svc/res.py": RESOURCES, "svc/bind.py": BINDINGS})
    assert sum(1 for s in stmts if s.semanticType == "eventbus_consumer") == 5
