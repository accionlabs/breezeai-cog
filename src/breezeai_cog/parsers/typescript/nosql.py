"""TypeScript NoSQL schema additive detector.

Runs AFTER the base TypeScript parser and adds NEW Class records for NoSQL/Graph
database schema definitions found in the file. It does NOT modify existing class
records. Conservative (honest-null): only emits what can be reliably determined
from the source text.

Supported patterns:

Pattern A: Mongoose ``new Schema({...})`` variable::

    const UserSchema = new Schema({ email: { type: String, required: true } })
    const UserSchema = new mongoose.Schema({ ... })

Pattern B: NestJS ``@Schema()`` decorated class + ``@Prop()`` fields::

    @Schema({ collection: 'users' })
    class User {
      @Prop({ required: true }) email: string;
    }

Pattern C: DynamoDB CDK ``new Table(...)``::

    const table = new dynamodb.Table(this, 'UsersTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
    })

Pattern D: Neo4j ``@Node(...)`` decorated class::

    @Node({ label: 'Persona' })
    class PersonaGraph { ... }
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import class_id, disambiguate, function_id
from ...schemas import Class, Function
from ..treesitter import line_span


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _child_of_type(node: Node, *types: str) -> Node | None:
    for c in node.named_children:
        if c.type in types:
            return c
    return None


def _children_of_type(node: Node, *types: str) -> list[Node]:
    return [c for c in node.named_children if c.type in types]


def _callee_text(new_expr: Node, source: bytes) -> str:
    """Return the callee text of a new_expression (e.g. 'Schema', 'mongoose.Schema', 'dynamodb.Table')."""
    ctor = new_expr.child_by_field_name("constructor")
    if ctor is None:
        return ""
    return _text(ctor, source)


def _callee_ends_in(new_expr: Node, source: bytes, suffix: str) -> bool:
    text = _callee_text(new_expr, source)
    return text == suffix or text.endswith("." + suffix)


def _first_object_arg(new_expr: Node) -> Node | None:
    """Return the first object literal argument of a new_expression."""
    args = new_expr.child_by_field_name("arguments")
    if args is None:
        return None
    for c in args.named_children:
        if c.type == "object":
            return c
    return None


def _object_args(new_expr: Node) -> list[Node]:
    """Return all object literal arguments of a new_expression, in order."""
    args = new_expr.child_by_field_name("arguments")
    if args is None:
        return []
    return [c for c in args.named_children if c.type == "object"]


def _unwrap_to_call(node: Node) -> Node | None:
    """Peel away await / parentheses / as-casts to find an inner call_expression."""
    if node.type == "call_expression":
        return node
    if node.type in ("await_expression", "as_expression", "parenthesized_expression"):
        for c in node.named_children:
            result = _unwrap_to_call(c)
            if result is not None:
                return result
    return None


def _string_value(node: Node, source: bytes) -> str | None:
    """Extract string content from a string node."""
    frag = next(
        (c for c in node.named_children if c.type in ("string_fragment", "template_chars")),
        None,
    )
    if frag is not None:
        return _text(frag, source)
    # single/double quoted with no child string_fragment — grab the raw bytes and strip quotes
    raw = _text(node, source)
    if len(raw) >= 2 and raw[0] in ('"', "'", "`") and raw[-1] in ('"', "'", "`"):
        return raw[1:-1]
    return None


def _get_pair_value_with_source(obj: Node, key_name: str, source: bytes) -> Node | None:
    """Find a pair with ``key_name`` in an object literal and return its value node."""
    for pair in obj.named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        if key is None:
            continue
        if key.type in ("property_identifier", "identifier"):
            ktext = _text(key, source)
        elif key.type == "string":
            ktext = _string_value(key, source) or ""
        else:
            ktext = _text(key, source)
        if ktext.strip("'\"` ") == key_name or ktext == key_name:
            return pair.child_by_field_name("value")
    return None


def _extract_type_from_value(value: Node, source: bytes) -> str:
    """Extract the data type from a schema field value node.

    - identifier → simple type (String, Number, Boolean, Date, ...)
    - object → config object; look for `type` pair inside
    - new_expression with Schema callee → nested embedded document
    - array → Array type
    """
    if value.type == "identifier":
        return _text(value, source)
    if value.type == "object":
        type_val = _get_pair_value_with_source(value, "type", source)
        if type_val is not None:
            if type_val.type == "identifier":
                return _text(type_val, source)
            if type_val.type == "array":
                return "Array"
        return "Object"
    if value.type == "new_expression":
        callee = _callee_text(value, source)
        if "Schema" in callee:
            return "Object"
        return "Unknown"
    if value.type == "array":
        return "Array"
    return "Unknown"


def _field_required(value: Node, source: bytes) -> bool | None:
    """Extract the required flag from a schema field config object."""
    if value.type != "object":
        return None
    req_val = _get_pair_value_with_source(value, "required", source)
    if req_val is None:
        return None
    text = _text(req_val, source)
    return text == "true"


def _field_unique(value: Node, source: bytes) -> bool | None:
    """Extract the unique flag from a schema field config object."""
    if value.type != "object":
        return None
    uniq_val = _get_pair_value_with_source(value, "unique", source)
    if uniq_val is None:
        return None
    text = _text(uniq_val, source)
    return text == "true"


def _extract_nested_columns(schema_obj: Node, source: bytes) -> list[dict]:
    """Extract field definitions from a Mongoose schema object literal."""
    columns: list[dict] = []
    for pair in schema_obj.named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        value = pair.child_by_field_name("value")
        if key is None or value is None:
            continue
        field_name = _text(key, source).strip("'\"` ")
        data_type = _extract_type_from_value(value, source)
        col: dict = {"name": field_name, "dataType": data_type}
        required = _field_required(value, source)
        if required is not None:
            col["required"] = required
        unique = _field_unique(value, source)
        if unique is not None:
            col["unique"] = unique
        # index flag
        if value.type == "object":
            index_val = _get_pair_value_with_source(value, "index", source)
            if index_val is not None and _text(index_val, source) == "true":
                col["index"] = True
        # default value
        if value.type == "object":
            default_val = _get_pair_value_with_source(value, "default", source)
            if default_val is not None:
                if default_val.type == "string":
                    col["default"] = _string_value(default_val, source)
                elif default_val.type in ("number", "true", "false", "identifier"):
                    col["default"] = _text(default_val, source)
        # enum values
        if value.type == "object":
            enum_val = _get_pair_value_with_source(value, "enum", source)
            if enum_val is not None and enum_val.type == "array":
                enum_items = []
                for item in enum_val.named_children:
                    if item.type == "string":
                        s = _string_value(item, source)
                        if s is not None:
                            enum_items.append(s)
                    elif item.type in ("number", "true", "false", "identifier"):
                        enum_items.append(_text(item, source))
                if enum_items:
                    col["enum"] = enum_items
        # itemType for array shorthand e.g. tags: [String]
        if data_type == "Array" and value.type == "array":
            item_nodes = [c for c in value.named_children if c.type == "identifier"]
            if item_nodes:
                col["itemType"] = _text(item_nodes[0], source)
        # ref: 'CollectionName' for cross-schema linking (e.g. ObjectId fields)
        if value.type == "object":
            ref_val = _get_pair_value_with_source(value, "ref", source)
            if ref_val is not None:
                if ref_val.type == "string":
                    ref_str = _string_value(ref_val, source)
                    if ref_str:
                        col["ref"] = ref_str
                elif ref_val.type == "identifier":
                    col["ref"] = _text(ref_val, source)
        # Nested embedded document: recurse
        if value.type == "new_expression" and "Schema" in _callee_text(value, source):
            nested_obj = _first_object_arg(value)
            if nested_obj is not None:
                col["nested"] = _extract_nested_columns(nested_obj, source)
        elif data_type == "Object" and value.type == "object":
            # Could be a plain nested sub-doc schema (not using new Schema)
            # Check if any values look like field configs
            nested = _extract_nested_columns(value, source)
            if nested:
                col["nested"] = nested
        columns.append(col)
    return columns


# ---------------------------------------------------------------------------
# Pattern A: Mongoose new Schema({...}) variable
# ---------------------------------------------------------------------------



def _collect_mongoose_schema_indexes(
    root: Node,
    source: bytes,
    var_to_class: dict[str, "Class"],
) -> None:
    """Second-pass: find schemaVar.index({fields}, {options}) and add to indexes."""
    def walk(node: Node) -> None:
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node is not None and fn_node.type == "member_expression":
                obj = fn_node.child_by_field_name("object")
                prop = fn_node.child_by_field_name("property")
                if obj is not None and prop is not None and _text(prop, source) == "index":
                    var_name = _text(obj, source)
                    if var_name in var_to_class:
                        args = node.child_by_field_name("arguments")
                        if args is not None:
                            obj_args = [c for c in args.named_children if c.type == "object"]
                            if obj_args:
                                fields_obj = obj_args[0]
                                columns = [
                                    _text(pair.child_by_field_name("key"), source).strip("'\"` ")
                                    for pair in fields_obj.named_children
                                    if pair.type == "pair" and pair.child_by_field_name("key") is not None
                                ]
                                if columns:
                                    index_entry: dict = {"columns": columns}
                                    if len(obj_args) >= 2:
                                        opts = obj_args[1]
                                        uniq = _get_pair_value_with_source(opts, "unique", source)
                                        if uniq is not None:
                                            index_entry["unique"] = _text(uniq, source) == "true"
                                    var_to_class[var_name].indexes.append(index_entry)
        for child in node.named_children:
            walk(child)
    walk(root)


def _collect_mongoose_model_names(
    root: Node,
    source: bytes,
    var_to_class: dict[str, "Class"],
) -> None:
    """Third-pass: find mongoose.model('Name', SchemaVar) and set collectionName."""
    def walk(node: Node) -> None:
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node is not None:
                fn_text = _text(fn_node, source)
                if fn_text == "model" or fn_text.endswith(".model"):
                    args = node.child_by_field_name("arguments")
                    if args is not None:
                        arg_nodes = list(args.named_children)
                        if len(arg_nodes) >= 2:
                            name_node = arg_nodes[0]
                            schema_node = arg_nodes[1]
                            if name_node.type == "string":
                                model_name = _string_value(name_node, source)
                            else:
                                model_name = None
                            schema_var = _text(schema_node, source) if schema_node.type == "identifier" else None
                            if model_name and schema_var and schema_var in var_to_class:
                                cls = var_to_class[schema_var]
                                if not getattr(cls, "collectionName", None):
                                    setattr(cls, "collectionName", model_name)
        for child in node.named_children:
            walk(child)
    walk(root)


def _detect_mongoose_schemas(
    root: Node,
    source: bytes,
    path: str,
    fid: str,
    seen_ids: set[str],
) -> list[Class]:
    """Detect ``const XxxSchema = new Schema({...})`` or ``new mongoose.Schema({...})``."""
    results: list[Class] = []

    def walk(node: Node) -> None:
        if node.type == "lexical_declaration":
            for vd in node.named_children:
                if vd.type != "variable_declarator":
                    continue
                value = vd.child_by_field_name("value")
                name_node = vd.child_by_field_name("name")
                if value is None or name_node is None:
                    continue
                if value.type != "new_expression":
                    continue
                if not _callee_ends_in(value, source, "Schema"):
                    continue
                schema_name = _text(name_node, source)
                objs = _object_args(value)
                schema_obj = objs[0] if objs else None
                schema_opts = objs[1] if len(objs) >= 2 else None
                columns: list[dict] = []
                if schema_obj is not None:
                    columns = _extract_nested_columns(schema_obj, source)
                # Extract schema-level validator from options (second arg)
                validation: dict | None = None
                if schema_opts is not None:
                    validator_val = _get_pair_value_with_source(schema_opts, "validator", source)
                    vl_val = _get_pair_value_with_source(schema_opts, "validationLevel", source)
                    if validator_val is not None or vl_val is not None:
                        validation = {}
                        if validator_val is not None:
                            validation["validator"] = _text(validator_val, source)
                        if vl_val is not None:
                            vl_text = (_string_value(vl_val, source) if vl_val.type == "string" else _text(vl_val, source))
                            if vl_text:
                                validation["validationLevel"] = vl_text
                start, end = line_span(vd)
                cid = disambiguate(class_id(path, schema_name), seen_ids)
                cls_kwargs: dict = dict(
                    id=cid,
                    parentId=fid,
                    path=path,
                    name=schema_name,
                    type="collection",
                    startLine=start,
                    endLine=end,
                    source="mongoose",
                    columns=columns,
                    indexes=[],
                )
                if validation:
                    cls_kwargs["metadata"] = {"validation": validation}
                cls_obj = Class(**cls_kwargs)
                results.append(cls_obj)
                var_to_class[schema_name] = cls_obj
        for child in node.named_children:
            walk(child)

    var_to_class: dict[str, Class] = {}
    walk(root)
    if var_to_class:
        _collect_mongoose_schema_indexes(root, source, var_to_class)
        _collect_mongoose_model_names(root, source, var_to_class)
    return results


# ---------------------------------------------------------------------------
# Pattern B: NestJS @Schema() decorated class + @Prop() fields
# ---------------------------------------------------------------------------


def _has_decorator_named(decorator_nodes: list[Node], name: str, source: bytes) -> bool:
    for dec in decorator_nodes:
        # decorator → call_expression → identifier / member_expression
        call = _child_of_type(dec, "call_expression")
        if call is None:
            # bare @Decorator (no parens)
            ident = _child_of_type(dec, "identifier")
            if ident is not None and _text(ident, source) == name:
                return True
            continue
        fn = call.child_by_field_name("function")
        if fn is None:
            continue
        fn_text = _text(fn, source)
        if fn_text == name or fn_text.endswith("." + name):
            return True
    return False


def _schema_collection_name(decorator_nodes: list[Node], source: bytes) -> str | None:
    """Extract the collection name from @Schema({ collection: 'users' })."""
    for dec in decorator_nodes:
        call = _child_of_type(dec, "call_expression")
        if call is None:
            continue
        fn = call.child_by_field_name("function")
        if fn is None:
            continue
        fn_text = _text(fn, source)
        if fn_text not in ("Schema",) and not fn_text.endswith(".Schema"):
            continue
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        obj = _child_of_type(args, "object")
        if obj is None:
            continue
        coll_val = _get_pair_value_with_source(obj, "collection", source)
        if coll_val is not None and coll_val.type == "string":
            return _string_value(coll_val, source)
    return None


def _prop_field_type(field_node: Node, source: bytes) -> str:
    """Extract the TypeScript type annotation from a public_field_definition."""
    annotation = next(
        (c for c in field_node.named_children if c.type == "type_annotation"),
        None,
    )
    if annotation is None:
        return "Unknown"
    # type_annotation → type node
    inner = next(
        (c for c in annotation.named_children if c.type not in (":",)),
        None,
    )
    if inner is None:
        return "Unknown"
    return _text(inner, source)


def _detect_nestjs_schemas(
    root: Node,
    source: bytes,
    path: str,
    fid: str,
    seen_ids: set[str],
) -> list[Class]:
    """Detect NestJS ``@Schema()`` decorated classes with ``@Prop()`` fields.

    In the tree-sitter AST, decorators on ``public_field_definition`` nodes are
    **children** of that node (not siblings). The class-level ``@Schema`` decorator
    is a named child of the ``export_statement`` node that wraps the class.
    """
    results: list[Class] = []

    def _field_decorators(field_node: Node) -> list[Node]:
        """Return decorator children of a public_field_definition node."""
        return [c for c in field_node.named_children if c.type == "decorator"]

    def handle_class(cnode: Node, decs: list[Node]) -> None:
        if not _has_decorator_named(decs, "Schema", source):
            return
        name_node = cnode.child_by_field_name("name")
        class_name = _text(name_node, source) if name_node is not None else ""
        collection_name = _schema_collection_name(decs, source)
        start, end = line_span(cnode)
        # Extract @Prop() fields: decorators are children of the field node
        columns: list[dict] = []
        body = cnode.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                if child.type not in ("public_field_definition", "field_definition"):
                    continue
                field_decs = _field_decorators(child)
                if not _has_decorator_named(field_decs, "Prop", source):
                    continue
                fname_node = child.child_by_field_name("name")
                fname = _text(fname_node, source) if fname_node is not None else ""
                ftype = _prop_field_type(child, source)
                col: dict = {"name": fname, "dataType": ftype}
                # Check for required in @Prop({required: true})
                for dec in field_decs:
                    call = _child_of_type(dec, "call_expression")
                    if call is None:
                        continue
                    fn = call.child_by_field_name("function")
                    if fn is None or _text(fn, source) not in ("Prop",):
                        continue
                    args = call.child_by_field_name("arguments")
                    if args is None:
                        continue
                    obj = _child_of_type(args, "object")
                    if obj is None:
                        continue
                    req = _get_pair_value_with_source(obj, "required", source)
                    if req is not None and _text(req, source) == "true":
                        col["required"] = True
                    uniq = _get_pair_value_with_source(obj, "unique", source)
                    if uniq is not None and _text(uniq, source) == "true":
                        col["unique"] = True
                    idx_v = _get_pair_value_with_source(obj, "index", source)
                    if idx_v is not None and _text(idx_v, source) == "true":
                        col["index"] = True
                    enum_v = _get_pair_value_with_source(obj, "enum", source)
                    if enum_v is not None and enum_v.type == "array":
                        items = []
                        for item in enum_v.named_children:
                            if item.type == "string":
                                s = _string_value(item, source)
                                if s is not None:
                                    items.append(s)
                            elif item.type in ("number", "true", "false", "identifier"):
                                items.append(_text(item, source))
                        if items:
                            col["enum"] = items
                    def_v = _get_pair_value_with_source(obj, "default", source)
                    if def_v is not None:
                        if def_v.type == "string":
                            col["default"] = _string_value(def_v, source)
                        elif def_v.type in ("number", "true", "false", "identifier"):
                            col["default"] = _text(def_v, source)
                    ref_v = _get_pair_value_with_source(obj, "ref", source)
                    if ref_v is not None:
                        if ref_v.type == "string":
                            ref_str = _string_value(ref_v, source)
                            if ref_str:
                                col["ref"] = ref_str
                        elif ref_v.type == "identifier":
                            col["ref"] = _text(ref_v, source)
                columns.append(col)
        schema_name = class_name + "Schema"
        cid = disambiguate(class_id(path, schema_name), seen_ids)
        kwargs: dict = dict(
            id=cid,
            parentId=fid,
            path=path,
            name=schema_name,
            type="collection",
            startLine=start,
            endLine=end,
            source="nestjs-mongoose",
            columns=columns,
            indexes=[],
        )
        if collection_name:
            kwargs["collectionName"] = collection_name
        results.append(Class(**kwargs))

    cur_pending: list[Node] = []
    for child in root.named_children:
        if child.type == "decorator":
            cur_pending.append(child)
        elif child.type == "comment":
            pass
        elif child.type in ("class_declaration", "abstract_class_declaration"):
            handle_class(child, cur_pending)
            cur_pending = []
        elif child.type == "export_statement":
            # Decorators preceding the class appear as named children of export_statement
            decs_in_export: list[Node] = []
            for c in child.named_children:
                if c.type == "decorator":
                    decs_in_export.append(c)
                elif c.type in ("class_declaration", "abstract_class_declaration"):
                    handle_class(c, cur_pending + decs_in_export)
                    decs_in_export = []
            cur_pending = []
        else:
            cur_pending = []

    return results


# ---------------------------------------------------------------------------
# Pattern C: DynamoDB CDK new dynamodb.Table(...)
# ---------------------------------------------------------------------------


def _dynamodb_key_info(
    table_args: Node, source: bytes
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract (tableName, partitionKey, sortKey, billingMode) from DynamoDB Table constructor arguments."""
    table_name: str | None = None
    partition_key: str | None = None
    sort_key: str | None = None

    # Arguments: (scope, id, props)
    # The props object is typically the 3rd argument
    objects = [c for c in table_args.named_children if c.type == "object"]
    if not objects:
        return None, None, None, None
    props = objects[0]

    # tableName
    tn_val = _get_pair_value_with_source(props, "tableName", source)
    if tn_val is not None and tn_val.type == "string":
        table_name = _string_value(tn_val, source)

    # partitionKey: { name: 'PK', type: ... }
    pk_val = _get_pair_value_with_source(props, "partitionKey", source)
    if pk_val is not None and pk_val.type == "object":
        pk_name = _get_pair_value_with_source(pk_val, "name", source)
        if pk_name is not None and pk_name.type == "string":
            partition_key = _string_value(pk_name, source)

    # sortKey: { name: 'SK', type: ... }
    sk_val = _get_pair_value_with_source(props, "sortKey", source)
    if sk_val is not None and sk_val.type == "object":
        sk_name = _get_pair_value_with_source(sk_val, "name", source)
        if sk_name is not None and sk_name.type == "string":
            sort_key = _string_value(sk_name, source)

    # billingMode: dynamodb.BillingMode.PAY_PER_REQUEST or just 'PAY_PER_REQUEST'
    billing_mode: str | None = None
    bm_val = _get_pair_value_with_source(props, "billingMode", source)
    if bm_val is not None:
        if bm_val.type == "member_expression":
            # e.g. dynamodb.BillingMode.PAY_PER_REQUEST — take the last segment
            bm_text = _text(bm_val, source)
            billing_mode = bm_text.rsplit(".", 1)[-1]
        elif bm_val.type == "string":
            billing_mode = _string_value(bm_val, source)
        elif bm_val.type == "identifier":
            billing_mode = _text(bm_val, source)

    return table_name, partition_key, sort_key, billing_mode


def _extract_gsi_info(gsi_obj: Node, source: bytes, index_type: str) -> dict:
    """Extract index metadata from an addGlobalSecondaryIndex / addLocalSecondaryIndex config object."""
    info: dict = {"type": index_type}
    index_name_val = _get_pair_value_with_source(gsi_obj, "indexName", source)
    if index_name_val is not None and index_name_val.type == "string":
        info["name"] = _string_value(index_name_val, source)
    pk_val = _get_pair_value_with_source(gsi_obj, "partitionKey", source)
    if pk_val is not None and pk_val.type == "object":
        pk_name = _get_pair_value_with_source(pk_val, "name", source)
        if pk_name is not None and pk_name.type == "string":
            info["partitionKey"] = _string_value(pk_name, source)
    sk_val = _get_pair_value_with_source(gsi_obj, "sortKey", source)
    if sk_val is not None and sk_val.type == "object":
        sk_name = _get_pair_value_with_source(sk_val, "name", source)
        if sk_name is not None and sk_name.type == "string":
            info["sortKey"] = _string_value(sk_name, source)
    return info


def _collect_dynamodb_indexes(
    root: Node,
    source: bytes,
    var_to_class: dict[str, Class],
) -> None:
    """Second-pass walk: find addGlobalSecondaryIndex / addLocalSecondaryIndex calls
    on known table variables and append index info to the table Class's indexes list."""
    _GSI_METHODS = {"addGlobalSecondaryIndex": "global_secondary_index", "addLocalSecondaryIndex": "local_secondary_index"}

    def walk(node: Node) -> None:
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node is not None and fn_node.type == "member_expression":
                obj = fn_node.child_by_field_name("object")
                prop = fn_node.child_by_field_name("property")
                if obj is not None and prop is not None:
                    var_name = _text(obj, source)
                    method = _text(prop, source)
                    index_type = _GSI_METHODS.get(method)
                    if index_type and var_name in var_to_class:
                        args = node.child_by_field_name("arguments")
                        if args is not None:
                            gsi_obj = _child_of_type(args, "object")
                            if gsi_obj is not None:
                                info = _extract_gsi_info(gsi_obj, source, index_type)
                                var_to_class[var_name].indexes.append(info)
        for child in node.named_children:
            walk(child)

    walk(root)


def _detect_dynamodb_tables(
    root: Node,
    source: bytes,
    path: str,
    fid: str,
    seen_ids: set[str],
) -> list[Class]:
    """Detect ``new dynamodb.Table(...)`` CDK constructs, including GSI/LSI indexes."""
    results: list[Class] = []
    var_to_class: dict[str, Class] = {}  # variable name → Class for GSI/LSI second pass

    def walk(node: Node) -> None:
        if node.type == "lexical_declaration":
            for vd in node.named_children:
                if vd.type != "variable_declarator":
                    continue
                value = vd.child_by_field_name("value")
                name_node = vd.child_by_field_name("name")
                if value is None or name_node is None:
                    continue
                if value.type != "new_expression":
                    continue
                callee = _callee_text(value, source)
                if not (callee.endswith(".Table") or callee == "Table"):
                    continue
                var_name = _text(name_node, source)
                args = value.child_by_field_name("arguments")
                table_name = None
                partition_key = None
                sort_key = None
                billing_mode = None
                if args is not None:
                    table_name, partition_key, sort_key, billing_mode = _dynamodb_key_info(args, source)
                start, end = line_span(vd)
                schema_name = table_name or var_name
                cid = disambiguate(class_id(path, schema_name), seen_ids)
                columns: list[dict] = []
                if partition_key:
                    columns.append({"name": partition_key, "dataType": "String", "keyType": "HASH"})
                if sort_key:
                    columns.append({"name": sort_key, "dataType": "String", "keyType": "RANGE"})
                tbl_kwargs: dict = dict(
                    id=cid,
                    parentId=fid,
                    path=path,
                    name=schema_name,
                    type="table",
                    startLine=start,
                    endLine=end,
                    source="dynamodb",
                    columns=columns,
                    indexes=[],
                )
                if billing_mode:
                    tbl_kwargs["billingMode"] = billing_mode
                tbl_class = Class(**tbl_kwargs)
                results.append(tbl_class)
                var_to_class[var_name] = tbl_class
        for child in node.named_children:
            walk(child)

    walk(root)
    if var_to_class:
        _collect_dynamodb_indexes(root, source, var_to_class)
    return results


# ---------------------------------------------------------------------------
# Pattern D: Neo4j @Node(...) / @Relationship(...) decorated class
# ---------------------------------------------------------------------------


def _extract_neo4j_columns(body: Node, source: bytes) -> list[dict]:
    """Extract property fields from a Neo4j entity class body into columns[].

    Collects all public_field_definition / field_definition nodes that are not
    method-like (no function body child).  Uses the TypeScript type annotation
    for dataType; falls back to "Unknown" when absent.  Works for both
    @Property()-annotated fields and plain class properties.
    """
    columns: list[dict] = []
    for child in body.named_children:
        if child.type not in ("public_field_definition", "field_definition"):
            continue
        # Skip method-like nodes (they have a function/statement_block child)
        if any(
            c.type in ("function", "arrow_function", "statement_block")
            for c in child.named_children
        ):
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        fname = _text(name_node, source)
        annotation = next(
            (c for c in child.named_children if c.type == "type_annotation"),
            None,
        )
        if annotation is not None:
            inner = next(
                (c for c in annotation.named_children if c.type != ":"),
                None,
            )
            data_type = _text(inner, source) if inner is not None else "Unknown"
        else:
            data_type = "Unknown"
        columns.append({"name": fname, "dataType": data_type})
    return columns



def _extract_neo4j_relationships(body: Node, source: bytes) -> list[dict]:
    """Extract relationship definitions from @RelatedTo/@Relationship decorator fields."""
    relationships: list[dict] = []
    _REL_DECORATORS = {"RelatedTo", "Relationship", "HasMany", "HasOne", "BelongsTo"}
    for child in body.named_children:
        if child.type not in ("public_field_definition", "field_definition"):
            continue
        decs = [c for c in child.named_children if c.type == "decorator"]
        rel_dec = None
        for dec in decs:
            call = _child_of_type(dec, "call_expression")
            if call is None:
                continue
            fn = call.child_by_field_name("function")
            if fn is None:
                continue
            fn_text = _text(fn, source)
            if any(fn_text == d or fn_text.endswith("." + d) for d in _REL_DECORATORS):
                rel_dec = call
                break
        if rel_dec is None:
            continue
        args = rel_dec.child_by_field_name("arguments")
        if args is None:
            continue
        obj = _child_of_type(args, "object")
        if obj is None:
            continue
        rel_entry: dict = {}
        # type / relationship name
        for key in ("relationship", "type"):
            val = _get_pair_value_with_source(obj, key, source)
            if val is not None and val.type == "string":
                rel_entry["type"] = _string_value(val, source)
                break
        # target class
        target_val = _get_pair_value_with_source(obj, "target", source)
        if target_val is not None:
            if target_val.type == "string":
                rel_entry["target"] = _string_value(target_val, source)
            elif target_val.type == "identifier":
                rel_entry["target"] = _text(target_val, source)
        # direction
        dir_val = _get_pair_value_with_source(obj, "direction", source)
        if dir_val is not None:
            dir_text = _text(dir_val, source)
            if "out" in dir_text.lower():
                rel_entry["direction"] = "outgoing"
            elif "in" in dir_text.lower():
                rel_entry["direction"] = "incoming"
            else:
                rel_entry["direction"] = dir_text.strip("'\"` ")
        if "type" in rel_entry or "target" in rel_entry:
            relationships.append(rel_entry)
    return relationships


def _extract_neo4j_constraints(decs: list[Node], dec_name: str, source: bytes) -> list[dict]:
    """Extract constraints from @Node({ constraints: [...] }) decorator options."""
    for dec in decs:
        call = _child_of_type(dec, "call_expression")
        if call is None:
            continue
        fn = call.child_by_field_name("function")
        if fn is None or _text(fn, source) != dec_name:
            continue
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        obj = _child_of_type(args, "object")
        if obj is None:
            continue
        constraints_val = _get_pair_value_with_source(obj, "constraints", source)
        if constraints_val is None or constraints_val.type != "array":
            return []
        constraints: list[dict] = []
        for item in constraints_val.named_children:
            if item.type != "object":
                continue
            entry: dict = {}
            type_v = _get_pair_value_with_source(item, "type", source)
            if type_v is not None and type_v.type == "string":
                entry["type"] = _string_value(type_v, source)
            cols_v = _get_pair_value_with_source(item, "columns", source)
            if cols_v is not None and cols_v.type == "array":
                cols = []
                for c in cols_v.named_children:
                    if c.type == "string":
                        s = _string_value(c, source)
                        if s:
                            cols.append(s)
                    elif c.type == "identifier":
                        cols.append(_text(c, source))
                if cols:
                    entry["columns"] = cols
            if entry:
                constraints.append(entry)
        return constraints
    return []


def _detect_neo4j_entities(
    root: Node,
    source: bytes,
    path: str,
    fid: str,
    seen_ids: set[str],
) -> list[Class]:
    """Detect Neo4j ``@Node(...)`` / ``@Relationship(...)`` decorated classes."""
    results: list[Class] = []

    def _neo4j_label(decs: list[Node], dec_name: str) -> str | None:
        for dec in decs:
            call = _child_of_type(dec, "call_expression")
            if call is None:
                continue
            fn = call.child_by_field_name("function")
            if fn is None or _text(fn, source) != dec_name:
                continue
            args = call.child_by_field_name("arguments")
            if args is None:
                continue
            obj = _child_of_type(args, "object")
            if obj is None:
                continue
            lbl = _get_pair_value_with_source(obj, "label", source)
            if lbl is not None and lbl.type == "string":
                return _string_value(lbl, source)
        return None

    def handle_class(cnode: Node, decs: list[Node]) -> None:
        is_node = _has_decorator_named(decs, "Node", source)
        is_rel = _has_decorator_named(decs, "Relationship", source)
        if not (is_node or is_rel):
            return
        class_type: str = "graph_node" if is_node else "graph_relationship"
        dec_name = "Node" if is_node else "Relationship"
        name_node = cnode.child_by_field_name("name")
        class_name = _text(name_node, source) if name_node is not None else ""
        label = _neo4j_label(decs, dec_name) or class_name
        start, end = line_span(cnode)
        cid = disambiguate(class_id(path, class_name), seen_ids)
        columns: list[dict] = []
        relationships: list[dict] = []
        body = cnode.child_by_field_name("body")
        if body is not None:
            columns = _extract_neo4j_columns(body, source)
            relationships = _extract_neo4j_relationships(body, source)
        constraints = _extract_neo4j_constraints(decs, dec_name, source)
        neo4j_kwargs: dict = dict(
            id=cid,
            parentId=fid,
            path=path,
            name=class_name,
            type=class_type,
            startLine=start,
            endLine=end,
            source="neo4j",
            label=label,
            columns=columns,
        )
        if constraints:
            neo4j_kwargs["constraints"] = constraints
        if relationships:
            neo4j_kwargs["relationships"] = relationships
        results.append(Class(**neo4j_kwargs))

    cur_pending: list[Node] = []
    for child in root.named_children:
        if child.type == "decorator":
            cur_pending.append(child)
        elif child.type == "comment":
            pass
        elif child.type in ("class_declaration", "abstract_class_declaration"):
            handle_class(child, cur_pending)
            cur_pending = []
        elif child.type == "export_statement":
            decs_in_export: list[Node] = []
            for c in child.named_children:
                if c.type == "decorator":
                    decs_in_export.append(c)
                elif c.type in ("class_declaration", "abstract_class_declaration"):
                    handle_class(c, cur_pending + decs_in_export)
                    decs_in_export = []
            cur_pending = []
        else:
            cur_pending = []

    return results


# ---------------------------------------------------------------------------
# MongoDB aggregation pipeline detection
# ---------------------------------------------------------------------------


def _detect_mongoose_aggregations(
    root: Node,
    source: bytes,
    path: str,
    fid: str,
    seen_ids: set[str],
) -> list[Function]:
    """Detect ``Model.aggregate([...])`` calls and emit Function records.

    Only detects calls assigned to a variable (lexical_declaration) so we can
    derive a name. Handles direct calls and await-wrapped calls.
    """
    results: list[Function] = []

    def walk(node: Node) -> None:
        if node.type == "lexical_declaration":
            for vd in node.named_children:
                if vd.type != "variable_declarator":
                    continue
                value = vd.child_by_field_name("value")
                name_node = vd.child_by_field_name("name")
                if value is None or name_node is None:
                    continue
                call = _unwrap_to_call(value)
                if call is None:
                    continue
                fn_node = call.child_by_field_name("function")
                if fn_node is None or fn_node.type != "member_expression":
                    continue
                prop = fn_node.child_by_field_name("property")
                if prop is None or _text(prop, source) != "aggregate":
                    continue
                args = call.child_by_field_name("arguments")
                if args is None:
                    continue
                if not any(c.type == "array" for c in args.named_children):
                    continue
                var_name = _text(name_node, source)
                start, end = line_span(vd)
                fid_fn = disambiguate(function_id(path, var_name, start), seen_ids)
                results.append(
                    Function(
                        id=fid_fn,
                        parentId=fid,
                        path=path,
                        name=var_name,
                        type="aggregation_pipeline",
                        startLine=start,
                        endLine=end,
                    )
                )
        for child in node.named_children:
            walk(child)

    if b"aggregate" in source:
        walk(root)
    return results


# ---------------------------------------------------------------------------
# Redis key schema detection
# ---------------------------------------------------------------------------

_REDIS_KEY_PATTERN = re.compile(r"\{\w+\}")  # {word} Redis-style placeholder (excludes ${var} template syntax)


def _is_redis_key_string(s: str) -> bool:
    """Heuristic: string looks like a Redis key pattern (has colons + {param})."""
    return ":" in s and bool(_REDIS_KEY_PATTERN.search(s))


def _extract_redis_patterns(obj_or_array: Node, source: bytes) -> list[dict]:
    """Extract Redis key pattern entries from an array of {pattern, dataType?, ttl?} objects
    or from an object whose values are such sub-objects."""
    patterns: list[dict] = []
    candidates: list[Node] = []

    if obj_or_array.type == "array":
        candidates = [c for c in obj_or_array.named_children if c.type == "object"]
    elif obj_or_array.type == "object":
        for pair in obj_or_array.named_children:
            if pair.type != "pair":
                continue
            val = pair.child_by_field_name("value")
            if val is not None and val.type == "object":
                candidates.append(val)

    for cand in candidates:
        pat_val = _get_pair_value_with_source(cand, "pattern", source)
        if pat_val is None or pat_val.type != "string":
            continue
        pat_str = _string_value(pat_val, source)
        if not pat_str or not _is_redis_key_string(pat_str):
            continue
        entry: dict = {"pattern": pat_str}
        for key in ("dataType", "type"):
            dt_val = _get_pair_value_with_source(cand, key, source)
            if dt_val is not None and dt_val.type == "string":
                entry["dataType"] = _string_value(dt_val, source) or "string"
                break
        ttl_val = _get_pair_value_with_source(cand, "ttl", source)
        if ttl_val is not None and ttl_val.type == "number":
            try:
                entry["ttl"] = int(_text(ttl_val, source))
            except ValueError:
                pass
        patterns.append(entry)
    return patterns


def _detect_redis_key_schemas(
    root: Node,
    source: bytes,
    path: str,
    fid: str,
    seen_ids: set[str],
) -> list[Class]:
    """Detect Redis key schema definitions — arrays/objects of {pattern, dataType?, ttl?}."""
    results: list[Class] = []

    def walk(node: Node) -> None:
        if node.type == "lexical_declaration":
            for vd in node.named_children:
                if vd.type != "variable_declarator":
                    continue
                value = vd.child_by_field_name("value")
                name_node = vd.child_by_field_name("name")
                if value is None or name_node is None:
                    continue
                if value.type not in ("array", "object"):
                    continue
                patterns = _extract_redis_patterns(value, source)
                if not patterns:
                    continue
                var_name = _text(name_node, source)
                start, end = line_span(vd)
                cid = disambiguate(class_id(path, var_name), seen_ids)
                results.append(
                    Class(
                        id=cid,
                        parentId=fid,
                        path=path,
                        name=var_name,
                        type="key_schema",
                        startLine=start,
                        endLine=end,
                        source="redis",
                        patterns=patterns,
                    )
                )
        for child in node.named_children:
            walk(child)

    if b"pattern" in source and b"{" in source and b"}" in source:
        walk(root)
    return results


# ---------------------------------------------------------------------------
# Elasticsearch TS mapping detection
# ---------------------------------------------------------------------------

_ES_FIELD_TYPES = frozenset(
    {"keyword", "text", "integer", "long", "float", "double", "boolean", "date", "object", "nested", "geo_point"}
)


def _extract_es_properties(props_obj: Node, source: bytes) -> list[dict]:
    """Extract Elasticsearch field definitions from a 'properties' object."""
    columns: list[dict] = []
    for pair in props_obj.named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        val = pair.child_by_field_name("value")
        if key is None or val is None or val.type != "object":
            continue
        field_name = _text(key, source).strip("'\"` ")
        type_val = _get_pair_value_with_source(val, "type", source)
        data_type = "Unknown"
        if type_val is not None and type_val.type == "string":
            data_type = _string_value(type_val, source) or "Unknown"
        col: dict = {"name": field_name, "dataType": data_type}
        analyzer_val = _get_pair_value_with_source(val, "analyzer", source)
        if analyzer_val is not None and analyzer_val.type == "string":
            col["analyzer"] = _string_value(analyzer_val, source)
        columns.append(col)
    return columns


def _detect_elasticsearch_mappings(
    root: Node,
    source: bytes,
    path: str,
    fid: str,
    seen_ids: set[str],
) -> list[Class]:
    """Detect ES index mapping objects: {mappings: {properties: {...}}} in TS/JS files."""
    results: list[Class] = []

    def _try_mapping_obj(obj: Node, var_name: str, start: int, end: int) -> None:
        mappings_val = _get_pair_value_with_source(obj, "mappings", source)
        if mappings_val is None or mappings_val.type != "object":
            return
        props_val = _get_pair_value_with_source(mappings_val, "properties", source)
        if props_val is None or props_val.type != "object":
            return
        columns = _extract_es_properties(props_val, source)
        if not columns:
            return
        # Derive index name: check for sibling 'index' key, else use var_name
        index_name_val = _get_pair_value_with_source(obj, "index", source)
        index_name = (
            (_string_value(index_name_val, source) if index_name_val and index_name_val.type == "string" else None)
            or var_name
        )
        # Extract settings if present
        settings_val = _get_pair_value_with_source(obj, "settings", source)
        settings: dict | None = None
        if settings_val is not None and settings_val.type == "object":
            settings = {}
            for key in ("numberOfShards", "numberOfReplicas", "number_of_shards", "number_of_replicas"):
                kv = _get_pair_value_with_source(settings_val, key, source)
                if kv is not None and kv.type == "number":
                    try:
                        settings[key] = int(_text(kv, source))
                    except ValueError:
                        pass
        cid = disambiguate(class_id(path, index_name), seen_ids)
        cls_kwargs: dict = dict(
            id=cid,
            parentId=fid,
            path=path,
            name=index_name,
            type="index_mapping",
            startLine=start,
            endLine=end,
            source="elasticsearch",
            columns=columns,
        )
        if settings:
            cls_kwargs["settings"] = settings
        results.append(Class(**cls_kwargs))

    def walk(node: Node) -> None:
        if node.type == "lexical_declaration":
            for vd in node.named_children:
                if vd.type != "variable_declarator":
                    continue
                value = vd.child_by_field_name("value")
                name_node = vd.child_by_field_name("name")
                if value is None or name_node is None:
                    continue
                if value.type != "object":
                    continue
                var_name = _text(name_node, source)
                start, end = line_span(vd)
                _try_mapping_obj(value, var_name, start, end)
        for child in node.named_children:
            walk(child)

    if b"mappings" in source and b"properties" in source:
        walk(root)
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_nosql_schemas(
    root: Node,
    source: bytes,
    path: str,
    fid: str,
    seen_ids: set[str],
) -> tuple[list[Class], list[Function]]:
    """Return (classes, functions) for NoSQL schema definitions found in the file.

    Runs AFTER the base TypeScript parser; adds NEW records (does not modify
    existing ones). Conservative: only emits what can be reliably determined.
    """
    classes: list[Class] = []
    functions: list[Function] = []

    has_mongoose = b"mongoose" in source or b"@Schema" in source
    has_schema_ctor = b"Schema" in source
    has_dynamodb = b"dynamodb" in source or b"DynamoDB" in source
    has_neo4j = b"@Node" in source or b"@Relationship" in source

    if has_schema_ctor and has_mongoose:
        classes.extend(_detect_mongoose_schemas(root, source, path, fid, seen_ids))

    if b"aggregate" in source:
        functions.extend(_detect_mongoose_aggregations(root, source, path, fid, seen_ids))

    if b"@Schema" in source and b"@Prop" in source:
        classes.extend(_detect_nestjs_schemas(root, source, path, fid, seen_ids))

    if has_dynamodb:
        classes.extend(_detect_dynamodb_tables(root, source, path, fid, seen_ids))

    if has_neo4j:
        classes.extend(_detect_neo4j_entities(root, source, path, fid, seen_ids))

    classes.extend(_detect_redis_key_schemas(root, source, path, fid, seen_ids))
    classes.extend(_detect_elasticsearch_mappings(root, source, path, fid, seen_ids))

    return classes, functions
