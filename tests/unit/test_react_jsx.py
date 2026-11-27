"""React JSX markup: links, form posts and event props, through the shared markup rules."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_react.parser import ReactParser
from breezeai_cog.schemas import FileRecord


def _parse(tmp_path, name: str, source: bytes, *, capture=True) -> FileRecord:
    p = tmp_path / name
    p.write_bytes(source)
    ctx = ParseContext(
        path=name, abs_path=p, source=source, repo_root=tmp_path, capture_statements=capture
    )
    return ReactParser().parse_file(ctx)


_LOGIN = b"""import React from "react";
export function Login() {
  const save = () => {};
  return (
    <form action="/session" method="post" onSubmit={save}>
      <a href="/help">Help</a>
      <a href={helpUrl}>More</a>
      <a href="https://example.com">Out</a>
      <button onClick={() => remove(id)}>Delete</button>
      <input onChange={this.setEmail} />
      <Panel onSaved={props.onSaved} />
      <button onClick={() => { busy = true; }}>X</button>
    </form>
  );
}
"""


def _jsx(rec: FileRecord):
    return [s for s in rec.statements if s.nodeType.startswith("jsx_")]


def test_form_post_and_links_use_the_shared_markup_rules(tmp_path) -> None:
    rec = _parse(tmp_path, "Login.tsx", _LOGIN)
    post = next(s for s in _jsx(rec) if s.semanticType == "api_call")
    assert (post.method, post.endpoint, post.startLine) == ("POST", "/session", 5)
    links = [(s.endpoint, s.startLine) for s in _jsx(rec) if s.semanticType == "route"]
    # a literal path; a link built in code (no endpoint); another site is not a page link
    assert links == [("/help", 6), (None, 7)]


def test_event_props_name_as_written_and_their_handler(tmp_path) -> None:
    rec = _parse(tmp_path, "Login.tsx", _LOGIN)
    events = [(s.name, s.handler, s.startLine) for s in _jsx(rec) if s.nodeType == "jsx_attribute"]
    assert events == [
        ("onSubmit", "save", 5),
        ("onClick", "remove", 9),
        ("onChange", "setEmail", 10),
        ("onSaved", "props.onSaved", 11),
        ("onClick", None, 12),  # an inline statement calls no one function
    ]


def test_jsx_records_belong_to_the_component(tmp_path) -> None:
    rec = _parse(tmp_path, "Login.tsx", _LOGIN)
    login = next(f for f in rec.functions if f.name == "Login")
    assert {s.parentId for s in _jsx(rec)} == {login.id}


def test_no_jsx_records_without_statement_capture(tmp_path) -> None:
    rec = _parse(tmp_path, "Login.tsx", _LOGIN, capture=False)
    assert _jsx(rec) == []


def test_custom_form_component_is_not_a_form_post(tmp_path) -> None:
    src = b"""import React from "react";
export const F = () => <Form action="/x" method="post" />;
"""
    rec = _parse(tmp_path, "F.tsx", src)
    assert not [s for s in rec.statements if s.semanticType == "api_call"]
