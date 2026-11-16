"""AWS SDK messaging addresses, shared by the TypeScript, C# and VB AWS detectors, and the AWS SDK
for .NET service tables, shared by the C# and VB detectors (one SDK, one set of rules).

A producer call names its destination in a request field (``TopicArn``, ``QueueUrl``,
``EventBusName``, ``PhoneNumber``). A literal address is written by name — a queue URL as its
queue, a topic or bus ARN as its topic or bus — the form a consumer records, so producer and
consumer meet on one address. An injected value is never resolved here."""

from __future__ import annotations

import re

from ..schemas import SemanticType

# Keys in an SDK argument object that name the destination address.
ADDRESS_KEYS = {"TopicArn", "TargetArn", "QueueUrl", "EventBusName", "PhoneNumber"}
# Subset of the above that also *proves* the call is AWS — needed where the publish verb is
# generic (the TypeScript SDK v2 ``publish``). ``PhoneNumber`` is excluded: every SMS vendor
# (Twilio, Vonage, MessageBird) names that field the same way, so it identifies a destination
# but not a provider. The remaining keys are AWS-specific strings, so for them naming a
# destination and proving AWS coincide.
AWS_EVIDENCE_KEYS = ADDRESS_KEYS - {"PhoneNumber"}
# Address keys that reach exactly one recipient. SNS names its API ``Publish`` for every
# destination, but only a *topic* fans out, so an SMS is point-to-point — the distinction
# Vert.x draws between ``publish()`` and ``send()``. Nothing can subscribe to a phone
# number, so calling it a publish would assert a one-to-many relationship that cannot exist.
#
# ``TargetArn`` is deliberately absent: the Publish API accepts *either* a platform-endpoint
# ARN (one device) *or* a topic ARN (fan-out) there, and the value is usually an injected
# symbol, so its cardinality is not knowable from the call site. It keeps the fan-out
# default rather than being guessed — a coarse classification beats a wrong one.
DIRECT_ADDRESS_KEYS = {"PhoneNumber"}

#: A literal SQS queue URL — ``https://sqs.<region>.amazonaws.com/<account>/<queue>`` or the legacy
#: ``https://<region>.queue.amazonaws.com/<account>/<queue>``.
_QUEUE_URL = re.compile(
    r"^https?://(?:sqs\.[\w-]+|[\w-]+\.queue)\.amazonaws\.com(?:\.cn)?/\d+/(?P<name>[\w.-]+)/?$"
)
#: A literal SNS topic / EventBridge bus ARN.
_TOPIC_ARN = re.compile(r"^arn:aws[\w-]*:sns:[\w-]+:\d+:(?P<name>[\w.-]+)$")
_BUS_ARN = re.compile(r"^arn:aws[\w-]*:events:[\w-]+:\d+:event-bus/(?P<name>[\w.-]+)$")
_NAME_FORMS = {"QueueUrl": _QUEUE_URL, "TopicArn": _TOPIC_ARN, "EventBusName": _BUS_ARN}


def address_name(key: str, value: str) -> str:
    """The name a literal address stands for, written as its consumer writes it: a queue URL →
    the queue (``…amazonaws.com/123/orders`` → ``orders``), a topic or bus ARN → the topic /
    bus. Anything not exactly in that form is kept as written."""
    form = _NAME_FORMS.get(key)
    m = form.match(value) if form is not None else None
    return m["name"] if m is not None else value


# ── AWS SDK for .NET (C# and VB) ──────────────────────────────────────────────────────────────

#: Client types → service (the ``framework`` value).
DOTNET_CLIENT_TYPES = {
    "IAmazonS3": "aws-s3", "AmazonS3Client": "aws-s3",
    "ITransferUtility": "aws-s3", "TransferUtility": "aws-s3",
    "IAmazonSimpleSystemsManagement": "aws-ssm", "AmazonSimpleSystemsManagementClient": "aws-ssm",
    "IAmazonCognitoIdentityProvider": "aws-cognito",
    "AmazonCognitoIdentityProviderClient": "aws-cognito",
    "IAmazonLambda": "aws-lambda", "AmazonLambdaClient": "aws-lambda",
    "IAmazonSimpleNotificationService": "aws-sns", "AmazonSimpleNotificationServiceClient": "aws-sns",
    "IAmazonSQS": "aws-sqs", "AmazonSQSClient": "aws-sqs",
    "IAmazonEventBridge": "aws-eventbridge", "AmazonEventBridgeClient": "aws-eventbridge",
    "IAmazonCloudWatchEvents": "aws-eventbridge", "AmazonCloudWatchEventsClient": "aws-eventbridge",
}

#: Services whose every operation is a request/response call.
DOTNET_API_SERVICES = frozenset({"aws-s3", "aws-ssm", "aws-cognito", "aws-lambda"})

#: Messaging operations (sync and ``Async``) → semantic type, per service.
DOTNET_MESSAGING: dict[str, dict[str, SemanticType]] = {
    "aws-sns": {"Publish": "eventbus_publish", "PublishBatch": "eventbus_publish"},
    "aws-sqs": {"SendMessage": "eventbus_send", "SendMessageBatch": "eventbus_send",
                "ReceiveMessage": "eventbus_consumer"},
    "aws-eventbridge": {"PutEvents": "eventbus_publish"},
}

#: The address the positional overload's first argument stands for
#: (``PublishAsync(topicArn, message)``, ``SendMessageAsync(queueUrl, body)``).
DOTNET_POSITIONAL_KEY = {"aws-sns": "TopicArn", "aws-sqs": "QueueUrl"}

#: Client methods that are not service operations: local signing, lifetime, ``object``.
DOTNET_NOT_OPERATIONS = frozenset({
    "Dispose", "DetermineServiceOperationEndpoint", "GetPreSignedURL", "GetPreSignedURLAsync",
    "Equals", "GetHashCode", "ToString", "GetType",
})
