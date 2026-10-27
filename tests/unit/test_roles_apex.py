"""Apex role table: docs/entry-point-roles/apex.md — one test per row."""

from __future__ import annotations

import gzip
import json

import pytest

from breezeai_cog.parsers.contract import is_entry_point

CLS = "force-app/main/default/classes/Svc.cls"
JOB = "force-app/main/default/classes/NightlySync.cls"
NIGHTLY = """global class NightlySync implements Schedulable {
    global void execute(SchedulableContext ctx) {}
}
"""


def _in(stmts, path: str):
    return [s for s in stmts if s.path == path]


def _eps(stmts, path: str = CLS):
    return [s for s in _in(stmts, path) if is_entry_point(s)]


def _line(src: str, text: str) -> int:
    return next(i for i, line in enumerate(src.splitlines(), 1) if text in line)


def test_rest_verbs_are_bindings(analyze_files) -> None:
    src = """@RestResource(urlMapping='/orders/*')
global with sharing class Svc {
    @HttpGet global static Order__c getOrder() { return null; }
    @HTTPPOST global static void create() {}
}
"""
    got = [(s.semanticType, s.framework, s.routeKind, s.method, s.endpoint, s.handler, s.responseDTO)
           for s in _eps(analyze_files({CLS: src}))]
    rest = "/services/apexrest/orders/*"
    assert got == [("route", "apex-rest", "route", "GET", rest, "getOrder", "Order__c"),
                   ("route", "apex-rest", "route", "POST", rest, "create", None)]


def test_rest_resource_class_is_wiring(analyze_files) -> None:
    src = "@RestResource(urlMapping='/orders/*')\nglobal class Svc { global static void helper() {} }\n"
    assert _eps(analyze_files({CLS: src})) == []


def test_aura_enabled_is_an_rpc_binding(analyze_files) -> None:
    src = "public class Svc {\n    @AuraEnabled(cacheable=true) public static List<Account> recent() { return null; }\n}\n"
    [r] = _eps(analyze_files({CLS: src}))
    assert (r.semanticType, r.framework, r.routeKind, r.method, r.endpoint, r.handler) == (
        "route", "apex-aura", "rpc", "RPC", "Svc.recent", "recent")


def test_webservice_is_a_soap_binding(analyze_files) -> None:
    src = "global class Svc {\n    webservice static Id cancel(Id orderId) { return orderId; }\n}\n"
    stmts = analyze_files({CLS: src})
    [r] = _eps(stmts)
    assert (r.semanticType, r.framework, r.routeKind, r.method, r.endpoint, r.handler, r.responseDTO) == (
        "route", "apex-soap", "rpc", "RPC", "Svc/cancel", "cancel", "Id")


def test_record_trigger_is_a_consumer_of_its_sobject(analyze_files) -> None:
    path = "force-app/main/default/triggers/OrderTrigger.trigger"
    src = "trigger OrderTrigger on Order__c (before insert, after update) {\n    System.debug('x');\n}\n"
    [r] = _eps(analyze_files({path: src}), path)
    assert (r.semanticType, r.framework, r.method, r.endpoint, r.handler, r.handlerLine, r.nodeType) == (
        "eventbus_consumer", "apex-trigger", "EVENT", "Order__c", "OrderTrigger", 1, "synthetic")
    assert r.text == "trigger OrderTrigger on Order__c (before insert, after update)"


@pytest.mark.parametrize("sobject", ["Order_Event__e", "AccountChangeEvent", "Invoice__ChangeEvent"])
def test_event_channel_trigger_is_a_platform_event_consumer(analyze_files, sobject: str) -> None:
    path = "force-app/main/default/triggers/Evt.trigger"
    src = f"trigger Evt on {sobject} (after insert) {{ }}\n"
    [r] = _eps(analyze_files({path: src}), path)
    assert (r.semanticType, r.framework, r.method, r.endpoint) == (
        "eventbus_consumer", "apex-platform-event", "EVENT", sobject)


def test_system_schedule_is_a_timer_binding(analyze_files) -> None:
    src = """public class Svc {
    public static void plan() {
        System.schedule('nightly', '0 0 1 * * ?', new NightlySync());
    }
}
"""
    [r] = _eps(analyze_files({CLS: src, JOB: NIGHTLY}))
    assert (r.semanticType, r.framework, r.method, r.endpoint, r.handler, r.handlerLine, r.handlerPath,
            r.startLine) == ("timer", "apex-schedule", "PERIODIC", "0 0 1 * * ?", "execute",
                             _line(NIGHTLY, "void execute"), JOB, _line(src, "System.schedule"))


def test_system_schedule_with_a_typed_job_variable_and_a_computed_cron(analyze_files) -> None:
    src = """public class Svc {
    public static void plan(String cron) {
        NightlySync job = new NightlySync();
        System.schedule('nightly', cron, job);
    }
}
"""
    [r] = _eps(analyze_files({CLS: src, JOB: NIGHTLY}))
    assert (r.endpoint, r.handler, r.handlerPath) == (None, "execute", JOB)


def test_schedulable_class_is_none(analyze_files) -> None:
    assert _eps(analyze_files({JOB: NIGHTLY}), JOB) == []


def test_eventbus_publish_is_outbound_to_its_channel(analyze_files) -> None:
    src = """public class Svc {
    public void send(Id id, List<Order_Event__e> batch, List<SObject> any) {
        EventBus.publish(new Order_Event__e(OrderId__c = id));
        EventBus.publish(batch);
        EventBus.publish(any);
    }
}
"""
    stmts = analyze_files({CLS: src})
    pubs = [(s.framework, s.method, s.endpoint) for s in _in(stmts, CLS) if s.semanticType == "eventbus_publish"]
    assert pubs == [("apex-platform-event", None, "Order_Event__e"),
                    ("apex-platform-event", None, "Order_Event__e"),
                    ("apex-platform-event", None, None)]
    assert _eps(stmts) == []


def test_callout_is_outbound_with_the_setters_in_force(analyze_files) -> None:
    src = """public class Svc {
    public void sync(Id id, Boolean retry) {
        Http h = new Http();
        HttpRequest req = new HttpRequest();
        req.setMethod('GET');
        req.setEndpoint('callout:ERP/orders/' + id);
        h.send(req);
        req = new HttpRequest();
        req.setMethod('POST');
        req.setEndpoint('https://erp.example.com/orders');
        h.send(req);
        if (retry) { req.setMethod('PUT'); }
        h.send(req);
    }
}
"""
    stmts = analyze_files({CLS: src})
    calls = [(s.method, s.endpoint) for s in _in(stmts, CLS) if s.semanticType == "api_call"]
    assert calls == [("GET", "callout:ERP/orders/{id}"),
                     ("POST", "https://erp.example.com/orders"),
                     (None, "https://erp.example.com/orders")]  # verb depends on the branch


def test_async_job_starts_are_none(analyze_files) -> None:
    src = """public class Svc {
    @future public static void later() {}
    public void go() {
        System.enqueueJob(new Q());
        Database.executeBatch(new B());
        later();
    }
}
"""
    stmts = analyze_files({CLS: src})
    assert [s for s in _in(stmts, CLS) if s.semanticType and s.semanticType != "comment"] == []


def test_data_access_is_not_an_entry_point(analyze_files) -> None:
    src = """public class Svc {
    public void f(Account a) {
        List<Account> xs = [SELECT Id FROM Account];
        Integer n = Database.countQuery('SELECT COUNT() FROM Account');
        update a;
    }
}
"""
    stmts = analyze_files({CLS: src})
    got = [(s.semanticType, s.method, s.dataAccessHint) for s in _in(stmts, CLS) if s.semanticType]
    assert got == [("query_statement", None, None), ("query_statement", "countQuery", None),
                   ("db_method_call", "update", "salesforce")]


def test_test_code_is_never_an_entry_point(analyze_files) -> None:
    src = """@IsTest private class Svc {
    @AuraEnabled public static void a() {}
    static void t() { System.schedule('n', '0 0 1 * * ?', new NightlySync()); }
}
"""
    other = "force-app/main/default/classes/P.cls"
    src2 = """public class P {
    static testMethod void t() { System.schedule('n', '0 0 1 * * ?', new NightlySync()); }
    @isTest static void u() { System.schedule('n', '0 0 1 * * ?', new NightlySync()); }
}
"""
    stmts = analyze_files({CLS: src, other: src2, JOB: NIGHTLY})
    assert _eps(stmts) == [] and _eps(stmts, other) == []


def test_fixture_files_are_never_entry_points(analyze_files) -> None:
    path = "fixtures/Svc.cls"
    src = """public class Svc {
    @AuraEnabled public static void a() {}
    public static void p() { System.schedule('n', '0 0 1 * * ?', new Svc()); }
}
"""
    assert _eps(analyze_files({path: src}), path) == []


@pytest.mark.parametrize("jobs", [1, 2])
def test_schedule_handler_in_another_file_end_to_end(tmp_path, jobs: int) -> None:
    from breezeai_cog import analyze_repo

    repo = tmp_path / "repo"
    src = "public class Svc {\n    public static void plan() {\n" \
          "        System.schedule('n', '0 0 1 * * ?', new NightlySync());\n    }\n}\n"
    for rel, text in {CLS: src, JOB: NIGHTLY}.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    result = analyze_repo(repo, capture_statements=True, out=tmp_path / "out", jobs=jobs)
    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    rec = next(json.loads(line) for line in lines if f'"{CLS}"' in line)
    (timer,) = [s for s in rec["statements"] if s.get("semanticType") == "timer"]
    assert (timer["handler"], timer["handlerLine"], timer["handlerPath"]) == ("execute", 2, JOB)
