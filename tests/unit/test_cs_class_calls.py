"""C# code outside any method makes calls: field / event initializers and property bodies and
initializers give ``Class.calls``; a constructor's ``: base(…)`` / ``: this(…)`` arguments are the
constructor's calls. ``nameof`` is no call."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records

SRC = """namespace N {
  static class Rules { public static bool IsNotWhitespace(string s) => true; public static int Run() => 1; }
  class Base { public Base(int x) { } }
  class Order : Base {
    private readonly int _seed = Rules.Run();
    public string Name { get; set; }
    public bool IsValid => Rules.IsNotWhitespace(Name);
    public string Label { get { return nameof(Name); } set { Rules.Run(); } }
    public Order() : base(Rules.Run()) { Audit(); }
    void Audit() { }
    class Inner { int y = Rules.Run(); }
  }
  class Plain { public int X { get; set; } void M() { Rules.Run(); } }
}
"""


def test_class_level_and_constructor_initializer_calls(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "Order.cs").write_text(SRC)
    (rec,) = list(iter_file_records(repo))
    classes = {c.name: [(x.name, x.path) for x in (c.calls or [])] for c in rec.classes}
    assert classes["Order"] == [("Run", "Order.cs"), ("IsNotWhitespace", "Order.cs")]
    assert classes["Inner"] == [("Run", "Order.cs")]     # a nested class's own
    assert classes["Plain"] == [] and classes["Rules"] == []
    ctor = next(f for f in rec.functions if f.id.split("#")[1] == "Order" and f.name == "Order")
    assert [(c.name, c.path) for c in ctor.calls] == [("Run", "Order.cs"), ("Audit", "Order.cs")]
