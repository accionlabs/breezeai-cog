"""C# names bound by scope: a member's locals and parameters, then the type's fields and
properties, its same-file bases, then outer types — not one map per file."""

from __future__ import annotations

from tree_sitter import Node

from breezeai_cog.parsers.csharp.scopes import TypeScopes, label_of
from breezeai_cog.parsers.csharp.statements import typed
from breezeai_cog.parsers.treesitter import node_text, parse_source

_S3 = typed({"IAmazonS3": "aws-s3", "AmazonS3Client": "aws-s3"})

SRC = b'''class Mine {
  IAmazonS3 _s3;
  void A() { _s3.Get(); }
  void B(IMyStore _s3) { _s3.Get(); this._s3.Get(); }
  void C() { items.ForEach(x => _s3.Get()); }
}
class Theirs {
  IMyStore _s3;
  void A() { _s3.Get(); }
}
class Base { protected IAmazonS3 Store { get; } }
class Derived : Base {
  void A() { Store.Get(); }
  class Inner { void A() { _s3.Get(); } }
}
partial class Split { IAmazonS3 _s3; }
partial class Split { void A() { _s3.Get(); } }
class Locals {
  void A() { var s3 = new AmazonS3Client(); s3.Get(); }
  void B() { if (a) { var s3 = new AmazonS3Client(); } else { var s3 = new Other(); } s3.Get(); }
  Func<int> F = () => { var s3 = new AmazonS3Client(); return s3.Get(); };
}
var top = new AmazonS3Client();
class SeesNoTopLevel { void A() { top.Get(); } }
'''


def _receivers(root: Node, source: bytes) -> list[tuple[int, str, Node]]:
    """(line, receiver text, receiver node) of every ``x.Get()``."""
    out = []

    def walk(n: Node) -> None:
        if n.type == "member_access_expression" and node_text(
                n.child_by_field_name("name"), source) == "Get":
            obj = n.child_by_field_name("expression")
            out.append((obj.start_point[0] + 1, node_text(obj, source), obj))
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def test_each_use_binds_to_its_own_scope() -> None:
    root = parse_source("csharp", SRC).root_node
    scopes = TypeScopes(root, SRC)
    got = []
    for line, text, node in _receivers(root, SRC):
        this = text.startswith("this.")
        name = text.removeprefix("this.")
        got.append((line, text, label_of(scopes.lookup(node, name, this_only=this), _S3)))
    assert got == [
        (3, "_s3", "aws-s3"),       # the field
        (4, "_s3", None),           # a parameter of another type shadows the field
        (4, "this._s3", "aws-s3"),  # …but ``this._s3`` is still the field
        (5, "_s3", "aws-s3"),       # inside a lambda of the member
        (9, "_s3", None),           # another class's field of the same name
        (13, "Store", "aws-s3"),    # a property of a same-file base class
        (14, "_s3", None),          # an outer class without the field
        (17, "_s3", "aws-s3"),      # the other part of a partial class
        (19, "s3", "aws-s3"),       # ``var`` known from ``new``
        (20, "s3", None),           # two locals of different types: not trusted
        (21, "s3", "aws-s3"),       # a lambda in a field initializer is its own scope
        (24, "top", None),          # top-level locals are not visible in a type
    ]


def test_outer_class_fields_are_visible_to_nested_types() -> None:
    src = b"class Outer { IAmazonS3 _s3; class Inner { void A() { _s3.Get(); } } }"
    root = parse_source("csharp", src).root_node
    (_, _, node), = _receivers(root, src)
    assert label_of(TypeScopes(root, src).lookup(node, "_s3"), _S3) == "aws-s3"
