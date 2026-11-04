"""AWS SDK messaging addresses, shared by the TypeScript and C# AWS detectors.

A producer call names its destination in a request field (``TopicArn``, ``QueueUrl``,
``EventBusName``, ``PhoneNumber``). A literal address is written by name — a queue URL as its
queue, a topic or bus ARN as its topic or bus — the form a consumer records, so producer and
consumer meet on one address. An injected value is never resolved here."""

from __future__ import annotations

import re

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
