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
    source = b'''class User < ActiveRecord::Base
end

class Data
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


def test_ruby_fetch_lookups_are_not_http_but_bare_fetch_keeps_existing_behavior(
    tmp_path: Path,
) -> None:
    source = b'''def read_config
  ENV.fetch("PORT", 3000)
  cop_config.fetch(:Enabled)
  fetch('/remote')
end
'''
    path = tmp_path / "config.rb"
    path.write_bytes(source)
    record = RubyParser().parse_file(ParseContext(
        path="config.rb", abs_path=path, source=source, repo_root=tmp_path,
        capture_statements=True,
    ))

    calls = [item for item in record.statements if item.semanticType == "api_call"]
    assert [(item.method, item.endpoint) for item in calls] == [("GET", "/remote")]


def test_ruby_api_url_interpolation_is_null_but_static_urls_are_preserved(
    tmp_path: Path,
) -> None:
    source = b'''def fetch_all(id)
  Net::HTTP.get("https://api.example.com/users/#{id}")
  Net::HTTP.get('https://api.example.com/users/#{id}')
  Net::HTTP.get("https://api.example.com/health")
end
'''
    path = tmp_path / "client.rb"
    path.write_bytes(source)
    record = RubyParser().parse_file(ParseContext(
        path="client.rb", abs_path=path, source=source, repo_root=tmp_path,
        capture_statements=True,
    ))
    calls = [item for item in record.statements if item.semanticType == "api_call"]

    assert [item.endpoint for item in calls] == [
        None,
        "https://api.example.com/users/#{id}",
        "https://api.example.com/health",
    ]
    assert not any(
        endpoint in {"https://api.example.com/users/", "https://api.example.com/users/{param}"}
        for endpoint in (item.endpoint for item in calls)
    )


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


def test_ruby_private_symbol_marks_only_named_method_private(tmp_path: Path) -> None:
    source = b'''class Account
  def open
  end

  def close
  end

  private :close
end
'''
    path = tmp_path / "account.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="account.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    visibility = {function.name: function.visibility for function in rec.functions}
    assert visibility == {"open": "public", "close": "private"}


def test_ruby_top_level_visibility_markers_apply_within_file_scope(tmp_path: Path) -> None:
    source = b'''private
def hidden
end

def also_hidden
end

public
def visible
end
'''
    path = tmp_path / "top_level.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="top_level.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    visibility = {function.name: function.visibility for function in rec.functions}
    assert visibility == {
        "hidden": "private",
        "also_hidden": "private",
        "visible": "public",
    }


def test_ruby_bare_top_level_method_defaults_private_and_nonstatic(tmp_path: Path) -> None:
    source = b'''def top_level_method
end
class Example
  def instance_method
  end
  def self.singleton_method
  end
end
'''
    path = tmp_path / "visibility_defaults.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="visibility_defaults.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))
    functions = {item.name: item for item in rec.functions}
    assert functions["top_level_method"].visibility == "private"
    assert functions["top_level_method"].isStatic is None
    assert functions["instance_method"].visibility == "public"
    assert functions["instance_method"].isStatic is None
    assert functions["singleton_method"].visibility == "public"
    assert functions["singleton_method"].isStatic is True


def test_ruby_nested_scopes_have_independent_visibility(tmp_path: Path) -> None:
    source = b'''class Outer
  private
  def outer_hidden
  end

  class Inner
    def inner_visible
    end

    protected
    def inner_protected
    end
  end
end
'''
    path = tmp_path / "nested.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="nested.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    visibility = {function.name: function.visibility for function in rec.functions}
    assert visibility == {
        "outer_hidden": "private",
        "inner_visible": "public",
        "inner_protected": "protected",
    }


def test_ruby_singleton_methods_follow_visibility_markers(tmp_path: Path) -> None:
    source = b'''class Account
  private
  def self.hidden
  end

  public
  def self.visible
  end
end
'''
    path = tmp_path / "account.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="account.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    visibility = {function.name: (function.visibility, function.isStatic) for function in rec.functions}
    assert visibility == {
        "hidden": ("public", True),
        "visible": ("public", True),
    }


def test_ruby_conditional_class_methods_keep_class_owner(tmp_path: Path) -> None:
    source = b'''class Conditional
  if RUBY_VERSION
    def enabled(value)
    end
  else
    def fallback
    end
  end
  unless DISABLED
    def available
    end
  end
end
'''
    path = tmp_path / "conditional.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="conditional.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))
    cls = next(item for item in rec.classes if item.name == "Conditional")
    methods = [item for item in rec.functions if item.name in {"enabled", "fallback", "available"}]
    assert {item.name for item in methods} == {"enabled", "fallback", "available"}
    assert len(methods) == 3
    assert all(item.parentId == cls.id for item in methods)
    assert next(item for item in methods if item.name == "enabled").params[0].name == "value"


def test_ruby_methods_in_visibility_and_control_flow_contexts(tmp_path: Path) -> None:
    source = b'''class ContextMethods
  private def inline_private(value)
    value
  end
  protected def inline_protected
  end
  public def inline_public
  end
  private_class_method def self.inline_class
  end
  begin
    def in_begin(arg)
      arg
    end
    private def private_in_begin
    end
  end
  case mode
  when :one
    def in_case
    end
  end
  def in_modifier
  end if RUBY_VERSION
end
'''
    path = tmp_path / "contexts.rb"
    path.write_bytes(source)
    record = RubyParser().parse_file(ParseContext(
        path="contexts.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    context_class = next(item for item in record.classes if item.name == "ContextMethods")
    methods = {item.name: item for item in record.functions if item.parentId == context_class.id}
    assert set(methods) == {
        "inline_private", "inline_protected", "inline_public", "inline_class",
        "in_begin", "private_in_begin", "in_case", "in_modifier",
    }
    assert {name: methods[name].visibility for name in methods} == {
        "inline_private": "private",
        "inline_protected": "protected",
        "inline_public": "public",
        "inline_class": "private",
        "in_begin": "public",
        "private_in_begin": "private",
        "in_case": "public",
        "in_modifier": "public",
    }
    assert methods["inline_class"].isStatic is True
    assert {name: (methods[name].startLine, methods[name].endLine) for name in methods} == {
        "inline_private": (2, 4),
        "inline_protected": (5, 6),
        "inline_public": (7, 8),
        "inline_class": (9, 10),
        "in_begin": (12, 14),
        "private_in_begin": (15, 16),
        "in_case": (20, 21),
        "in_modifier": (23, 24),
    }


def test_ruby_definition_walk_does_not_descend_into_arbitrary_calls(tmp_path: Path) -> None:
    source = b'''class Scoped
  configure do
    def dynamically_added
    end
  end
  value = "def not_a_method; end"
end
'''
    path = tmp_path / "scoped.rb"
    path.write_bytes(source)
    record = RubyParser().parse_file(ParseContext(
        path="scoped.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    assert not any(item.name in {"dynamically_added", "not_a_method"} for item in record.functions)


def test_ruby_dynamic_class_eval_method_is_not_assigned_lexical_owner(tmp_path: Path) -> None:
    source = b'''class Dynamic
  class_eval do
    def injected; end
  end
end
'''
    path = tmp_path / "dynamic.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="dynamic.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))
    assert not any(item.name == "injected" for item in rec.functions)


def test_ruby_operator_and_setter_method_names_are_preserved(tmp_path: Path) -> None:
    source = b'''class Operators
  def [](index); end
  def []=(index, value); end
  def ==(other); end
  def <=>(other); end
  def <<(value); end
  def default=(value); end
  def app=(value); end
  def ordinary(value); end
end
'''
    path = tmp_path / "operators.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="operators.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))
    expected = {"[]", "[]=", "==", "<=>", "<<", "default=", "app=", "ordinary"}
    methods = [item for item in rec.functions if item.parentId != rec.id]
    assert {item.name for item in methods} == expected
    assert len({item.id for item in methods}) == len(methods)


def test_ruby_private_class_method_only_marks_named_singleton_method(tmp_path: Path) -> None:
    source = b'''class Visibility
  def instance_method; end
  def self.hidden; end
  def self.also_hidden; end
  def self.visible; end
  private_class_method :hidden, 'also_hidden'
end
'''
    path = tmp_path / "visibility.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="visibility.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))
    visibility = {item.name: (item.visibility, item.isStatic) for item in rec.functions}
    assert visibility == {
        "instance_method": ("public", None),
        "hidden": ("private", True),
        "also_hidden": ("private", True),
        "visible": ("public", True),
    }


def test_ruby_qualified_and_nested_class_names_preserve_contract(tmp_path: Path) -> None:
    source = b'''class Rack::Builder < Rack::Base
  def build; end
end
class Outer
  class Inner
  end
end
'''
    path = tmp_path / "builder.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="builder.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))
    builder = next(item for item in rec.classes if item.name == "Rack::Builder")
    build = next(item for item in rec.functions if item.name == "build")
    assert builder.startLine == 1
    assert builder.extends == "Rack::Base"
    assert builder.id.endswith("Rack::Builder")
    assert build.parentId == builder.id
    assert any(item.name == "Inner" for item in rec.classes)


def test_ruby_methods_in_singleton_class_are_static_and_owned_by_class(tmp_path: Path) -> None:
    source = b'''class Base
  class << self
    private
    def requires_gem(name, version = nil)
      validate(name)
    end
  end

  def instance_method(value)
    validate(value)
  end
end
'''
    path = tmp_path / "base.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="base.rb", abs_path=path, source=source, repo_root=tmp_path,
        capture_statements=True,
    ))

    base = next(cls for cls in rec.classes if cls.name == "Base")
    requires_gem = [fn for fn in rec.functions if fn.name == "requires_gem"]
    instance_method = next(fn for fn in rec.functions if fn.name == "instance_method")

    assert len(requires_gem) == 1
    method = requires_gem[0]
    assert method.parentId == base.id
    assert method.isStatic is True
    assert method.visibility == "private"
    assert [(param.name, param.default) for param in method.params] == [
        ("name", None),
        ("version", "nil"),
    ]
    assert (method.startLine, method.endLine) == (4, 6)
    assert instance_method.parentId == base.id
    assert instance_method.isStatic is None

    validate_statements = [stmt for stmt in rec.statements if stmt.text == "validate(name)"]
    assert len(validate_statements) == 1
    assert validate_statements[0].parentId == method.id
    assert validate_statements[0].startLine == 5


def test_ruby_multiple_methods_in_singleton_class_are_extracted_once(tmp_path: Path) -> None:
    source = b'''class Example
  class << self
    def first; end
    def second(value); validate(value); end
  end
end
'''
    path = tmp_path / "example.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="example.rb", abs_path=path, source=source, repo_root=tmp_path,
        capture_statements=True,
    ))

    example = next(cls for cls in rec.classes if cls.name == "Example")
    functions = [fn for fn in rec.functions if fn.name in {"first", "second"}]

    assert {fn.name for fn in functions} == {"first", "second"}
    assert len(functions) == 2
    assert all(fn.parentId == example.id and fn.isStatic is True for fn in functions)
    assert {fn.name: (fn.startLine, fn.endLine) for fn in functions} == {
        "first": (3, 3),
        "second": (4, 4),
    }
    second = next(fn for fn in functions if fn.name == "second")
    statements = [stmt for stmt in rec.statements if stmt.text == "validate(value)"]
    assert len(statements) == 1
    assert statements[0].parentId == second.id


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


def test_ruby_chained_calls_do_not_resolve_to_same_named_local_methods(
    tmp_path: Path,
) -> None:
    source = b'''class Example
  def save; end
  def call; end
  def to_s; end
  def each; end

  def run
    x.y.save
    A.new.save
    x.call
    obj.to_s
    items.each
    save()
    self.save
    self.call
  end
end
'''
    path = tmp_path / "app.rb"
    path.write_bytes(source)
    record = RubyParser().parse_file(ParseContext(
        path="app.rb", abs_path=path, source=source, repo_root=tmp_path,
    ))

    run = next(function for function in record.functions if function.name == "run")
    by_name: dict[str, list[str | None]] = {}
    for call in run.calls:
        by_name.setdefault(call.name, []).append(call.path)

    assert by_name["save"].count(None) == 2  # x.y.save and A.new.save
    assert by_name["save"].count("app.rb") == 2  # bare save and self.save
    assert by_name["call"] == [None, "app.rb"]  # x.call and self.call
    assert by_name["to_s"] == [None]
    assert by_name["each"] == [None]
    # Nested receiver calls are kept distinct, but cannot resolve to `save` by name alone.
    assert by_name["new"] == [None]
    assert by_name["y"] == [None]


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
    assert "User" in index

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


def test_ruby_active_record_calls_require_model_inheritance(tmp_path: Path) -> None:
    source = b'''class Severity
  def self.find_by(value)
    value
  end
end

class Book < ActiveRecord::Base
end

Severity.find_by("warning")
Book.find_by(title: "Example")
'''
    path = tmp_path / "models.rb"
    path.write_bytes(source)
    rec = RubyParser().parse_file(ParseContext(
        path="models.rb", abs_path=path, source=source, repo_root=tmp_path,
        capture_statements=True,
    ))

    db_calls = {
        statement.text: statement.dataAccessHint
        for statement in rec.statements
        if statement.semanticType == "db_method_call"
    }
    assert not any(text.startswith("Severity.find_by") for text in db_calls)
    assert db_calls['Book.find_by(title: "Example")'] == "activerecord"


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


def test_ruby_typed_parameter_save_and_update(tmp_path: Path) -> None:
    """A YARD-annotated model parameter calling save/update is an ActiveRecord write."""
    src = (
        b"class User < ApplicationRecord\nend\n\n"
        b"class UsersService\n"
        b"  # @param user [User]\n"
        b"  def deactivate(user)\n"
        b"    user.save\n"
        b"    user.update(active: false)\n"
        b"  end\n"
        b"end\n"
    )
    p = tmp_path / "service.rb"
    p.write_bytes(src)
    rec = RubyParser().parse_file(ParseContext(
        path="service.rb", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True,
    ))
    db_stmts = [s for s in rec.statements if s.semanticType == "db_method_call"]
    calls = {s.text.strip(): (s.method, s.dataAccessHint) for s in db_stmts}
    assert any("user.save" in k for k in calls), calls
    assert any("user.update" in k for k in calls), calls
    for method, hint in calls.values():
        assert hint == "activerecord"
        assert method in {"save", "update"}

    # Verify Parameter signature extraction
    fn = next(f for f in rec.functions if f.name == "deactivate")
    assert len(fn.params) == 1
    assert fn.params[0].name == "user"
    assert fn.params[0].type == "User"


def test_ruby_parameter_defaults_are_not_model_type_hints(tmp_path: Path) -> None:
    src = (
        b"class User < ApplicationRecord\nend\n\n"
        b"class UsersService\n"
        b"  def keyword_default(user: User)\n"
        b"    user.save\n"
        b"  end\n\n"
        b"  def expression_default(user = User.new)\n"
        b"    user.save\n"
        b"  end\n"
        b"end\n"
    )
    path = tmp_path / "service.rb"
    path.write_bytes(src)
    record = RubyParser().parse_file(ParseContext(
        path="service.rb",
        abs_path=path,
        source=src,
        repo_root=tmp_path,
        capture_statements=True,
    ))

    assert not any(
        statement.semanticType == "db_method_call" and "user.save" in statement.text
        for statement in record.statements
    )
    keyword_default = next(fn for fn in record.functions if fn.name == "keyword_default")
    expression_default = next(fn for fn in record.functions if fn.name == "expression_default")
    assert keyword_default.params[0].type == ""
    assert keyword_default.params[0].default == "User"
    assert expression_default.params[0].type == ""
    assert expression_default.params[0].default == "User.new"


def test_ruby_sorbet_and_yard_type_hints_recorded(tmp_path: Path) -> None:
    """Sorbet sig and YARD type hints resolve parameters to model instances."""
    src = (
        b"class User < ApplicationRecord\nend\n\n"
        b"class Handler\n"
        b"  sig { params(user: User).void }\n"
        b"  def deactivate_sorbet(user)\n"
        b"    user.save\n"
        b"  end\n\n"
        b"  # @param user [User]\n"
        b"  def archive_yard(user)\n"
        b"    user.update(archived: true)\n"
        b"  end\n"
        b"end\n"
    )
    p = tmp_path / "handler.rb"
    p.write_bytes(src)
    rec = RubyParser().parse_file(ParseContext(
        path="handler.rb", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True,
    ))
    db_stmts = [s for s in rec.statements if s.semanticType == "db_method_call"]
    calls = {s.text.strip(): (s.method, s.dataAccessHint) for s in db_stmts}
    assert any("user.save" in k for k in calls), calls
    assert any("user.update" in k for k in calls), calls


def test_ruby_association_resolved_parameters(tmp_path: Path) -> None:
    """Parameters resolved through has_many or belongs_to associations are recorded."""
    src = (
        b"class Session < ApplicationRecord\nend\n"
        b"class Account < ApplicationRecord\nend\n"
        b"class User < ApplicationRecord\n"
        b"  has_many :sessions\n"
        b"  belongs_to :account\n\n"
        b"  def deactivate_session(session)\n"
        b"    session.save\n"
        b"  end\n\n"
        b"  def attach_account(account)\n"
        b"    account.save\n"
        b"  end\n"
        b"end\n"
    )
    p = tmp_path / "user.rb"
    p.write_bytes(src)
    rec = RubyParser().parse_file(ParseContext(
        path="user.rb", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True,
    ))
    db_stmts = [s for s in rec.statements if s.semanticType == "db_method_call"]
    calls = {s.text.strip(): s.dataAccessHint for s in db_stmts}
    assert any("session.save" in k for k in calls), calls
    assert any("account.save" in k for k in calls), calls


def test_ruby_enclosing_model_scope_and_self_writes(tmp_path: Path) -> None:
    """Explicit self.save/update and association reader writes in model scope are recorded."""
    src = (
        b"class Account < ApplicationRecord\nend\n"
        b"class User < ApplicationRecord\n"
        b"  belongs_to :account\n\n"
        b"  def touch_record\n"
        b"    self.save\n"
        b"    self.update(updated_at: Time.now)\n"
        b"  end\n\n"
        b"  def sync_account\n"
        b"    account.save\n"
        b"    account.update(synced: true)\n"
        b"  end\n"
        b"end\n"
    )
    p = tmp_path / "user.rb"
    p.write_bytes(src)
    rec = RubyParser().parse_file(ParseContext(
        path="user.rb", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True,
    ))
    db_stmts = [s for s in rec.statements if s.semanticType == "db_method_call"]
    calls = {s.text.strip(): s.dataAccessHint for s in db_stmts}
    assert any("self.save" in k for k in calls), calls
    assert any("self.update" in k for k in calls), calls
    assert any("account.save" in k for k in calls), calls
    assert any("account.update" in k for k in calls), calls


def test_ruby_block_arguments_resolved_from_associations(tmp_path: Path) -> None:
    """Block arguments iterating over model associations are resolved to model instances."""
    src = (
        b"class Session < ApplicationRecord\nend\n"
        b"class User < ApplicationRecord\n"
        b"  has_many :sessions\n\n"
        b"  def deactivate_all\n"
        b"    sessions.each do |session|\n"
        b"      session.save\n"
        b"    end\n"
        b"  end\n"
        b"end\n\n"
        b"class AdminController\n"
        b"  # @param user [User]\n"
        b"  def cleanup(user)\n"
        b"    user.sessions.each do |s|\n"
        b"      s.save\n"
        b"    end\n"
        b"  end\n"
        b"end\n"
    )
    p = tmp_path / "app.rb"
    p.write_bytes(src)
    rec = RubyParser().parse_file(ParseContext(
        path="app.rb", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True,
    ))
    db_stmts = [s for s in rec.statements if s.semanticType == "db_method_call"]
    calls = {s.text.strip(): s.dataAccessHint for s in db_stmts}
    assert any("session.save" in k for k in calls), calls
    assert any("s.save" in k for k in calls), calls


def test_ruby_non_model_false_positive_protections(tmp_path: Path) -> None:
    """config.save, file.update, non-model self.save, untyped parameters, and ordinary methods remain excluded."""
    src = (
        b"class User < ApplicationRecord\nend\n"
        b"class ConfigFile\n"
        b"  def persist\n"
        b"    self.save\n"
        b"    self.update(dirty: false)\n"
        b"  end\n"
        b"end\n\n"
        b"class Service\n"
        b"  def run(config, file, user, options: Config)\n"
        b"    config.save\n"
        b"    file.update(content: 'test')\n"
        b"    user.save\n"
        b"    options.save\n"
        b"    [1, 2, 3].each do |item|\n"
        b"      item.save\n"
        b"    end\n"
        b"    configs = [config]\n"
        b"    configs.each do |c|\n"
        b"      c.save\n"
        b"    end\n"
        b"  end\n\n"
        b"  def inspect_model(user: User)\n"
        b"    user.inspect\n"
        b"    user.to_s\n"
        b"    user.email\n"
        b"    user.calculate_score\n"
        b"  end\n"
        b"end\n"
    )
    p = tmp_path / "service.rb"
    p.write_bytes(src)
    rec = RubyParser().parse_file(ParseContext(
        path="service.rb", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True,
    ))
    db_stmts = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert len(db_stmts) == 0, [s.text for s in db_stmts]


def test_ruby_cross_file_associations_with_model_index(tmp_path: Path) -> None:
    """Cross-file repo index resolves associations and typed parameters across files."""
    models_dir = tmp_path / "app" / "models"
    models_dir.mkdir(parents=True)
    user_rb = models_dir / "user.rb"
    user_rb.write_bytes(
        b"class User < ApplicationRecord\n"
        b"  has_many :sessions\n"
        b"  belongs_to :account\n"
        b"end\n"
    )
    session_rb = models_dir / "session.rb"
    session_rb.write_bytes(b"class Session < ApplicationRecord\nend\n")
    account_rb = models_dir / "account.rb"
    account_rb.write_bytes(b"class Account < ApplicationRecord\nend\n")

    svc_rb = tmp_path / "app" / "services" / "deactivator.rb"
    svc_rb.parent.mkdir(parents=True)
    svc_rb.write_bytes(
        b"class Deactivator\n"
        b"  # @param user [User]\n"
        b"  def deactivate(user)\n"
        b"    user.save\n"
        b"    user.sessions.each do |session|\n"
        b"      session.save\n"
        b"    end\n"
        b"    user.account.save\n"
        b"    config.save\n"
        b"  end\n"
        b"end\n"
    )

    parser = RubyParser()
    index = parser.build_index(tmp_path, [user_rb, session_rb, account_rb, svc_rb], jobs=2)
    assert "User" in index
    assert "Session" in index
    assert "Account" in index
    assert "sessions" in index.associations["User"]
    assert "account" in index.associations["User"]

    rec = parser.parse_file(ParseContext(
        path="app/services/deactivator.rb", abs_path=svc_rb,
        source=svc_rb.read_bytes(), repo_root=tmp_path,
        capture_statements=True, resolution_index=index,
    ))
    db_stmts = [s for s in rec.statements if s.semanticType == "db_method_call"]
    calls = {s.text.strip(): s.dataAccessHint for s in db_stmts}
    assert any("user.save" in k for k in calls), calls
    assert any("session.save" in k for k in calls), calls
    assert any("user.account.save" in k for k in calls), calls
    # config.save must remain excluded
    assert not any("config.save" in k for k in calls), calls


def test_ruby_model_index_includes_supported_non_rb_source(tmp_path: Path) -> None:
    """A Ruby model in a supported project file resolves calls from a Ruby service."""
    model_file = tmp_path / "models" / "severity.rake"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"class Severity < ApplicationRecord\nend\n")
    service_file = tmp_path / "services" / "alerts.rb"
    service_file.parent.mkdir(parents=True)
    service_file.write_bytes(
        b'class Alerts\n  def find_severity\n'
        b'    Severity.find_by(name: "critical")\n  end\nend\n'
    )

    parser = RubyParser()
    index = parser.build_index(tmp_path, [model_file, service_file], jobs=1)
    record = parser.parse_file(ParseContext(
        path="services/alerts.rb",
        abs_path=service_file,
        source=service_file.read_bytes(),
        repo_root=tmp_path,
        capture_statements=True,
        resolution_index=index,
    ))

    db_calls = [s for s in record.statements if s.semanticType == "db_method_call"]
    assert "Severity" in index
    assert len(db_calls) == 1
    assert db_calls[0].dataAccessHint == "activerecord"


def test_ruby_model_index_resolves_namespaced_superclasses_across_files(tmp_path: Path) -> None:
    base_file = tmp_path / "app" / "models" / "application_record.rb"
    base_file.parent.mkdir(parents=True)
    base_file.write_bytes(
        b"module Billing\n"
        b"  class ApplicationRecord < ActiveRecord::Base\n  end\n"
        b"end\n"
    )
    invoice_file = tmp_path / "app" / "models" / "invoice.rb"
    invoice_file.write_bytes(
        b"module Billing\n"
        b"  class Invoice < ApplicationRecord\n  end\n"
        b"end\n"
    )
    receipt_file = tmp_path / "app" / "models" / "receipt.rb"
    receipt_file.write_bytes(b"class Receipt < ::Billing::ApplicationRecord\nend\n")
    service_file = tmp_path / "app" / "services" / "billing.rb"
    service_file.parent.mkdir(parents=True)
    service_file.write_bytes(
        b"class BillingService\n"
        b"  def lookups\n"
        b"    Billing::Invoice.find_by(number: 1)\n"
        b"    Receipt.find_by(number: 2)\n"
        b"  end\n"
        b"end\n"
    )

    parser = RubyParser()
    index = parser.build_index(
        tmp_path, [base_file, invoice_file, receipt_file, service_file], jobs=1
    )
    record = parser.parse_file(ParseContext(
        path="app/services/billing.rb",
        abs_path=service_file,
        source=service_file.read_bytes(),
        repo_root=tmp_path,
        capture_statements=True,
        resolution_index=index,
    ))
    db_calls = [s.text for s in record.statements if s.semanticType == "db_method_call"]

    assert "Billing::Invoice" in index
    assert "Receipt" in index
    assert any("Billing::Invoice.find_by" in call for call in db_calls), db_calls
    assert any("Receipt.find_by" in call for call in db_calls), db_calls


def test_ruby_model_index_rejects_ambiguous_terminal_names(tmp_path: Path) -> None:
    model_file = tmp_path / "app" / "models" / "models.rb"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(
        b"module Billing\n"
        b"  class User < ActiveRecord::Base\n  end\n"
        b"end\n"
        b"module Admin\n"
        b"  class User < ActiveRecord::Base\n  end\n"
        b"end\n"
        b"module Legacy\n"
        b"  class User\n  end\n"
        b"end\n"
        b"class User < ActiveRecord::Base\nend\n"
    )
    service_file = tmp_path / "app" / "services" / "lookups.rb"
    service_file.parent.mkdir(parents=True)
    service_file.write_bytes(
        b"class Lookups\n"
        b"  def find_users\n"
        b"    Billing::User.find_by(active: true)\n"
        b"    Admin::User.find_by(active: true)\n"
        b"    Legacy::User.find_by(active: true)\n"
        b"    User.find_by(active: true)\n"
        b"    ::User.find_by(active: true)\n"
        b"  end\n"
        b"end\n"
    )

    parser = RubyParser()
    index = parser.build_index(tmp_path, [model_file, service_file], jobs=1)
    record = parser.parse_file(ParseContext(
        path="app/services/lookups.rb",
        abs_path=service_file,
        source=service_file.read_bytes(),
        repo_root=tmp_path,
        capture_statements=True,
        resolution_index=index,
    ))
    db_calls = [s.text for s in record.statements if s.semanticType == "db_method_call"]

    assert "User" not in index
    assert "Billing::User" in index
    assert "Admin::User" in index
    assert "::User" in index
    assert any("Billing::User.find_by" in call for call in db_calls), db_calls
    assert any("Admin::User.find_by" in call for call in db_calls), db_calls
    assert any("::User.find_by" in call for call in db_calls), db_calls
    assert not any("Legacy::User.find_by" in call for call in db_calls), db_calls
    assert not any("User.find_by" in call and "::User" not in call for call in db_calls), db_calls


def test_ruby_model_index_does_not_apply_alias_to_other_qualified_receiver(
    tmp_path: Path,
) -> None:
    model_file = tmp_path / "app" / "models" / "user.rb"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(
        b"module Billing\n"
        b"  class User < ActiveRecord::Base\n  end\n"
        b"end\n"
    )
    service_file = tmp_path / "app" / "services" / "lookups.rb"
    service_file.parent.mkdir(parents=True)
    service_file.write_bytes(
        b"class Lookups\n"
        b"  def find_users\n"
        b"    Billing::User.find_by(active: true)\n"
        b"    Other::User.find_by(active: true)\n"
        b"    User.find_by(active: true)\n"
        b"  end\n"
        b"end\n"
    )

    parser = RubyParser()
    index = parser.build_index(tmp_path, [model_file, service_file], jobs=1)
    record = parser.parse_file(ParseContext(
        path="app/services/lookups.rb",
        abs_path=service_file,
        source=service_file.read_bytes(),
        repo_root=tmp_path,
        capture_statements=True,
        resolution_index=index,
    ))
    db_calls = [s.text for s in record.statements if s.semanticType == "db_method_call"]

    assert "Billing::User" in index
    assert "User" in index  # The terminal alias is unambiguous in this repository.
    assert any("Billing::User.find_by" in call for call in db_calls), db_calls
    assert any("User.find_by" in call and "Billing::User" not in call for call in db_calls), db_calls
    assert not any("Other::User.find_by" in call for call in db_calls), db_calls


def test_extensionless_ruby_model_script_contributes_to_model_index(tmp_path: Path) -> None:
    model_file = tmp_path / "exe" / "user_model"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(
        b"#!/usr/bin/env ruby\n"
        b"class User < ActiveRecord::Base\nend\n"
    )
    service_file = tmp_path / "app" / "services" / "users.rb"
    service_file.parent.mkdir(parents=True)
    service_file.write_bytes(
        b"class Users\n"
        b"  def find\n"
        b"    User.find_by(name: 'Ada')\n"
        b"  end\n"
        b"end\n"
    )

    parser = RubyParser()
    index = parser.build_index(tmp_path, [model_file, service_file], jobs=1)
    record = parser.parse_file(ParseContext(
        path="app/services/users.rb",
        abs_path=service_file,
        source=service_file.read_bytes(),
        repo_root=tmp_path,
        capture_statements=True,
        resolution_index=index,
    ))

    assert "User" in index
    assert any(
        statement.semanticType == "db_method_call" and "User.find_by" in statement.text
        for statement in record.statements
    )
