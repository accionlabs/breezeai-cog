"""Struts2 configuration parser and annotation rules: selection against ``config``, every
Struts2 version, the record it keeps, gating, and the address helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java_struts2.common import (
    action_endpoint, convention_name, convention_namespace, extension, xml_constants,
)
from breezeai_cog.parsers.java_struts2.config import Struts2ConfigParser
from breezeai_cog.schemas import FileRecord

#: The DOCTYPE each Struts2 generation ships.
_DTDS = {
    "2.0": '"-//Apache Software Foundation//DTD Struts Configuration 2.0//EN" '
           '"http://struts.apache.org/dtds/struts-2.0.dtd"',
    "2.5": '"-//Apache Software Foundation//DTD Struts Configuration 2.5//EN" '
           '"http://struts.apache.org/dtds/struts-2.5.dtd"',
    "6.0": '"-//Apache Software Foundation//DTD Struts Configuration 6.0//EN" '
           '"https://struts.apache.org/dtds/struts-6.0.dtd"',
    "none": None,
}


def _doc(dtd: str | None, body: str) -> bytes:
    head = f"<!DOCTYPE struts PUBLIC {dtd}>\n" if dtd else ""
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{head}<struts>\n{body}</struts>\n'.encode()


def _parse(tmp_path: Path, rel: str, source: bytes, *, capture: bool = True) -> FileRecord:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(source)
    ctx = ParseContext(path=rel, abs_path=p, source=source, repo_root=tmp_path,
                       capture_statements=capture)
    return Struts2ConfigParser().parse_file(ctx)


@pytest.mark.parametrize("version", sorted(_DTDS))
def test_every_struts2_generation(tmp_path, version) -> None:
    rec = _parse(tmp_path, "struts.xml", _doc(_DTDS[version],
        '<package name="p" namespace="/n" extends="struts-default">\n'
        '  <action name="a" class="x.A"/>\n</package>\n'))
    [r] = rec.statements
    assert (r.endpoint, r.handler, r.nodeType) == ("/n/a.action", "execute", "element")
    assert rec.framework == "struts2"
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors


def test_record_stays_a_config_record(tmp_path) -> None:
    rec = _parse(tmp_path, "struts.xml", _doc(None, '<package name="p"><action name="a"/>'
                                                     "</package>\n"))
    assert (rec.type, rec.language) == ("config", "config")
    assert rec.metadata  # what the config parser extracts is kept


def test_capture_off_and_fixtures_emit_no_routes(tmp_path) -> None:
    src = _doc(None, '<package name="p"><action name="a"/></package>\n')
    assert _parse(tmp_path, "struts.xml", src, capture=False).statements == []
    assert _parse(tmp_path, "fixtures/struts.xml", src).statements == []


def test_constant_attributes_in_either_order() -> None:
    src = (b'<constant value="do" name="struts.action.extension"/>'
           b'<constant name="struts.devMode" value="true"/>')
    assert xml_constants(src) == {"struts.action.extension": "do", "struts.devMode": "true"}


def test_selection_against_config() -> None:
    from breezeai_cog.parsers.config.parser import ConfigParser

    registry.clear()
    registry.register(ConfigParser())
    registry.register(Struts2ConfigParser())
    struts = _doc(None, '<package name="p"></package>\n')
    assert registry.select("src/main/resources/struts.xml", struts).name == "java-struts2"
    assert registry.select("conf/routes-admin.xml", struts).name == "java-struts2"
    assert registry.select("WEB-INF/struts-config.xml",
                           b"<struts-config><action-mappings/></struts-config>").name == "config"
    assert registry.select("pom.xml", b"<project/>").name == "config"
    assert registry.base_parser_for("struts.xml").name == "config"  # indexes stay keyed by config
    registry.clear()


@pytest.mark.parametrize("value,suffix", [
    (None, ".action"), ("action", ".action"), ("action,,", ".action"), ("do,action", ".do"),
    (",", ""), ("", ""), ("${ext}", None),
])
def test_extension(value, suffix) -> None:
    assert extension(value) == suffix


@pytest.mark.parametrize("namespace,name,suffix,expected", [
    ("", "a", ".action", "/a.action"), ("/", "a", ".action", "/a.action"),
    ("/x/", "a", "", "/x/a"), (None, "a", ".do", None), ("/x", "/abs", ".do", "/abs.do"),
    ("/x", "a", None, None), ("/x", "", ".do", None), ("admin", "a", ".do", "/admin/a.do"),
])
def test_action_endpoint(namespace, name, suffix, expected) -> None:
    assert action_endpoint(namespace, name, suffix) == expected


def test_convention_rules() -> None:
    assert convention_name("UserProfileAction") == "user-profile"
    assert convention_name("Report") == "report"
    assert convention_name("Action") == "action"
    assert convention_name("HTMLAction") == "h-t-m-l"
    locators = ("action", "actions", "struts", "struts2")
    assert convention_namespace("com.shop.actions.admin.users", locators) == "/admin/users"
    assert convention_namespace("com.shop.struts2", locators) == ""
    assert convention_namespace("com.shop.web", locators) is None
