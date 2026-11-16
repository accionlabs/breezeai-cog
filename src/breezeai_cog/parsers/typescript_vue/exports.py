"""What a Vue single-file component exports — a registered fact, ``typescript.declarations.vue``.

A ``.vue`` file is owned by the Vue parser, so the TypeScript declarations fact never reads it;
this fact adds its one export, read back through the TypeScript ``module_exports`` with every
other ``typescript.declarations.<x>`` fact. It lets ``import Avatar from './Avatar.vue'``
resolve to the component.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..additive import FactSpec, register_fact
from ..typescript.declarations import FACT_PREFIX, FileExports

FACT = FACT_PREFIX + "vue"

# A Vue single-file component exports itself as `default`: its name is the `name` option
# (`defineOptions({ name: 'X' })`, or `name:` in `export default { … }`), else — Vue's own rule —
# the file name; its line is the `<script setup>` tag or the `export default`.
_VUE_NAME_RE = re.compile(rb"""(?:defineOptions\(\s*\{[^}]*?|export\s+default\s*(?:defineComponent\(\s*)?\{[^}]*?)\bname\s*:\s*['"]([\w$-]+)['"]""", re.S)
_VUE_DECL_RE = re.compile(rb"<script\b[^>]*\bsetup\b|export\s+default\b")


def _collect_vue(root: object, source: bytes, rel: str,
                 repo_root: Path | None) -> tuple[str, FileExports] | None:
    decl = _VUE_DECL_RE.search(source)
    if decl is None:
        return None
    m = _VUE_NAME_RE.search(source)
    name = m.group(1).decode() if m else Path(rel).stem
    line = source.count(b"\n", 0, decl.start()) + 1
    return rel, FileExports({"default": ("decl", name, line)})



def _resolve(values: list[tuple[str, FileExports]], index: Any) -> dict[str, FileExports] | None:
    return dict(values) or None


register_fact(FactSpec(
    name=FACT, language="typescript", gate=(b"<script",), sources=(".vue",),
    collect=_collect_vue, resolve=_resolve,
))
