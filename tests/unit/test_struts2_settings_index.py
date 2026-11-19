"""A ``struts.xml`` action's extension comes from the ``config`` repo index (the
``struts2.settings`` fact, the same rules as the annotated actions): the parser opens no other
file to find it."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from breezeai_cog.parsers.additive import index_fact
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.config.index import ConfigIndex, build_config_index
from breezeai_cog.parsers.config.parser import ConfigParser
from breezeai_cog.parsers.java_struts2.config import Struts2ConfigParser
from breezeai_cog.parsers.java_struts2.facts import SETTINGS

RES = "src/main/resources"
WEB = "src/main/webapp/WEB-INF"


def _xml(body: str, constants: str = "") -> str:
    return f'<?xml version="1.0"?>\n<struts>\n{constants}{body}</struts>\n'


def _ext(value: str) -> str:
    return f'<constant name="struts.action.extension" value="{value}"/>\n'


_ACTION = '<package name="p" namespace="/n"><action name="a"/></package>\n'
_WEB_XML = """<web-app><filter><filter-name>s</filter-name>
<filter-class>org.apache.struts2.dispatcher.filter.StrutsPrepareAndExecuteFilter</filter-class>
<init-param><param-name>struts.action.extension</param-name><param-value>{}</param-value></init-param>
</filter></web-app>
"""


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


def _endpoints(analyze_files, files: dict[str, str]) -> dict[str, list[str | None]]:
    out: dict[str, list[str | None]] = {}
    for s in analyze_files(files):
        if s.framework == "struts2" and s.routeKind == "route":
            out.setdefault(s.id.split(":", 1)[0], []).append(s.endpoint)
    return out


# --- the module's settings, whichever Struts file declares them -------------------------------

def test_a_constant_in_an_included_file_applies_to_the_configuration(analyze_files) -> None:
    eps = _endpoints(analyze_files, {
        f"{RES}/struts.xml": _xml(_ACTION, '<include file="struts-constants.xml"/>\n'),
        f"{RES}/struts-constants.xml": _xml("", '<include file="struts.xml"/>\n' + _ext("do")),
    })  # the include cycle is harmless
    assert eps == {f"{RES}/struts.xml": ["/n/a.do"]}


def test_a_struts_file_nothing_includes_is_not_loaded(analyze_files) -> None:
    # Struts loads `struts.xml` and what it includes; a stray file's constant is not the app's.
    eps = _endpoints(analyze_files, {
        f"{RES}/struts.xml": _xml(_ACTION),
        f"{RES}/struts-old.xml": _xml("", _ext("html")),
        "conf/struts-extra.xml": _xml("", _ext("xyz")),
    })
    assert eps == {f"{RES}/struts.xml": ["/n/a.action"]}


def test_a_stray_file_of_a_module_is_not_the_repositorys_configuration(analyze_files) -> None:
    eps = _endpoints(analyze_files, {
        "shop/pom.xml": "<project/>",
        f"shop/{RES}/struts.xml": _xml(_ACTION),
        "shop/conf/extra.xml": _xml("", _ext("xyz")),  # outside src/main, inside the module
    })
    assert eps == {f"shop/{RES}/struts.xml": ["/n/a.action"]}


def test_a_stray_file_does_not_contradict_the_configuration(analyze_files) -> None:
    eps = _endpoints(analyze_files, {
        f"{RES}/struts.xml": _xml(_ACTION, _ext("do")),
        f"{RES}/struts-old.xml": _xml("", _ext("html")),
    })
    assert eps == {f"{RES}/struts.xml": ["/n/a.do"]}


def test_two_included_files_that_disagree_leave_the_extension_unknown(analyze_files) -> None:
    eps = _endpoints(analyze_files, {
        f"{RES}/struts.xml": _xml(_ACTION, '<include file="a.xml"/>\n<include file="b.xml"/>\n'),
        f"{RES}/a.xml": _xml("", _ext("do")),
        f"{RES}/b.xml": _xml("", _ext("html")),
    })
    assert eps == {f"{RES}/struts.xml": [None]}  # never one of the two


def test_a_filter_naming_its_own_configuration_files_counts_every_struts_file(analyze_files) -> None:
    # Which files the `config` init-param loads is not read, so any Struts file may be one.
    web = _WEB_XML.format("x").replace("struts.action.extension", "config")
    files = {f"{RES}/struts.xml": _xml(_ACTION), f"{RES}/custom.xml": _xml("", _ext("do")),
             f"{WEB}/web.xml": web}
    assert _endpoints(analyze_files, files) == {f"{RES}/struts.xml": ["/n/a.do"]}
    files[f"{RES}/other.xml"] = _xml("", _ext("html"))
    assert _endpoints(analyze_files, files) == {f"{RES}/struts.xml": [None]}


def test_a_module_without_a_struts_xml_counts_every_struts_file(analyze_files) -> None:
    eps = _endpoints(analyze_files, {
        f"{RES}/app-actions.xml": _xml(_ACTION),
        f"{RES}/app-constants.xml": _xml("", _ext("do")),
    })
    assert eps == {f"{RES}/app-actions.xml": ["/n/a.do"]}


def test_struts_properties_counts_beside_the_struts_xml_only(analyze_files) -> None:
    eps = _endpoints(analyze_files, {
        f"{RES}/struts.xml": _xml(_ACTION, '<include file="sub/more.xml"/>\n' + _ext("do")),
        f"{RES}/sub/more.xml": _xml(""),
        f"{RES}/sub/struts.properties": "struts.action.extension=html\n",  # not the classpath root
    })
    assert eps == {f"{RES}/struts.xml": ["/n/a.do"]}


def test_web_xml_overrides_the_xml_constant(analyze_files) -> None:
    eps = _endpoints(analyze_files, {
        f"{RES}/struts.xml": _xml(_ACTION, _ext("do")),
        f"{WEB}/web.xml": _WEB_XML.format("htm"),
    })
    assert eps == {f"{RES}/struts.xml": ["/n/a.htm"]}


def test_each_module_keeps_its_own_extension(analyze_files) -> None:
    files = {}
    for module, ext in (("a", "do"), ("b", "html")):
        files[f"{module}/pom.xml"] = "<project/>"
        files[f"{module}/{RES}/struts.xml"] = _xml(_ACTION, _ext(ext))
    eps = _endpoints(analyze_files, files)
    assert eps == {f"a/{RES}/struts.xml": ["/n/a.do"], f"b/{RES}/struts.xml": ["/n/a.html"]}


def test_no_constant_anywhere_is_the_struts_default(analyze_files) -> None:
    assert _endpoints(analyze_files, {f"{RES}/struts.xml": _xml(_ACTION)}) == {
        f"{RES}/struts.xml": ["/n/a.action"]}


# --- no other file is opened while parsing ----------------------------------------------------

def test_parsing_opens_no_other_settings_file(tmp_path: Path, monkeypatch) -> None:
    files = {
        f"{RES}/struts.xml": _xml(_ACTION, '<include file="struts-admin.xml"/>\n'),
        f"{RES}/struts-admin.xml": _xml("", _ext("do")),
        f"{RES}/struts.properties": "struts.action.extension=html\n",
        f"{WEB}/web.xml": _WEB_XML.format("htm"),
    }
    _write(tmp_path, files)
    index = ConfigParser().build_index(tmp_path, [tmp_path / r for r in files if r.endswith(".xml")])
    rel = f"{RES}/struts.xml"
    ctx = ParseContext(path=rel, abs_path=tmp_path / rel, source=(tmp_path / rel).read_bytes(),
                       repo_root=tmp_path, capture_statements=True, resolution_index=index)
    opened: list[str] = []
    real_read_bytes, real_open = Path.read_bytes, builtins.open
    monkeypatch.setattr(Path, "read_bytes", lambda self: opened.append(str(self)) or real_read_bytes(self))
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: opened.append(str(f)) or real_open(f, *a, **k))
    record = Struts2ConfigParser().parse_file(ctx)
    assert opened == []
    assert [s.endpoint for s in record.statements if s.routeKind == "route"] == ["/n/a.htm"]


def test_without_an_index_only_the_files_own_constants_apply(tmp_path: Path) -> None:
    files = {
        f"{RES}/struts.xml": _xml(_ACTION, _ext("do")),
        f"{RES}/struts.properties": "struts.action.extension=html\n",  # not read
    }
    _write(tmp_path, files)
    rel = f"{RES}/struts.xml"
    ctx = ParseContext(path=rel, abs_path=tmp_path / rel, source=(tmp_path / rel).read_bytes(),
                       repo_root=tmp_path, capture_statements=True)
    record = Struts2ConfigParser().parse_file(ctx)
    assert [s.endpoint for s in record.statements if s.routeKind == "route"] == ["/n/a.do"]


# --- the config index -------------------------------------------------------------------------

@pytest.mark.parametrize("jobs", [1, 2])
def test_the_config_index_holds_the_settings_for_any_jobs(tmp_path: Path, jobs: int) -> None:
    files = {
        f"a/{RES}/struts.xml": _xml("", _ext("do")),
        f"b/{RES}/struts.xml": _xml("", _ext("html")),
        f"b/{RES}/struts.properties": "struts.action.extension=htm\n",
        "a/pom.xml": "<project/>",
        "b/pom.xml": "<project/>",
        "app.yaml": "name: x\n",
    }
    _write(tmp_path, files)
    index = build_config_index(tmp_path, sorted(tmp_path / r for r in files), jobs)
    assert isinstance(index, ConfigIndex)
    settings = index_fact(index, SETTINGS)
    assert settings["a"]["struts.action.extension"] == "do"
    assert settings["b"]["struts.action.extension"] == "htm"  # struts.properties beside it wins


def test_a_repo_without_struts_builds_no_config_index(tmp_path: Path) -> None:
    _write(tmp_path, {"app.yaml": "name: x\n", "pom.xml": "<project/>"})
    assert build_config_index(tmp_path, [tmp_path / "app.yaml", tmp_path / "pom.xml"]) is None
