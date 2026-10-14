"""Apex parser: structure, triggers, case-insensitivity, claims and cross-file resolution."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.apex.index import build_class_index
from breezeai_cog.parsers.apex.parser import ApexParser
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.schemas import FileRecord

SRC_SERVICE = b'''
@RestResource(urlMapping='/accounts/*')
global with sharing class AccountService extends BaseService implements Schedulable {
    private static final String LABEL = 'a';
    public Integer counter { get; set; }

    public AccountService(String seed) { super(); }

    public enum Status { ACTIVE, INACTIVE }
    public interface Greeter extends Named { String greet(String who); }
    public class Inner { public void run() {} }

    @AuraEnabled(cacheable=true)
    public static List<Account> list(String q) { return null; }

    protected abstract void hook();
    public virtual void v() {}
    void packageLevel() {}
}
'''

SRC_TRIGGER = b'''trigger AccountTrigger on Account (before insert, after update) {
    AccountHandler.handle(Trigger.new);
}
'''


def _parse(tmp_path, src: bytes, name: str, *, capture: bool = True, index=None) -> FileRecord:
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture, resolution_index=index)
    return ApexParser().parse_file(ctx)


def test_class_structure(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_SERVICE, "AccountService.cls")
    assert rec.language == "apex"
    classes = {c.name: c for c in rec.classes}
    assert set(classes) == {"AccountService", "Status", "Greeter", "Inner"}
    svc = classes["AccountService"]
    assert (svc.type, svc.visibility, svc.extends) == ("class", "global", "BaseService")
    assert svc.implements == ["Schedulable"]
    assert svc.metadata == {"sharing": "with"}
    assert [(d.name, d.args) for d in svc.decorators] == [("RestResource", ["urlMapping=/accounts/*"])]
    assert classes["Status"].type == "enum"
    assert classes["Greeter"].type == "interface" and classes["Greeter"].implements == ["Named"]
    assert classes["Inner"].parentId == svc.id  # nested type parented to its enclosing class


def test_enum_members_are_statements(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_SERVICE, "AccountService.cls")
    members = [s.name for s in rec.statements if s.semanticType == "enum_member"]
    assert members == ["ACTIVE", "INACTIVE"]


def test_method_fields(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_SERVICE, "AccountService.cls")
    fns = {f.name: f for f in rec.functions}
    lst = fns["list"]
    assert (lst.type, lst.visibility, lst.isStatic) == ("method", "public", True)
    assert lst.returnType == "List<Account>"
    assert [(p.name, p.type) for p in lst.params] == [("q", "String")]
    assert [(d.name, d.args) for d in lst.decorators] == [("AuraEnabled", ["cacheable=true"])]
    assert fns["packageLevel"].visibility == "private"  # Apex default is private, not package
    assert fns["greet"].visibility == "public"  # interface members are public
    ctor = next(f for f in rec.functions if f.type == "constructor")
    assert ctor.name == "AccountService"
    assert [(p.name, p.type) for p in next(
        c for c in rec.classes if c.name == "AccountService").constructorParams] == [("seed", "String")]


def test_abstract_class(tmp_path) -> None:
    rec = _parse(tmp_path, b"public abstract class A { }", "A.cls")
    assert rec.classes[0].isAbstract is True


def test_virtual_is_not_abstract(tmp_path) -> None:
    rec = _parse(tmp_path, b"public virtual class B { }", "B.cls")
    assert rec.classes[0].isAbstract is False


def test_case_insensitive_keywords(tmp_path) -> None:
    src = b"PUBLIC WITHOUT SHARING CLASS Shout { @TestVisible PRIVATE STATIC Integer n; PUBLIC VOID Go() {} }"
    rec = _parse(tmp_path, src, "Shout.cls")
    cls = rec.classes[0]
    assert (cls.visibility, cls.metadata) == ("public", {"sharing": "without"})
    fn = rec.functions[0]
    assert (fn.name, fn.visibility) == ("Go", "public")


def test_trigger_is_one_function(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_TRIGGER, "AccountTrigger.trigger")
    assert rec.classes == []
    (fn,) = rec.functions
    assert (fn.name, fn.type, fn.parentId) == ("AccountTrigger", "trigger", rec.id)
    assert fn.metadata == {"sObject": "Account", "events": ["before insert", "after update"]}
    assert [c.name for c in fn.calls] == ["handle"]


def test_statements_need_capture(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_SERVICE, "AccountService.cls", capture=False)
    assert rec.statements == []


def test_cross_file_call_resolution(tmp_path) -> None:
    (tmp_path / "classes").mkdir()
    (tmp_path / "classes" / "AccountHandler.cls").write_bytes(b"public class AccountHandler { }")
    (tmp_path / "classes" / "Dup.cls").write_bytes(b"public class Dup { }")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "Dup.cls").write_bytes(b"public class dup { }")
    files = sorted(tmp_path.glob("**/*.cls"))
    index = build_class_index(tmp_path, files)
    assert index["accounthandler"] == "classes/AccountHandler.cls"
    assert index["dup"] is None  # declared twice: never a guessed file

    rec = _parse(tmp_path, SRC_TRIGGER, "AccountTrigger.trigger", index=index)
    assert rec.functions[0].calls[0].path == "classes/AccountHandler.cls"


def test_receiver_type_resolution_is_case_insensitive(tmp_path) -> None:
    src = b"public class C { private AccountHandler H; public void run() { h.Handle(); } }"
    rec = _parse(tmp_path, src, "C.cls", index={"accounthandler": "classes/AccountHandler.cls"})
    assert rec.functions[0].calls[0].path == "classes/AccountHandler.cls"


def test_claims_rejects_vb6_class_module() -> None:
    registry.clear()
    registry.register(ApexParser())
    vb6 = b'VERSION 1.0 CLASS\nBEGIN\nEND\nAttribute VB_Name = "Foo"\n'
    assert registry.select("Foo.cls", vb6) is None
    assert registry.select("Foo.cls", b"public class Foo {}").name == "apex"
    assert registry.select("T.trigger", SRC_TRIGGER).name == "apex"
    registry.clear()


def test_output_validates(tmp_path) -> None:
    for src, name in ((SRC_SERVICE, "AccountService.cls"), (SRC_TRIGGER, "AccountTrigger.trigger")):
        rec = _parse(tmp_path, src, name)
        errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                      .iter_errors(json.loads(to_line(rec))))
        assert not errors, errors


SRC_SEMANTICS = b'''public class Svc {
    private static final String BASE = 'callout:Cred/api';
    public void run(String q, Account a) {
        Account one = [SELECT Id FROM Account WHERE Name = :q];
        List<List<SObject>> found = [FIND :q IN ALL FIELDS RETURNING Account(Id)];
        List<Account> dyn = Database.query('SELECT Id FROM Account');
        for (Account acc : [SELECT Id FROM Account]) { System.debug(acc); }
        insert a;
        UPDATE a;
        upsert a;
        delete a;
        undelete a;
        merge a a;
        Database.insert(a, false);
        Http h = new Http();
        HttpRequest req = new HttpRequest();
        req.setEndpoint(BASE + '/items');
        req.setMethod('post');
        HttpResponse res = h.send(req);
        HttpResponse res2 = new Http().send(req);
        System.schedule('job', '0 0 * * * ?', new Svc());
        EventBus.publish(new Order_Event__e());
    }
}
'''


def _sem(rec: FileRecord):
    return [(s.semanticType, s.method, s.endpoint, s.dataAccessHint, s.startLine)
            for s in rec.statements if s.semanticType and s.semanticType not in ("comment", "enum_member")]


def test_soql_and_sosl(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_SEMANTICS, "Svc.cls")
    queries = [(m, line) for sem, m, _, _, line in _sem(rec) if sem == "query_statement"]
    assert ("soql", 4) in queries          # variable initialiser
    assert ("sosl", 5) in queries
    assert ("query", 6) in queries         # Database.query(...) with a string argument
    assert ("soql", 7) in queries          # for (x : [SELECT ...])


def test_dml_keywords_and_database_methods(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_SEMANTICS, "Svc.cls")
    dml = {line: m for sem, m, _, hint, line in _sem(rec)
           if sem == "db_method_call" and hint == "salesforce"}
    assert dml == {8: "insert", 9: "update", 10: "upsert", 11: "delete", 12: "undelete",
                   13: "merge", 14: "insert"}


def test_http_callout(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_SEMANTICS, "Svc.cls")
    calls = {line: (m, ep) for sem, m, ep, _, line in _sem(rec) if sem == "api_call"}
    assert calls[19] == ("POST", "{BASE}/items")   # variable receiver; verb upper-cased
    assert calls[20] == ("POST", "{BASE}/items")   # `new Http().send(req)`


def test_timer_and_event(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_SEMANTICS, "Svc.cls")
    kinds = {(sem, m) for sem, m, _, _, _ in _sem(rec)}
    assert ("timer", "schedule") in kinds
    assert ("eventbus_publish", "publish") in kinds


def test_no_false_positives(tmp_path) -> None:
    src = b'''public class N {
    public void f(Account a, Mailer m) {
        System.debug('hi');
        m.send(a);
        List<Account> xs = new List<Account>();
        xs.add(a);
        Mailer http = new Mailer();
        http.send(a);
        Integer insert_count = 1;
    }
}
'''
    assert _sem(_parse(tmp_path, src, "N.cls")) == []


def test_callout_without_request_details_has_null_fields(tmp_path) -> None:
    src = b"public class C { public void f(HttpRequest r) { Http h = new Http(); h.send(r); } }"
    rec = _parse(tmp_path, src, "C.cls")
    ((sem, method, endpoint, _, _),) = _sem(rec)
    assert (sem, method, endpoint) == ("api_call", None, None)


def test_trigger_statements_are_classified(tmp_path) -> None:
    src = b"trigger T on Account (before insert) { insert new Task(Subject = 'x'); }"
    rec = _parse(tmp_path, src, "T.trigger")
    assert [(s.semanticType, s.method) for s in rec.statements if s.semanticType] == [
        ("db_method_call", "insert")]
    assert all(s.parentId == rec.functions[0].id for s in rec.statements)
