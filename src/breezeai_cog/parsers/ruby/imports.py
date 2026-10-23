"""Ruby import extraction + repo-local resolution."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..treesitter import node_text


_RUBY_STDLIB_REQUIRES = frozenset({
    "base64", "benchmark", "bigdecimal", "cgi", "csv", "date", "delegate", "digest",
    "English", "erb", "etc", "fcntl", "fiddle", "fileutils", "forwardable", "io/console",
    "io/nonblock", "io/wait", "ipaddr", "json", "logger", "matrix", "monitor", "mutex_m",
    "net/http", "net/https", "nkf", "open-uri", "optparse", "ostruct", "pathname", "pp",
    "prettyprint", "prime", "pstore", "racc/parser", "rake", "rbconfig", "readline", "resolv",
    "ripper", "securerandom", "set", "shellwords", "singleton", "socket", "stringio", "strscan",
    "tempfile", "time", "timeout", "tmpdir", "tsort", "uri", "weakref", "yaml", "zlib",
})


def _iter(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _iter(child)


def _static_require_string(node: Node, source: bytes) -> str | None:
    """Return a literal require string; reject interpolation and other expressions."""
    if node.type != "string" or any(child.type != "string_content" for child in node.named_children):
        return None
    return "".join(node_text(child, source) for child in node.named_children)


def _inside_repo(path: Path, repo_root: Path) -> bool:
    try:
        path.relative_to(repo_root)
        return True
    except ValueError:
        return False


def _ruby_file_candidate(base: Path, req: str) -> Path | None:
    requested = Path(req)
    if requested.suffix:
        return base / requested if requested.suffix == ".rb" else None
    return (base / requested).with_suffix(".rb")


def _resolve_require_relative(req: str, file_path: str, repo_root: Path) -> str | None:
    """Resolve Ruby's explicit file-relative require relative to the importing file."""
    if not req:
        return None
    importing_file = Path(file_path)
    if not importing_file.is_absolute():
        importing_file = repo_root / importing_file
    candidate = _ruby_file_candidate(importing_file.parent, req)
    if candidate is None:
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError):
        return None
    if resolved.suffix == ".rb" and _inside_repo(resolved, repo_root):
        return repo_relative(resolved, repo_root)
    return None


def _load_path_roots(file_path: str, repo_root: Path) -> list[Path]:
    """Known conventional Ruby load roots, without treating cwd or importer dir as $LOAD_PATH."""
    roots: dict[Path, None] = {}
    candidates = [repo_root / "lib", *repo_root.glob("*/lib")]
    importing_file = Path(file_path)
    if not importing_file.is_absolute():
        importing_file = repo_root / importing_file
    parent = importing_file.parent
    while _inside_repo(parent, repo_root):
        candidates.append(parent / "lib")
        if parent == repo_root:
            break
        parent = parent.parent
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if resolved.is_dir() and _inside_repo(resolved, repo_root):
            roots.setdefault(resolved, None)
    return list(roots)


def _resolve_bare_require(req: str, file_path: str, repo_root: Path) -> str | None:
    """Resolve only a unique conventional lib-root target; otherwise remain unresolved."""
    if not req or req in _RUBY_STDLIB_REQUIRES:
        return None
    matches: dict[str, None] = {}
    for load_root in _load_path_roots(file_path, repo_root):
        candidate = _ruby_file_candidate(load_root, req)
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.suffix == ".rb" and _inside_repo(resolved, repo_root):
            matches.setdefault(repo_relative(resolved, repo_root), None)
    return next(iter(matches)) if len(matches) == 1 else None


def extract_imports(
    root: Node,
    source: bytes,
    file_path: str,
    repo_root: str | Path,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    """Extract imports, exports, and name bindings from a Ruby file.

    Returns:
        (importFiles, externalImports, exports, bindings)

    Bare ``require`` is linked only when a unique target exists under a recognized
    repository ``lib`` load-path root. Unresolved and external literals stay in
    ``externalImports``; dynamic expressions are omitted.

    C9 fix: ``attr_reader``, ``attr_writer``, ``attr_accessor``, and ``public`` method
    declarations are registered as exports.
    """
    repo_root = Path(repo_root).resolve()
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    exports: list[str] = []
    bindings: dict[str, str] = {}

    for node in _iter(root):
        if node.type != "call":
            continue
        method = node.child_by_field_name("method")
        if method is None:
            continue
        name = node_text(method, source)
        if name in {"require", "require_relative"}:
            if node.child_by_field_name("receiver") is not None:
                continue
            arguments = node.child_by_field_name("arguments")
            first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
            req = _static_require_string(first, source) if first is not None else None
            if not req:
                continue
            if name == "require_relative":
                resolved = _resolve_require_relative(req, file_path, repo_root)
            else:
                resolved = _resolve_bare_require(req, file_path, repo_root)
            if resolved is not None:
                internal.setdefault(resolved, None)
            else:
                external.setdefault(req, None)
        elif name in {"attr_reader", "attr_writer", "attr_accessor", "public"}:
            # C9: collect exported method/attribute names
            receiver = node.child_by_field_name("receiver")
            if receiver is None:
                arguments = node.child_by_field_name("arguments")
                if arguments is not None:
                    for arg_node in arguments.named_children:
                        sym = node_text(arg_node, source).lstrip(":").strip('"\'')
                        if sym and sym.isidentifier():
                            exports.append(sym)

    return list(internal), list(external), exports, bindings
