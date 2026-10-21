"""Celery task names as a registered fact, ``celery.tasks``.

A Celery task is addressed by its name. ``@app.task(name="reports.send")`` gives it; otherwise
Celery names it ``<module>.<function>``, the module being the file's import path::

    # proj/tasks.py  (proj/__init__.py exists → module proj.tasks)
    @shared_task
    def cleanup(days): …                         # named proj.tasks.cleanup

That default depends on how the worker imports the file (its ``sys.path``), which the code does
not show. So a default name is used only when the code confirms it — wrong data is never
written:

* the name itself appears as a literal somewhere in the repository: a beat entry
  (``"task": "proj.tasks.cleanup"``), ``send_task("proj.tasks.cleanup")``,
  ``signature("proj.tasks.cleanup")``; or
* the module is named in Celery's own task-module configuration:
  ``autodiscover_tasks(["proj"])`` (→ ``proj.tasks``; ``related_name`` changes the module),
  ``Celery(…, include=["proj.tasks"])``, ``app.conf.include`` / ``imports``.

Otherwise the name is unknown (``None``). The module is the file's dotted path, walking up while
each folder has an ``__init__.py``. Collected from every file (gate: Celery markers), resolved
once: file → function → name, and name → the function, for the Celery detector (:mod:`.celery`).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text

FACT = "celery.tasks"

#: An explicit ``name=`` that is not a literal: the task has a name, but it is not known.
UNKNOWN_NAME = "\0"
_CONFIG_LISTS = frozenset({"include", "imports", "CELERY_IMPORTS", "CELERY_INCLUDE"})


@dataclass(frozen=True)
class TaskDef:
    """A function decorated as a Celery task."""

    function: str
    #: ``name=`` when given: its literal, or :data:`UNKNOWN_NAME`; ``None`` = not given.
    explicit: str | None
    decorator: Node
    definition: Node


@dataclass(frozen=True)
class CeleryTasks:
    #: file → {task function → its name, ``None`` when not known}.
    by_file: dict[str, dict[str, str | None]] = field(default_factory=dict)
    #: task name → (file, function); a name two tasks share → ``None``.
    by_name: dict[str, tuple[str, str] | None] = field(default_factory=dict)


def celery_tasks(index: Any | None) -> CeleryTasks | None:
    fact: CeleryTasks | None = index_fact(index, FACT)
    return fact


def string(node: Node | None, source: bytes) -> str | None:
    """A plain string literal's value (no f-string, no implicit concatenation)."""
    if node is None or node.type != "string":
        return None
    parts = node.named_children
    if any(p.type == "interpolation" for p in parts):
        return None
    content = [p for p in parts if p.type == "string_content"]
    return node_text(content[0], source) if len(content) == 1 else (
        "" if not content else None)


def walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.named_children))


def _callee(call: Node, source: bytes) -> str:
    fn = call.child_by_field_name("function")
    return node_text(fn, source) if fn is not None else ""


def kwarg(call: Node, name: str, source: bytes) -> Node | None:
    args = call.child_by_field_name("arguments")
    for a in args.named_children if args is not None else []:
        key = a.child_by_field_name("name") if a.type == "keyword_argument" else None
        if key is not None and node_text(key, source) == name:
            return a.child_by_field_name("value")
    return None


def positional(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return [a for a in (args.named_children if args is not None else [])
            if a.type not in ("keyword_argument", "comment", "list_splat", "dictionary_splat")]


def task_defs(root: Node, source: bytes) -> list[TaskDef]:
    """Functions decorated ``@shared_task`` / ``@<app>.task`` (with or without arguments), in a
    file that uses Celery."""
    if b"celery" not in source and b"shared_task" not in source:
        return []
    out: list[TaskDef] = []
    for node in walk(root):
        if node.type != "decorated_definition":
            continue
        fdef = node.child_by_field_name("definition")
        name = fdef.child_by_field_name("name") if fdef is not None and \
            fdef.type == "function_definition" else None
        if fdef is None or name is None:
            continue
        for dec in (c for c in node.named_children if c.type == "decorator"):
            expr = dec.named_children[0] if dec.named_children else None
            call = expr if expr is not None and expr.type == "call" else None
            target = call.child_by_field_name("function") if call is not None else expr
            text = node_text(target, source) if target is not None else ""
            if text not in ("shared_task", "celery.shared_task") and not (
                    target is not None and target.type == "attribute" and text.endswith(".task")):
                continue
            explicit: str | None = None
            value = kwarg(call, "name", source) if call is not None else None
            if value is not None:
                explicit = string(value, source) or UNKNOWN_NAME
            out.append(TaskDef(node_text(name, source), explicit, dec, fdef))
            break
    return out


def beat_entries(root: Node, source: bytes) -> Iterator[tuple[Node, Node | None, Node | None]]:
    """``(entry, task, schedule)`` per beat-schedule entry: a dict assigned to ``beat_schedule``
    (``app.conf.beat_schedule = …``, ``CELERY_BEAT_SCHEDULE = …``) or passed as
    ``beat_schedule=…``, each value a dict with ``"task"`` and ``"schedule"``."""
    for node in walk(root):
        value: Node | None = None
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            if left is not None and node_text(left, source).lower().endswith("beat_schedule"):
                value = node.child_by_field_name("right")
        elif node.type == "keyword_argument":
            key = node.child_by_field_name("name")
            if key is not None and node_text(key, source) == "beat_schedule":
                value = node.child_by_field_name("value")
        if value is None or value.type != "dictionary":
            continue
        for pair in value.named_children:
            entry = pair.child_by_field_name("value") if pair.type == "pair" else None
            if entry is None or entry.type != "dictionary":
                continue
            fields = {string(p.child_by_field_name("key"), source): p.child_by_field_name("value")
                      for p in entry.named_children if p.type == "pair"}
            yield pair, fields.get("task"), fields.get("schedule")


def module_name(rel: str, repo_root: Path | None) -> str | None:
    """``proj/tasks.py`` → ``proj.tasks`` when ``proj/__init__.py`` exists; a file outside any
    package → its stem; ``None`` without the repository to look at."""
    if repo_root is None:
        return None
    p = PurePosixPath(rel)
    parts = [] if p.name == "__init__.py" else [p.stem]
    folder = p.parent
    while folder.name and (repo_root / folder / "__init__.py").is_file():
        parts.insert(0, folder.name)
        folder = folder.parent
    return ".".join(parts) or None


@dataclass(frozen=True)
class _FileTasks:
    rel: str
    module: str | None
    tasks: tuple[tuple[str, str | None], ...]  # (function, explicit name)
    used: frozenset[str]  # task names written as literals
    modules: frozenset[str]  # task modules named in Celery's configuration


def _strings(node: Node | None, source: bytes) -> list[str]:
    if node is None or node.type not in ("list", "tuple"):
        return []
    return [s for s in (string(c, source) for c in node.named_children) if s]


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> _FileTasks | None:
    tasks = tuple((t.function, t.explicit) for t in task_defs(root, source))
    used: set[str] = set()
    modules: set[str] = set()
    for _, task, _ in beat_entries(root, source):
        name = string(task, source)
        if name:
            used.add(name)
    for node in walk(root):
        if node.type == "call":
            callee = _callee(node, source).rsplit(".", 1)[-1]
            args = positional(node)
            if callee in ("send_task", "signature", "Signature") and args:
                name = string(args[0], source)
                if name:
                    used.add(name)
            elif callee == "autodiscover_tasks":
                related = string(kwarg(node, "related_name", source), source) or "tasks"
                pkgs = _strings(args[0] if args else kwarg(node, "packages", source), source)
                modules.update(f"{pkg}.{related}" for pkg in pkgs)
            elif callee == "Celery":
                modules.update(_strings(kwarg(node, "include", source), source))
        elif node.type == "assignment":
            left = node.child_by_field_name("left")
            name = node_text(left, source).rsplit(".", 1)[-1] if left is not None else ""
            if name in _CONFIG_LISTS:
                modules.update(_strings(node.child_by_field_name("right"), source))
    if not tasks and not used and not modules:
        return None
    return _FileTasks(rel, module_name(rel, repo_root), tasks, frozenset(used), frozenset(modules))


def _resolve(values: list[_FileTasks], index: object) -> CeleryTasks | None:
    used = {n for v in values for n in v.used}
    modules = {m for v in values for m in v.modules}
    by_file: dict[str, dict[str, str | None]] = {}
    by_name: dict[str, tuple[str, str] | None] = {}
    for v in values:
        for function, explicit in v.tasks:
            name: str | None
            if explicit is not None:
                name = None if explicit == UNKNOWN_NAME else explicit
            else:
                derived = f"{v.module}.{function}" if v.module else None
                name = derived if derived and (derived in used or v.module in modules) else None
            record_distinct(by_file.setdefault(v.rel, {}), function, name)
            if name is not None:
                record_distinct(by_name, name, (v.rel, function))
    return CeleryTasks(by_file, by_name) if by_file else None


register_fact(FactSpec(
    name=FACT, language="python",
    gate=(b"celery", b"CELERY", b"shared_task", b"beat_schedule", b"send_task"),
    collect=_collect, resolve=_resolve,
))
