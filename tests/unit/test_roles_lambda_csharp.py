"""Role-table tests for C# AWS Lambda handlers (docs/entry-point-roles/lambda-csharp.md)."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}


def _handler(event_type: str, ns: str = "Amazon.Lambda.SQSEvents") -> str:
    return f"""using Amazon.Lambda.Core;
using {ns};

namespace Jobs;

public class Function
{{
    public Task FunctionHandler({event_type} evt, ILambdaContext context) => Task.CompletedTask;

    public void Report(string message, ILambdaContext context) {{ }}
}}
"""


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def test_event_handler_is_binding(analyze_files) -> None:
    stmts = analyze_files({"Function.cs": _handler("SQSEvent")})
    [r] = _eps(stmts)
    assert r.semanticType == "eventbus_consumer"
    assert (r.handler, r.handlerLine) == ("FunctionHandler", 8)


@pytest.mark.xfail(strict=True, reason="framework is aws-lambda, not the trigger's service")
def test_event_handler_framework_is_trigger(analyze_files) -> None:
    stmts = analyze_files({"Function.cs": _handler("SQSEvent")})
    [r] = _eps(stmts)
    assert r.framework == "aws-sqs"


@pytest.mark.xfail(strict=True, reason="endpoint holds the method name instead of the queue")
def test_event_handler_endpoint_not_code_name(analyze_files) -> None:
    stmts = analyze_files({"Function.cs": _handler("SQSEvent")})
    [r] = _eps(stmts)
    assert r.endpoint in (None, "")


def test_http_event_handler_is_route(analyze_files) -> None:
    stmts = analyze_files(
        {"Function.cs": _handler("APIGatewayProxyRequest", "Amazon.Lambda.APIGatewayEvents")}
    )
    [r] = _eps(stmts)
    assert (r.semanticType, r.routeKind, r.handler) == ("route", "route", "FunctionHandler")


@pytest.mark.xfail(strict=True, reason="scheduled event typed eventbus_consumer, not timer")
def test_scheduled_event_handler_is_timer(analyze_files) -> None:
    stmts = analyze_files(
        {
            "Function.cs": _handler(
                "ScheduledEvent", "Amazon.Lambda.CloudWatchEvents.ScheduledEvents"
            )
        }
    )
    [r] = _eps(stmts)
    assert r.semanticType == "timer"


def test_context_only_helper_is_none(analyze_files) -> None:
    stmts = analyze_files({"Function.cs": _handler("SQSEvent")})
    assert [s for s in _eps(stmts) if s.handler == "Report"] == []


@pytest.mark.xfail(strict=True, reason="deployment-config trigger (queue name) not read")
def test_deployment_config_is_wiring(analyze_files) -> None:
    template = """{
  "Resources": {
    "Worker": {
      "Type": "AWS::Serverless::Function",
      "Properties": {
        "Handler": "Jobs::Jobs.Function::FunctionHandler",
        "Events": {"Q": {"Type": "SQS", "Properties": {"Queue": "arn:aws:sqs:eu-west-1:1:orders"}}}
      }
    }
  }
}
"""
    stmts = analyze_files({"Function.cs": _handler("SQSEvent"), "serverless.template": template})
    [r] = _eps(stmts)
    assert r.endpoint == "orders"
