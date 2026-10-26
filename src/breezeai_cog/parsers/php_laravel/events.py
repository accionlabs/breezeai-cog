"""Laravel's event and command buses (``illuminate/events``, ``illuminate/bus``) as an additive
detector.

The buses are standalone packages: a Slim or plain-PHP app can use them in files no Laravel
parser owns. So they are detected here, for every PHP file, whichever framework parser won
the file (skill §3: entry points that can appear in another framework's files are additive).

==========================================  ==============================================
Code                                        Record
==========================================  ==============================================
``Event::listen('order.shipped', L::class)``  ``eventbus_consumer``, ``CONSUMER``, handler
                                            ``L@handle`` (``[L::class, 'm']`` → ``L@m``;
                                            a closure → its start line)
``Event::dispatch('order.shipped')`` /      ``eventbus_publish``, endpoint = the literal
``event('order.shipped')``                  event name, else empty
``Bus::dispatch(new Job)`` / ``dispatch()``   ``eventbus_send``, empty endpoint
``Event::subscribe(S::class)``              none — a subscriber registers many handlers
                                            (wiring)
==========================================  ==============================================

Statements carry ``framework: laravel``; the file's own label stays with its parser.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas.enums import SemanticType
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..php_route_common import (
    PHP_CALL_TYPES, args_of, callback_handler, method_of, string_value, unwrap, walk,
)
from ..treesitter import node_text

_EVENT_MARKERS = (
    b"illuminate\\contracts\\events\\dispatcher",
    b"illuminate\\events\\dispatcher",
    b"illuminate\\support\\facades\\event",
    b"namespace illuminate\\events",
)
_BUS_MARKERS = (
    b"illuminate\\contracts\\bus\\dispatcher",
    b"illuminate\\bus\\dispatcher",
    b"illuminate\\support\\facades\\bus",
    b"illuminate\\foundation\\bus",
    b"namespace illuminate\\bus",
)
_DISPATCH = {"dispatch", "until", "dispatchafterresponse", "dispatchsync", "dispatchnow",
             "dispatchtoqueue"}


def _last_name(value: str) -> str:
    return value.rsplit("\\", 1)[-1].rsplit("->", 1)[-1].lstrip("$").lower()


def _kind(call: Node, source: bytes, low: bytes, events: bool,
          bus: bool) -> SemanticType | None:
    method = method_of(call, source)
    if method is None and call.type == "function_call_expression":
        function = call.child_by_field_name("function")
        method = node_text(function, source).rsplit("\\", 1)[-1].lower() if function else None
    if method is None:
        return None
    if call.type == "function_call_expression":
        if method == "event" and events:
            return "eventbus_publish"
        if method == "dispatch" and bus:
            return "eventbus_send"
        return None
    receiver = call.child_by_field_name("scope") or call.child_by_field_name("object")
    scoped = _last_name(node_text(receiver, source)) if receiver is not None else ""
    in_events = scoped in {"this", "self"} and b"namespace illuminate\\events" in low
    in_bus = scoped in {"this", "self"} and b"namespace illuminate\\bus" in low
    if method == "listen" and events and (scoped in {"event", "events", "dispatcher"} or in_events):
        return "eventbus_consumer"
    if method in _DISPATCH:
        if events and (scoped in {"event", "events", "eventdispatcher"} or in_events):
            return "eventbus_publish"
        if bus and (scoped in {"bus", "dispatcher"} or in_bus):
            return "eventbus_send"
    return None


def _listener(node: Node | None, source: bytes) -> tuple[str | None, int | None]:
    """A listener as Laravel calls it: a class alone is its ``handle`` method."""
    value = unwrap(node)
    if value is None:
        return None, None
    if value.type == "class_constant_access_expression":
        written = node_text(value, source)
        if not written.lower().endswith("::class"):
            return None, None
        cls = written.rsplit("::", 1)[0].lstrip("\\")
        return f"{cls}@handle", None
    handler, line = callback_handler(value, source)
    if handler is not None and "@" not in handler and string_value(value, source) is not None:
        handler = f"{handler}@handle"  # 'App\Listeners\SendMail' names a listener class
    return handler, line


def detect_laravel_events(dc: DetectContext) -> Detection | None:
    source = dc.source
    low = source.lower()
    # A grouped facade import (``use Illuminate\Support\Facades\{Route, Event};``) has no
    # ``facades\event`` substring: the facade prefix plus a static call stands in for it.
    facades = b"illuminate\\support\\facades\\" in low
    events = any(m in low for m in _EVENT_MARKERS) or (facades and b"event::" in low)
    bus = any(m in low for m in _BUS_MARKERS) or (facades and b"bus::" in low)
    if not events and not bus:
        return None
    found: list[Emit] = []
    for call in walk(dc.root):
        if call.type not in PHP_CALL_TYPES:
            continue
        semantic = _kind(call, source, low, events, bus)
        if semantic is None:
            continue
        args = args_of(call)
        fields: dict[str, object] = {
            "semanticType": semantic, "framework": "laravel",
            "method": "CONSUMER" if semantic == "eventbus_consumer" else None,
            # The event name is the first argument; a later string is payload, not an address.
            "endpoint": string_value(args[0], source) if args and semantic != "eventbus_send" else None,
        }
        if semantic == "eventbus_consumer":
            fields["handler"], fields["handlerLine"] = _listener(
                args[1] if len(args) > 1 else None, source)
        found.append(Emit(call, fields, enrich="own"))
    return Detection(found) if found else None


register_detector(Detector(
    name="laravel-events",
    gate=Gate("php", order=20, guard=b"lluminate\\", skip_fixtures=True),
    detect=detect_laravel_events,
    frameworks=("laravel",),
))
