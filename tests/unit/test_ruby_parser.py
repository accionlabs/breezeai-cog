from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.ruby.parser import RubyParser

SRC = b'''require_relative "lib/service"

class User
  def greet(name)
    service.call(name)
  end
end

module Admin
  class Session
    def start
      puts "boot"
    end
  end
end
'''


def _parse_ruby_source(
    root: Path,
    source: bytes,
    path: str = "x.rb",
    *,
    capture_statements: bool = False,
):
    abs_path = root / path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(source)
    return RubyParser().parse_file(ParseContext(
        path=path, abs_path=abs_path, source=source, repo_root=root,
        capture_statements=capture_statements,
    ))


def test_ruby_class_and_methods(tmp_path: Path) -> None:
    repo = tmp_path
    service_path = repo / "lib" / "service.rb"
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text("class Service\n  def call(name)\n    name\n  end\nend\n")

    p = repo / "app.rb"
    p.write_bytes(SRC)
    ctx = ParseContext(path="app.rb", abs_path=p, source=SRC, repo_root=repo)
    rec = RubyParser().parse_file(ctx)

    assert rec.language == "ruby"
    assert rec.path == "app.rb"
    assert {c.name for c in rec.classes} >= {"User", "Session"}
    assert {f.name for f in rec.functions} >= {"greet", "start"}
    assert any("lib/service.rb" in item for item in rec.importFiles)
    assert any(c.name == "greet" for c in rec.functions)
    assert any(c.name == "start" for c in rec.functions)


def test_ruby_statements_are_owned_by_the_most_specific_scope(tmp_path: Path) -> None:
    source = b'''class User
  def greet(name)
    if name
      puts name
    end
  end
end
'''
    path = tmp_path / "app.rb"
    path.write_bytes(source)
    ctx = ParseContext(
        path="app.rb",
        abs_path=path,
        source=source,
        repo_root=tmp_path,
        capture_statements=True,
    )

    rec = RubyParser().parse_file(ctx)

    if_statements = [statement for statement in rec.statements if statement.nodeType == "if"]
    greet = next(function for function in rec.functions if function.name == "greet")
    assert len(if_statements) == 1
    assert if_statements[0].parentId == greet.id


def test_ruby_statements_use_shared_semantic_detection(tmp_path: Path) -> None:
    source = b'''class Data
  def load(id)
    User.where(id: id)
    Net::HTTP.get(uri)
    "SELECT * FROM users"
  end
end
'''
    path = tmp_path / "data.rb"
    path.write_bytes(source)
    ctx = ParseContext(
        path="data.rb",
        abs_path=path,
        source=source,
        repo_root=tmp_path,
        capture_statements=True,
    )

    rec = RubyParser().parse_file(ctx)

    assert any(statement.semanticType == "db_method_call" for statement in rec.statements)
    assert any(statement.semanticType == "api_call" for statement in rec.statements)
    assert any(statement.semanticType == "query_statement" for statement in rec.statements)
    assert not any(statement.nodeType in {"class", "module", "method"} for statement in rec.statements)


def test_ruby_binary_expressions_are_not_standalone_statements(tmp_path: Path) -> None:
    source = b'''def render
  msg = "n=" + a.to_s + " of " + total.to_s
  if a > b && total != 0
    msg
  end
end
'''
    path = tmp_path / "render.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="render.rb", abs_path=path, source=source, repo_root=tmp_path,
        capture_statements=True,
    ))

    assert not any(statement.nodeType == "binary" for statement in rec.statements)


def test_ruby_method_visibility_follows_section_markers(tmp_path: Path) -> None:
    source = b'''class Account
  def open
  end

  protected
  def audit
  end

  private
  def balance
  end

  public
  def close
  end
end
'''
    path = tmp_path / "account.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="account.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    visibility = {function.name: function.visibility for function in rec.functions}
    assert visibility == {
        "open": "public",
        "audit": "protected",
        "balance": "private",
        "close": "public",
    }


def test_ruby_calls_do_not_resolve_across_classes(tmp_path: Path) -> None:
    source = b'''class Alpha
  def process(value)
    value
  end
end

class Beta
  def run
    process(3)
  end
end
'''
    path = tmp_path / "app.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="app.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    run = next(function for function in rec.functions if function.name == "run")
    process_call = next(call for call in run.calls if call.name == "process")
    assert process_call.path is None


def test_ruby_require_relative_does_not_bind_lowercase_basename(tmp_path: Path) -> None:
    service_path = tmp_path / "lib" / "service.rb"
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text("class Service\n  def call(value)\n    value\n  end\nend\n")

    source = b'''require_relative "lib/service"
class Worker
  def run
    service = ThirdParty::Client.new
    service.call(1)
  end
end
'''
    path = tmp_path / "app.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="app.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    run = next(function for function in rec.functions if function.name == "run")
    call = next(call for call in run.calls if call.name == "call")
    assert call.path is None
    assert any(item.endswith("lib/service.rb") for item in rec.importFiles)


def test_ruby_model_index_identifies_activerecord_receivers(tmp_path: Path) -> None:
    """End-to-end: the repo pre-pass finds the model, so generic verbs in a *different*
    file (the controller) are trusted. Without that evidence they would be dropped."""
    (tmp_path / "app" / "models").mkdir(parents=True)
    (tmp_path / "app" / "models" / "user.rb").write_bytes(
        b"class User < ApplicationRecord\nend\n"
    )
    controller = tmp_path / "app" / "controllers" / "users_controller.rb"
    controller.parent.mkdir(parents=True)
    controller.write_bytes(
        b"class UsersController < ApplicationController\n"
        b"  def show\n"
        b"    user = User.find(params[:id])\n"
        b"    user.save\n"
        b"    Tempfile.create('x')\n"
        b"  end\n"
        b"end\n"
    )

    parser = RubyParser()
    index = parser.build_index(tmp_path, [tmp_path / "app" / "models" / "user.rb", controller])
    assert "User" in index.models

    rec = parser.parse_file(ParseContext(
        path="app/controllers/users_controller.rb", abs_path=controller,
        source=controller.read_bytes(), repo_root=tmp_path,
        capture_statements=True, resolution_index=index,
    ))
    hints = {s.text.splitlines()[0]: s.dataAccessHint for s in rec.statements
             if s.semanticType == "db_method_call"}
    assert any("User.find" in k for k in hints), hints
    assert any("user.save" in k for k in hints), hints
    # Tempfile.create shares a verb with ActiveRecord but is not a model -> never tagged.
    assert not any("Tempfile" in k for k in hints), hints


def test_ruby_model_index_includes_extensionless_scripts(tmp_path: Path) -> None:
    script = tmp_path / "models"
    script.write_bytes(b"#!/usr/bin/env ruby\nclass ScriptModel < ApplicationRecord\nend\n")

    index = RubyParser().build_index(tmp_path, [script])

    assert "ScriptModel" in index.models


def test_ruby_generic_verbs_dropped_without_model_evidence(tmp_path: Path) -> None:
    """The cost of the gate: with no index (single-file parse, or a model defined in a gem)
    a generic verb is dropped rather than guessed."""
    src = b"class C\n  def go\n    Thing.create(1)\n    Thing.all\n  end\nend\n"
    p = tmp_path / "c.rb"
    p.write_bytes(src)
    rec = RubyParser().parse_file(ParseContext(
        path="c.rb", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True,
    ))
    assert not any(s.semanticType == "db_method_call" for s in rec.statements)


def test_ruby_nested_class_ids_and_method_owners_are_qualified(tmp_path: Path) -> None:
    source = b'''class Outer
  class Result
    def render
      "outer"
    end

    class Middle
      class Leaf
        def read
          "leaf"
        end
      end
    end
  end
end

class Other
  class Result
    def render
      "other"
    end
  end
end
'''
    record = _parse_ruby_source(tmp_path, source)

    classes = {cls.id: cls for cls in record.classes}
    outer_result_id = "x.rb#Outer.Result"
    other_result_id = "x.rb#Other.Result"
    leaf_id = "x.rb#Outer.Result.Middle.Leaf"

    assert outer_result_id in classes
    assert other_result_id in classes
    assert leaf_id in classes
    assert classes[outer_result_id].name == "Result"
    assert classes[other_result_id].name == "Result"
    assert classes[leaf_id].name == "Leaf"

    methods = {fn.parentId: fn for fn in record.functions}
    assert methods[outer_result_id].id.startswith(f"{outer_result_id}#render@")
    assert methods[other_result_id].id.startswith(f"{other_result_id}#render@")
    assert methods[leaf_id].id.startswith(f"{leaf_id}#read@")


def test_ruby_nested_class_ids_do_not_depend_on_declaration_order_or_run(
    tmp_path: Path,
) -> None:
    outer = b'''class Outer
  class Result
  end
end
'''
    other = b'''class Other
  class Result
  end
end
'''
    first = _parse_ruby_source(tmp_path, outer + b"\n" + other)
    reordered = _parse_ruby_source(tmp_path, other + b"\n" + outer)
    repeated = _parse_ruby_source(tmp_path, outer + b"\n" + other)

    expected = {"Outer", "Outer.Result", "Other", "Other.Result"}
    expected_ids = {f"x.rb#{name}" for name in expected}
    assert {cls.id for cls in first.classes} == expected_ids
    assert {cls.id for cls in reordered.classes} == expected_ids
    assert {cls.id for cls in first.classes} == {cls.id for cls in repeated.classes}
    assert {fn.id for fn in first.functions} == {fn.id for fn in repeated.functions}


def test_ruby_module_scope_is_included_in_nested_class_identity(tmp_path: Path) -> None:
    source = b'''module X
  class Outer
    class Result
    end
  end
end
'''
    record = _parse_ruby_source(tmp_path, source)

    assert {cls.id for cls in record.classes} == {
        "x.rb#X",
        "x.rb#X.Outer",
        "x.rb#X.Outer.Result",
    }


def test_ruby_top_level_class_and_method_ids_remain_unchanged(tmp_path: Path) -> None:
    source = b'''class Account
  def open
  end
end
'''
    record = _parse_ruby_source(tmp_path, source)

    account = next(cls for cls in record.classes if cls.name == "Account")
    open_method = next(fn for fn in record.functions if fn.name == "open")
    assert account.id == "x.rb#Account"
    assert open_method.id == "x.rb#Account#open@2"
    assert open_method.parentId == account.id


def test_ruby_symbol_visibility_declarations(tmp_path: Path) -> None:
    source = b'''class Student
  private :calculate
  protected :save, :update

  def calculate; end
  def save; end
  def update; end
  def display; end

  def change_after; end
  protected :change_after
  def unaffected; end

  private *dynamic_methods
  def unsupported_target; end

  private
  public :explicitly_public
  def explicitly_public; end
  def remains_private; end
end
'''
    rec = _parse_ruby_source(tmp_path, source)

    visibility = {
        function.name: function.visibility
        for function in rec.functions
    }

    assert visibility["calculate"] == "private"
    assert visibility["save"] == "protected"
    assert visibility["update"] == "protected"
    assert visibility["display"] == "public"
    assert visibility["change_after"] == "protected"
    assert visibility["unaffected"] == "public"
    assert visibility["unsupported_target"] == "public"
    assert visibility["explicitly_public"] == "public"
    assert visibility["remains_private"] == "private"


def test_ruby_version_constant_is_one_ordinary_assignment(tmp_path: Path) -> None:
    source = b'''module Version
  VERSION = "1.0"
end
'''
    record = _parse_ruby_source(tmp_path, source, capture_statements=True)

    version_assignments = [
        statement for statement in record.statements
        if statement.name == "VERSION"
    ]
    assert len(version_assignments) == 1
    version = version_assignments[0]
    assert version.nodeType == "assignment"
    assert version.semanticType is None
    assert version.text == 'VERSION = "1.0"'
    assert version.startLine == 2
    assert not any(statement.semanticType == "enum_member" for statement in record.statements)


def test_ruby_nested_configuration_constants_are_not_enum_members(
    tmp_path: Path,
) -> None:
    source = b'''module Outer
  module Settings
    TIMEOUT = 30
    RETRIES = 3
  end

  class Defaults
    ENABLED = true
  end
end
'''
    record = _parse_ruby_source(tmp_path, source, capture_statements=True)

    assignments = [
        statement for statement in record.statements
        if statement.nodeType == "assignment"
    ]
    assert [(statement.name, statement.startLine) for statement in assignments] == [
        ("TIMEOUT", 3),
        ("RETRIES", 4),
        ("ENABLED", 8),
    ]
    assert len({statement.startLine for statement in assignments}) == len(assignments)
    assert not any(statement.semanticType == "enum_member" for statement in record.statements)