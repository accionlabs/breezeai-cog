"""AWS SDK v3 command-pattern clients (S3, Cognito, SSM), for TypeScript.

``client.send(new XxxCommand(...))`` — not a method chain: the receiver resolves to the client
type (DI field, class field, or free variable) and the first ``send()`` argument must be a
``new *Command(…)`` **imported from that same SDK package** (so a CQRS command or an unrelated
``*Command`` class cannot match). Endpoint = the Command class name verbatim, the same for all
three. One shared detector, one entry per SDK — see ``_detect_send_command_calls`` /
``_injected_client_fields`` / ``_package_imports``. The ``api_call`` shape is shared with the
other vendor-SDK detectors (``typescript_sdk_common.py``).
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import node_text
from ..typescript_sdk_common import (
    Sdk, annotation_type, callee_chain, client_identifiers, emit_outbound, this_member_call, walk_calls,
)


# --- AWS SDK v3 command-pattern clients ---------------------------------------
# ``client.send(new PutObjectCommand({...}))`` — a command-pattern call, not a method
# chain. One shared detector (:func:`_detect_send_command_calls`) serves all three; each
# SDK contributes only its package guard, client type and framework label.
_S3_BYTE_GUARD = b"@aws-sdk/client-s3"
_S3_CLIENT_TYPES = frozenset({"S3Client", "S3"})


def _command_variables(root: Node, source: bytes) -> dict[str, str]:
    """Map variable name → Command class name for ``const cmd = new PutObjectCommand(…)``."""
    out: dict[str, str] = {}

    def walk(n: Node) -> None:
        if n.type == "variable_declarator":
            name = n.child_by_field_name("name")
            v = n.child_by_field_name("value")
            if name is not None and name.type == "identifier" and v is not None:
                if v.type == "new_expression":
                    ctor = v.child_by_field_name("constructor")
                    if ctor is not None:
                        cn = node_text(ctor, source)
                        if cn.endswith("Command"):
                            out[node_text(name, source)] = cn
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _extract_command_name(
    call: Node, source: bytes, cmd_vars: dict[str, str] | None = None,
) -> str | None:
    """Extract the Command class name from ``client.send(new XxxCommand(…))`` or
    ``client.send(commandVar)`` where ``commandVar = new XxxCommand(…)``."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    first_arg = next((c for c in args.named_children if c.type != "comment"), None)
    if first_arg is None:
        return None
    if first_arg.type == "await_expression" and first_arg.named_children:
        first_arg = first_arg.named_children[0]
    if first_arg.type == "new_expression":
        ctor = first_arg.child_by_field_name("constructor")
        if ctor is None:
            return None
        name = node_text(ctor, source)
        return name if name.endswith("Command") else None
    # Variable reference: resolve via pre-scanned command variables
    if first_arg.type == "identifier" and cmd_vars:
        return cmd_vars.get(node_text(first_arg, source))
    return None


# --- AWS Cognito (command pattern) --------------------------------------------
_COGNITO_BYTE_GUARD = b"@aws-sdk/client-cognito-identity-provider"
_COGNITO_CLIENT_TYPES = frozenset({"CognitoIdentityProviderClient"})


def _injected_client_fields(root: Node, source: bytes, types: frozenset[str]) -> set[str]:
    """Field/variable names bound to a client of one of ``types`` — constructor DI
    (``constructor(private c: T) {}``), a class field (``private c: T;`` /
    ``private readonly c = new T({})`` / ``private c: T = new T({})``), a typed local
    (``const c: T = …``), or a local ``new T(…)`` initializer. Shared by every
    command-pattern SDK; ``types`` is the caller's set of client type names."""
    fields: set[str] = set()

    def walk(n: Node) -> None:
        if n.type == "required_parameter":
            if annotation_type(n, source) in types:
                name_node = n.child_by_field_name("pattern") or (
                    n.named_children[0] if n.named_children else None
                )
                if name_node is not None and name_node.type == "identifier":
                    fields.add(node_text(name_node, source))
        elif n.type in ("variable_declarator", "public_field_definition", "field_definition"):
            name = n.child_by_field_name("name")
            if name is not None and name.type in ("identifier", "property_identifier"):
                if annotation_type(n, source) in types:
                    fields.add(node_text(name, source))
                else:
                    v = n.child_by_field_name("value")
                    while v is not None and v.type in ("await_expression", "parenthesized_expression"):
                        v = v.named_children[0] if v.named_children else None
                    if v is not None and v.type == "new_expression":
                        ctor = v.child_by_field_name("constructor")
                        if ctor is not None and node_text(ctor, source) in types:
                            fields.add(node_text(name, source))
        for c in n.named_children:
            walk(c)

    walk(root)
    return fields


# --- AWS Parameter Store / SSM (command pattern) --------------------------------
_SSM_BYTE_GUARD = b"@aws-sdk/client-ssm"
_SSM_CLIENT_TYPES = frozenset({"SSMClient"})


def _package_imports(root: Node, source: bytes, package: str) -> set[str]:
    """Local names bound by ``import {…} from "<package>"`` or ``const {…} = require("<package>")``.

    An alias resolves to the *local* binding, since that is what appears at the call site
    (``GetParameterCommand as GPC`` → ``GPC``). The module specifier must match ``package``
    **exactly**, so a sibling package never contributes — ``@aws-sdk/client-ssm-incidents``
    does not count as ``@aws-sdk/client-ssm``, even though the byte guard is a substring test."""
    names: set[str] = set()

    def specifier(node: Node | None) -> str | None:
        if node is None:
            return None
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else None

    def add_esm(n: Node) -> None:
        for c in n.named_children:
            if c.type == "import_specifier":
                local = c.child_by_field_name("alias") or c.child_by_field_name("name")
                if local is not None:
                    names.add(node_text(local, source))
            else:
                add_esm(c)

    def add_cjs(pattern: Node) -> None:
        for el in pattern.named_children:
            if el.type == "shorthand_property_identifier_pattern":
                names.add(node_text(el, source))
            elif el.type == "pair_pattern":  # { SignUpCommand: SUC }
                local = el.child_by_field_name("value")
                if local is not None and local.type == "identifier":
                    names.add(node_text(local, source))

    def walk(n: Node) -> None:
        if n.type == "import_statement":
            if specifier(n.child_by_field_name("source")) == package:
                add_esm(n)
        elif n.type == "variable_declarator":
            name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if (
                name is not None
                and name.type == "object_pattern"
                and value is not None
                and value.type == "call_expression"
            ):
                fn = value.child_by_field_name("function")
                args = value.child_by_field_name("arguments")
                if fn is not None and node_text(fn, source) == "require" and args is not None:
                    arg = next((c for c in args.named_children if c.type == "string"), None)
                    if specifier(arg) == package:
                        add_cjs(name)
        for c in n.named_children:
            walk(c)

    walk(root)
    return names


def _detect_send_command_calls(
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
    out: list[Emit],
    *,
    byte_guard: bytes,
    client_types: frozenset[str],
    framework: str,
) -> bool:
    """Detect ``client.send(new XxxCommand(…))`` calls for one AWS SDK v3 client (S3, Cognito,
    SSM, …). Handles DI-field, class-field, and free-variable client forms (via
    :func:`_injected_client_fields` / :func:`client_identifiers`). The endpoint is the
    Command class name verbatim — the token the code actually contains, not a derived
    operation name.

    The command class must be imported from **this** SDK package (:func:`_package_imports`).
    Without that, ``*Command`` is only a naming convention: a CQRS/mediator command, or an
    unrelated object that happens to share a variable name with a client elsewhere in the
    file, would be emitted with a fabricated endpoint. A command reached through a barrel
    re-export is therefore not detected — its origin is in another file (absent beats wrong)."""
    if byte_guard not in source:
        return False
    sdk_commands = _package_imports(root, source, byte_guard.decode())
    if not sdk_commands:
        return False
    di_fields = _injected_client_fields(root, source, client_types)
    sdk = Sdk(
        import_marker=byte_guard,
        framework=framework,
        client_types=client_types,
        operations=frozenset(),
    )
    free_clients = client_identifiers(root, source, sdk)
    if not di_fields and not free_clients:
        return False
    cmd_vars = _command_variables(root, source)
    emitted = False
    for call in walk_calls(root):
        command_name: str | None = None
        # Pattern A: this.field.send(new Cmd(…)) or this.field.send(cmdVar) — DI/class field
        if di_fields:
            result = this_member_call(call, source)
            if result is not None:
                field, method = result
                if field in di_fields and method == "send":
                    command_name = _extract_command_name(call, source, cmd_vars)
        # Pattern B: variable.send(new Cmd(…)) or variable.send(cmdVar) — free variable
        if command_name is None and free_clients:
            chain = callee_chain(call, source)
            if chain is not None:
                _callee_text, receiver, tail = chain
                if receiver in free_clients and tail == "send":
                    command_name = _extract_command_name(call, source, cmd_vars)
        if command_name is not None and command_name in sdk_commands:
            emit_outbound(call, command_name, framework, out)
            emitted = True
    return emitted


_CLIENTS = (
    (_S3_BYTE_GUARD, _S3_CLIENT_TYPES, "aws-s3"),
    (_COGNITO_BYTE_GUARD, _COGNITO_CLIENT_TYPES, "aws-cognito"),
    (_SSM_BYTE_GUARD, _SSM_CLIENT_TYPES, "aws-ssm"),
)


def _detect(dc: DetectContext) -> Detection | None:
    out: list[Emit] = []
    label: str | None = None
    for byte_guard, client_types, framework in _CLIENTS:
        if _detect_send_command_calls(
            dc.root, dc.source, dc.path, dc.record, out,
            byte_guard=byte_guard, client_types=client_types, framework=framework,
        ):
            label = label or framework
    return Detection(out, label=label) if out else None


register_detector(Detector(
    name="aws-sdk-calls", gate=Gate("typescript", order=34, guard=b"@aws-sdk/client-"), detect=_detect,
    frameworks=("aws-s3", "aws-cognito", "aws-ssm"),
))
