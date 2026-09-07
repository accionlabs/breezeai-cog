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
