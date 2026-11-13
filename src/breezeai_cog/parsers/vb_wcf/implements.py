"""VB interface members and the methods that implement them, the registered fact
``vb.interface-members``.

VB names the member a method implements in its header, so the method may be named differently::

    Public Interface IOrderService                                  ' Contracts/IOrderService.vb
        <OperationContract()> Function GetOrder(id As String) As Order
    Public Class OrderService                                       ' Services/OrderService.svc.vb
        Implements IOrderService
        Public Function Fetch(id As String) As Order Implements IOrderService.GetOrder
            → IOrderService.GetOrder is run by OrderService.Fetch (Services/OrderService.svc.vb:5)

The contract and its implementation are usually in different files, so the clause is collected
while the VB index is built: ``interface.member`` (the interface's simple name, generic arguments
dropped, lower-cased — VB names are case-insensitive) → the class, method, file and line, and
``interface`` → the one class implementing it. A member or interface implemented by more than
one class has no entry (honest-null: the interface method stays the handler).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..treesitter import node_text
from ..vb.functions import method_name
from ..vb.source import member_implements

FACT = "vb.interface-members"

_TYPE_BLOCKS = ("class_block", "structure_block", "struct_block", "module_block")
_GENERIC_ARGS = re.compile(r"\s*\(\s*Of\b[^()]*\)", re.IGNORECASE)


#: One collected clause: ``(interface, member, class, method, file, line)``.
_Clause = tuple[str, str, str, str, str, int]


class Member(NamedTuple):
    cls: str
    method: str
    path: str
    line: int


@dataclass(frozen=True)
class InterfaceMembers:
    members: dict[str, Member] = field(default_factory=dict)
    #: interface (lower-cased simple name) → (implementing class, its file)
    implementers: dict[str, tuple[str, str]] = field(default_factory=dict)

    def member(self, interface: str, name: str) -> Member | None:
        return self.members.get(f"{_simple(interface)}.{name}".lower())

    def implementer(self, interface: str) -> tuple[str, str] | None:
        return self.implementers.get(_simple(interface).lower())


def interface_members(index: Any | None) -> InterfaceMembers:
    """The repo's implemented interface members (empty without an index)."""
    return index_fact(index, FACT) or InterfaceMembers()


def _simple(name: str) -> str:
    """``Contracts.IRepository(Of T)`` → ``IRepository``."""
    return _GENERIC_ARGS.sub("", name).strip().rsplit(".", 1)[-1].strip("[]")


def _split(member: str) -> tuple[str, str] | None:
    """``IRepository(Of T).GetAll`` → ``("IRepository", "GetAll")``."""
    plain = _GENERIC_ARGS.sub("", member).strip()
    if "." not in plain:
        return None
    iface, name = plain.rsplit(".", 1)
    return _simple(iface), name.strip("[]")


def _owner(node: Node, source: bytes) -> str | None:
    up = node.parent
    while up is not None and up.type not in _TYPE_BLOCKS:
        up = up.parent
    name = up.child_by_field_name("name") if up is not None else None
    return node_text(name, source) if name is not None else None


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> list[_Clause] | None:
    """``(interface, member, class, method, line)`` for each method or property whose header
    names the members it implements (one tree: a nesting level's)."""
    clauses = member_implements(source)
    if not clauses:
        return None
    found: list[_Clause] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type not in ("method_declaration", "property_declaration"):
            continue
        name, broken = method_name(node, source)
        name_node = node.child_by_field_name("name")
        if not name or broken or name_node is None:
            continue
        members = clauses.get(name_node.start_point[0] + 1)
        owner = _owner(node, source) if members else None
        if owner is None:
            continue
        for written in members or []:
            split = _split(written)
            if split is not None:
                found.append((*split, owner, name, rel, node.start_point[0] + 1))
    return found or None


def _resolve(values: list[list[_Clause]], index: object) -> InterfaceMembers | None:
    members: dict[str, Member | None] = {}
    implementers: dict[str, tuple[str, str] | None] = {}
    for per_file in values:
        for iface, member, cls, method, rel, line in per_file:
            key = f"{iface}.{member}".lower()
            hit = Member(cls, method, rel, line)
            members[key] = hit if members.get(key, hit) == hit else None
            impl = (cls, rel)
            implementers[iface.lower()] = impl if implementers.get(iface.lower(), impl) == impl else None
    out = InterfaceMembers(
        {k: v for k, v in members.items() if v is not None},
        {k: v for k, v in implementers.items() if v is not None},
    )
    return out if out.members else None


register_fact(FactSpec(
    name=FACT, language="vb", gate=(b"Implements", b"implements", b"IMPLEMENTS"),
    collect=_collect, resolve=_resolve,
))
