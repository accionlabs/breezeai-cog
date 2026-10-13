"""Lambda triggers declared in deployment config, as a registered fact, ``aws.deployed-handlers``.

A Lambda handler's trigger is often written only in the deployment config — ``serverless.yml``
or a SAM template (``AWS::Serverless::Function`` resources in ``template.yaml`` or the JSON
``serverless.template``; ``Handler`` relative to ``CodeUri``, ``Globals.Function`` filling in)::

    # serverless.yml
    functions:
      onOrder:
        handler: src/handler.onOrder                       # src/handler.ts, export onOrder
        events:
          - sqs: arn:aws:sqs:eu-west-1:123456789012:orders
          - http: { path: users/{id}, method: get }
      report:
        handler: Jobs::Jobs.Function::Report               # .NET: Assembly::Type::Method

The config is wiring (contract §4): each event is a binding of the handler it names, and its
address is copied onto the handler's record — the record stays at the handler in code. One
record per event: the first event's fields go on the handler's record, each further event adds
a record on the same handler.

=================================  ====================  =========================================
Event                              Record                ``endpoint``
=================================  ====================  =========================================
``sqs``                            eventbus_consumer     the queue name, from a literal ARN
``sns``                            eventbus_consumer     the topic name, or from a literal ARN
``stream`` (kinesis / dynamodb)    eventbus_consumer     the stream / table name, from a literal ARN
``eventBridge``                    eventbus_consumer     the event bus, when named literally
``schedule`` / ``eventBridge``     timer, ``PERIODIC``   the expression as written (``rate(1 day)``)
  with a schedule
``http`` / ``httpApi``             route                 ``/path`` (verb in ``method``; ``*`` →
                                                         ``ANY``)
=================================  ====================  =========================================

SAM spells the same events ``SQS`` / ``SNS`` / ``Kinesis`` / ``DynamoDB`` / ``Schedule`` /
``ScheduleV2`` / ``EventBridgeRule`` / ``CloudWatchEvent`` / ``Api`` / ``HttpApi`` (an
``HttpApi`` without a path is the catch-all route: ``ANY``, no path).

Anything the config leaves to deploy time — a variable (``${self:custom.queue}``), a CloudFormation
reference (``Fn::GetAtt``, ``!Ref``), a catch-all path — gives an empty endpoint, never its text.
Other event types (``s3``, ``websocket``, ``cognitoUserPool`` …) add nothing.

The handler is matched by name: ``src/handler.onOrder`` (relative to the config's folder) is the
export ``onOrder`` of ``src/handler.{ts,js,…}``; ``Assembly::Namespace.Type::Method`` is method
``Method`` of type ``Namespace.Type``. A handler in built output (``dist/…``) matches no source
file, and nothing is added.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .additive import FactSpec, index_fact, register_fact
from .typescript.reexports import ModuleExports, collect_exports, declaring

FACT = "aws.deployed-handlers"

#: A ``serverless.yml``: a top-level ``functions:`` with a ``handler:`` under it.
_SERVERLESS = re.compile(rb"(?m)^functions:\s*$")
_HANDLER = re.compile(rb"(?m)^\s+handler:")
_ARN = re.compile(r"^arn:aws[\w-]*:(?P<service>[\w-]+):[\w-]*:\d*:(?P<resource>.+)$")


@dataclass(frozen=True)
class Trigger:
    """One event of a deployed handler, as the fields of its record."""

    semantic_type: str
    framework: str
    method: str | None = None
    endpoint: str | None = None
    route_kind: str | None = None

    def fields(self) -> dict[str, Any]:
        return {"semanticType": self.semantic_type, "framework": self.framework,
                "method": self.method, "endpoint": self.endpoint, "routeKind": self.route_kind}


@dataclass(frozen=True)
class DeployedHandlers:
    #: TypeScript / JavaScript: source path without extension → export name → its triggers.
    modules: dict[str, dict[str, tuple[Trigger, ...]]] = field(default_factory=dict)
    #: .NET: ``Namespace.Type`` → method → its triggers.
    types: dict[str, dict[str, tuple[Trigger, ...]]] = field(default_factory=dict)


def deployed_handlers(index: Any | None) -> DeployedHandlers | None:
    fact: DeployedHandlers | None = index_fact(index, FACT)
    return fact


def module_triggers(index: Any | None, path: str, name: str) -> tuple[Trigger, ...]:
    """Triggers of export ``name`` of the TypeScript / JavaScript file ``path``."""
    fact = deployed_handlers(index)
    stem = str(PurePosixPath(path).with_suffix(""))
    return fact.modules.get(stem, {}).get(name, ()) if fact is not None else ()


def type_triggers(index: Any | None, type_name: str, method: str) -> tuple[Trigger, ...]:
    """Triggers of method ``method`` of the .NET type ``type_name`` (``Namespace.Type``)."""
    fact = deployed_handlers(index)
    return fact.types.get(type_name, {}).get(method, ()) if fact is not None else ()


# ── reading ────────────────────────────────────────────────────────────────────────────────

class _Ref:
    """A CloudFormation tag (``!Ref``, ``!GetAtt``, ``!Sub`` …): a deploy-time value."""


def _load(source: bytes) -> Any:
    import yaml  # type: ignore[import-untyped]

    base: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

    class Loader(base):  # type: ignore[misc]
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: _Ref())
    try:
        return yaml.load(source, Loader=Loader)  # noqa: S506 — a SafeLoader subclass
    except yaml.YAMLError:
        return None


def _literal(value: Any) -> str | None:
    """A plain string the config fixes; a variable or reference → None."""
    if not isinstance(value, str) or "${" in value or not value.strip():
        return None
    return value.strip()


def _arn_resource(value: Any, service: str) -> str | None:
    """The resource of a literal ARN of ``service`` (``arn:aws:sqs:r:1:orders`` → ``orders``)."""
    text = _literal(value)
    m = _ARN.match(text) if text is not None else None
    return m["resource"] if m is not None and m["service"] == service else None


def _sqs(spec: Any) -> Trigger:
    arn = spec.get("arn") if isinstance(spec, dict) else spec
    return Trigger("eventbus_consumer", "aws-sqs", endpoint=_arn_resource(arn, "sqs"))


def _sns(spec: Any) -> Trigger:
    if isinstance(spec, dict):
        name = _literal(spec.get("topicName")) or _arn_resource(spec.get("arn"), "sns")
    else:
        text = _literal(spec)
        name = _arn_resource(text, "sns") if text and text.startswith("arn:") else text
    return Trigger("eventbus_consumer", "aws-sns", endpoint=name)


def _stream(spec: Any) -> Trigger | None:
    arn = spec.get("arn") if isinstance(spec, dict) else spec
    kind = spec.get("type") if isinstance(spec, dict) else None
    text = _literal(arn) or ""
    if kind == "kinesis" or ":kinesis:" in text:
        resource = _arn_resource(arn, "kinesis")  # stream/orders
        return Trigger("eventbus_consumer", "aws-kinesis",
                       endpoint=resource.split("/", 1)[1] if resource and "/" in resource else None)
    if kind == "dynamodb" or ":dynamodb:" in text:
        resource = _arn_resource(arn, "dynamodb")  # table/Orders/stream/2024-…
        parts = resource.split("/") if resource else []
        return Trigger("eventbus_consumer", "aws-dynamodb",
                       endpoint=parts[1] if len(parts) > 1 and parts[0] == "table" else None)
    return None


def _schedules(spec: Any) -> list[Trigger]:
    rates = spec.get("rate") if isinstance(spec, dict) else spec
    rates = rates if isinstance(rates, list) else [rates]
    return [Trigger("timer", "aws-eventbridge", "PERIODIC", _literal(r)) for r in rates]


def _event_bridge(spec: Any) -> list[Trigger]:
    if not isinstance(spec, dict):
        return []
    if spec.get("schedule") is not None:
        return _schedules(spec["schedule"])
    bus = spec.get("eventBus")
    name = _arn_resource(bus, "events") if isinstance(bus, str) and bus.startswith("arn:") \
        else _literal(bus)
    if name is not None and name.startswith("event-bus/"):
        name = name.removeprefix("event-bus/")
    return [Trigger("eventbus_consumer", "aws-eventbridge", endpoint=name)]


def _http(spec: Any) -> Trigger:
    method: str | None = None
    path: str | None = None
    if isinstance(spec, str):
        if spec.strip() == "*":
            method = "*"
        else:
            parts = spec.split(None, 1)
            method, path = (parts[0], parts[1]) if len(parts) == 2 else (None, None)
    elif isinstance(spec, dict):
        method, path = spec.get("method"), spec.get("path")
    verb = _literal(method)
    verb = "ANY" if verb in ("*", "any", "ANY") else verb.upper() if verb else None
    text = _literal(path)
    endpoint = "/" + text.strip("/") if text is not None and text != "*" else None
    return Trigger("route", "aws-apigw", verb, endpoint, "route")


def _triggers(events: Any) -> tuple[Trigger, ...]:
    out: list[Trigger] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or len(event) != 1:
            continue
        (kind, spec), = event.items()
        if kind == "sqs":
            out.append(_sqs(spec))
        elif kind == "sns":
            out.append(_sns(spec))
        elif kind == "stream":
            t = _stream(spec)
            if t is not None:
                out.append(t)
        elif kind == "schedule":
            out.extend(_schedules(spec))
        elif kind == "eventBridge":
            out.extend(_event_bridge(spec))
        elif kind in ("http", "httpApi"):
            out.append(_http(spec))
    return tuple(out)


@dataclass(frozen=True)
class _Function:
    handler: str  # as written
    base: str  # the config's folder, repo-relative ("" at the root)
    triggers: tuple[Trigger, ...]


def read_serverless(source: bytes, rel: str) -> list[_Function]:
    """The functions of a ``serverless.yml`` with a literal handler."""
    if not _SERVERLESS.search(source) or not _HANDLER.search(source):
        return []
    doc = _load(source)
    functions = doc.get("functions") if isinstance(doc, dict) else None
    if not isinstance(functions, dict):
        return []
    base = str(PurePosixPath(rel).parent)
    base = "" if base == "." else base
    out: list[_Function] = []
    for spec in functions.values():
        handler = _literal(spec.get("handler")) if isinstance(spec, dict) else None
        if handler is not None:
            out.append(_Function(handler, base, _triggers(spec.get("events"))))
    return out


def _module_key(fn: _Function) -> tuple[str, str] | None:
    """``src/handler.onOrder`` → (``<base>/src/handler``, ``onOrder``)."""
    if "::" in fn.handler or "." not in fn.handler:
        return None
    module, name = fn.handler.rsplit(".", 1)
    stem = str(PurePosixPath(fn.base) / module.lstrip("./")) if fn.base else module.lstrip("./")
    return stem, name


def _type_key(fn: _Function) -> tuple[str, str] | None:
    """``Assembly::Namespace.Type::Method`` → (``Namespace.Type``, ``Method``)."""
    parts = fn.handler.split("::")
    return (parts[1], parts[2]) if len(parts) == 3 and parts[1] and parts[2] else None


def _resolve(values: list[Any], index: object) -> DeployedHandlers | None:
    functions = [v for v in values if isinstance(v, list)]
    exports = {v.stem: v for v in values if isinstance(v, ModuleExports)}
    modules: dict[str, dict[str, tuple[Trigger, ...]]] = {}
    types: dict[str, dict[str, tuple[Trigger, ...]]] = {}

    def add(table: dict[str, dict[str, tuple[Trigger, ...]]], key: tuple[str, str],
            triggers: tuple[Trigger, ...]) -> None:
        # Two triggers the config leaves identical (two queues by reference: no name) cannot be
        # told apart, so they are one record.
        known = table.setdefault(key[0], {}).get(key[1], ())
        table[key[0]][key[1]] = tuple(dict.fromkeys((*known, *triggers)))

    for per_file in sorted(functions, key=lambda fs: [(f.base, f.handler) for f in fs]):
        for fn in per_file:
            module = _module_key(fn)
            if module is not None:
                # handler.sender may only re-export sender (`export { sender } from
                # './sqs/sender'`): the triggers go to the file that declares it.
                add(modules, declaring(exports, *module) or module, fn.triggers)
            dotnet = _type_key(fn)
            if dotnet is not None:
                add(types, dotnet, fn.triggers)
    fact = DeployedHandlers(modules, types)
    return fact if modules or types else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sam_triggers(events: Any) -> tuple[Trigger, ...]:
    """SAM ``Events`` (``{name: {Type, Properties}}``) as triggers — the same rules as
    ``serverless.yml`` events, spelled the SAM way."""
    out: list[Trigger] = []
    for event in events.values() if isinstance(events, dict) else []:
        if not isinstance(event, dict):
            continue
        kind = event.get("Type")
        props = _dict(event.get("Properties"))
        if kind == "SQS":
            out.append(_sqs(props.get("Queue")))
        elif kind == "SNS":
            topic = props.get("Topic")
            out.append(_sns(topic if isinstance(topic, str) and topic.startswith("arn:") else None))
        elif kind == "Kinesis":
            out.append(_stream({"type": "kinesis", "arn": props.get("Stream")}) or
                       Trigger("eventbus_consumer", "aws-kinesis"))
        elif kind == "DynamoDB":
            out.append(_stream({"type": "dynamodb", "arn": props.get("Stream")}) or
                       Trigger("eventbus_consumer", "aws-dynamodb"))
        elif kind == "Schedule":
            out.extend(_schedules(props.get("Schedule")))
        elif kind == "ScheduleV2":
            out.extend(_schedules(props.get("ScheduleExpression")))
        elif kind in ("EventBridgeRule", "CloudWatchEvent"):
            out.extend(_event_bridge({"eventBus": props.get("EventBusName")}))
        elif kind in ("Api", "HttpApi"):
            # HttpApi without a path is the catch-all ($default) route: every verb, any path.
            out.append(_http({"path": props.get("Path"), "method": props.get("Method")})
                       if props.get("Path") is not None else _http("*"))
    return tuple(out)


def _code_dir(template_dir: str, code_uri: Any) -> str | None:
    """The folder a SAM function's ``Handler`` is relative to: ``CodeUri`` (a local folder) under
    the template's folder; an S3 location or a reference → None (not in the repository)."""
    if code_uri is None:
        return template_dir
    text = _literal(code_uri)
    if text is None or text.startswith("s3://") or text.endswith((".zip", ".jar")):
        return None
    joined = PurePosixPath(template_dir) / text.strip("/") if template_dir else PurePosixPath(text.strip("/"))
    return "" if str(joined) in (".", "") else str(joined)


def read_sam(source: bytes, rel: str) -> list[_Function]:
    """The ``AWS::Serverless::Function`` resources of a SAM / CloudFormation template (YAML, or
    JSON as in ``serverless.template``) with a literal ``Handler``; ``Globals.Function`` supplies
    a missing ``Handler`` / ``CodeUri``."""
    if b"AWS::Serverless::Function" not in source:
        return []
    if rel.lower().endswith((".json", ".template")):
        import json

        try:
            doc = json.loads(source)
        except ValueError:
            return []
    else:
        doc = _load(source)
    resources = doc.get("Resources") if isinstance(doc, dict) else None
    if not isinstance(resources, dict):
        return []
    globals_ = _dict(_dict(doc.get("Globals")).get("Function"))
    template_dir = str(PurePosixPath(rel).parent)
    template_dir = "" if template_dir == "." else template_dir
    out: list[_Function] = []
    for resource in resources.values():
        if not isinstance(resource, dict) or resource.get("Type") != "AWS::Serverless::Function":
            continue
        props = _dict(resource.get("Properties"))
        handler = _literal(props.get("Handler", globals_.get("Handler")))
        base = _code_dir(template_dir, props.get("CodeUri", globals_.get("CodeUri")))
        if handler is not None and base is not None:
            out.append(_Function(handler, base, _sam_triggers(props.get("Events"))))
    return out


_SOURCES = (".yml", ".yaml", ".json", ".template")


_TS_FILES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _collect(root: Any, source: bytes, rel: str,
             repo_root: Path | None) -> list[_Function] | ModuleExports | None:
    """Deployment config: its functions. A TypeScript / JavaScript file (the index's own): its
    exports, to follow a configured handler through re-exports to the file declaring it."""
    if rel.lower().endswith(_TS_FILES):
        return collect_exports(root, source, rel, repo_root) if root is not None else None
    if not rel.lower().endswith(_SOURCES):
        return None
    return (read_serverless(source, rel) + read_sam(source, rel)) or None


# The same fact on both indexes: TypeScript reads the module handlers, C# the .NET ones.
for _language in ("typescript", "csharp"):
    register_fact(FactSpec(
        name=FACT, language=_language,
        gate=(b"functions:", b"AWS::Serverless::Function", b"export"),
        sources=_SOURCES,
        collect=_collect, resolve=_resolve,
    ))
