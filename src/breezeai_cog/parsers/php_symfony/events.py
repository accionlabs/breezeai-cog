"""Symfony's EventDispatcher and Messenger components as an additive detector.

Both components are standalone packages (``symfony/event-dispatcher``, ``symfony/messenger``)
used well outside Symfony apps — Drupal, Slim, plain PHP. So they are detected here, for every
PHP file, whichever framework parser owns it; importing them no longer makes a file "Symfony"
(skill §3: entry points that can appear in another framework's files are additive).

==========================================  ==============================================
Code                                        Record
==========================================  ==============================================
``$dispatcher->addListener('x', $cb)``      ``eventbus_consumer``, ``CONSUMER``, endpoint
                                            ``x``, handler from the callback
``$dispatcher->dispatch($event, 'x')``      ``eventbus_publish``, endpoint ``x`` (the event
                                            name argument), else empty
``$bus->dispatch(new Message())``           ``eventbus_send`` (Messenger), empty endpoint
``$dispatcher->addSubscriber($s)``          none — a subscriber registers many handlers
                                            (wiring)
==========================================  ==============================================

Statements carry ``framework: symfony``; the file's own label stays with its parser.
"""

from __future__ import annotations

import re

from ...schemas.enums import SemanticType
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..php_route_common import args_of, callback_handler, string_value, walk
from ..treesitter import node_text

_CALL_TYPES = {"member_call_expression", "nullsafe_member_call_expression", "scoped_call_expression"}


def detect_symfony_events(dc: DetectContext) -> Detection | None:
    source = dc.source
    if b"ispatch" not in source and b"Listener" not in source:
        return None  # cheap pre-check: neither dispatch() nor addListener() is called
    low = source.lower()
    has_dispatcher = b"eventdispatcher" in low or b"event_dispatcher" in low
    has_messenger = b"messagebus" in low or b"symfony\\component\\messenger" in low
    if not has_dispatcher and not has_messenger:
        return None
    found: list[Emit] = []
    for call in walk(dc.root):
        if call.type not in _CALL_TYPES:
            continue
        name = call.child_by_field_name("name")
        method = node_text(name, source).lower() if name is not None else ""
        if method not in {"dispatch", "addlistener"}:
            continue
        receiver_node = call.child_by_field_name("object") or call.child_by_field_name("scope")
        receiver = node_text(receiver_node, source).lower() if receiver_node is not None else ""
        terminal = re.split(r"->|::", receiver)[-1].replace("$", "")
        if terminal in {"this", "self", "static"}:
            continue
        is_dispatcher = has_dispatcher and ("dispatcher" in receiver or terminal in {
            "dispatcher", "eventdispatcher", "eventdispatcherinterface"})
        is_messenger = has_messenger and ("messagebus" in receiver or terminal in {
            "bus", "messagebus", "eventbus"})
        args = args_of(call)
        semantic: SemanticType
        fields: dict[str, object] = {}
        if method == "dispatch" and is_messenger:
            semantic, endpoint = "eventbus_send", None
        elif method == "dispatch" and is_dispatcher:
            semantic = "eventbus_publish"
            endpoint = string_value(args[1], source) if len(args) > 1 else None
        elif method == "addlistener" and is_dispatcher:
            semantic = "eventbus_consumer"
            endpoint = string_value(args[0], source) if args else None
            fields["handler"], fields["handlerLine"] = (
                callback_handler(args[1], source) if len(args) > 1 else (None, None))
        else:
            continue
        fields.update({
            "semanticType": semantic, "framework": "symfony", "endpoint": endpoint,
            "method": "CONSUMER" if semantic == "eventbus_consumer" else None,
        })
        found.append(Emit(call, fields, enrich="own"))
    return Detection(found) if found else None


register_detector(Detector(
    name="symfony-events",
    gate=Gate("php", order=30, skip_fixtures=True),
    detect=detect_symfony_events,
    frameworks=("symfony",),
))
