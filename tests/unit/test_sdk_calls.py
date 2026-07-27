"""Vendor-SDK outbound-call detection (detection/sdk_calls.py, additive on TypeScriptParser):
import-keyed client resolution, api_call reuse, honest-null, and false-positive guards."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.index_common import ClassHeritage
from breezeai_cog.parsers.typescript.imports import TsAliasIndex
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.schemas import FileRecord

# HubSpot: client bound via a factory whose return type is the imported `Client` type,
# then a deep chain call — the exact shape in hubspot-tools.
HUBSPOT_SRC = b"""import { Client } from '@hubspot/api-client';

let _client: Client | null = null;
function getClient(): Client {
  if (!_client) _client = new Client({ accessToken: 'x' });
  return _client;
}

export async function search(objectType: string) {
  const client = getClient();
  const response = await client.crm.objects.searchApi.doSearch(objectType, { limit: 100 });
  return response;
}
"""

# Chargebee: client passed in as a typed parameter, `client.<resource>.list(...)`.
CHARGEBEE_SRC = b"""import Chargebee, { Customer } from 'chargebee';

export async function listCustomers(client: Chargebee) {
  const result = await client.customer.list({ limit: 100 });
  return result.list;
}
"""


def _parse(tmp_path, src: bytes, rel: str, *, capture=True, resolution_index=None) -> FileRecord:
    p = tmp_path / "f.ts"
    p.write_bytes(src)
    ctx = ParseContext(
        path=rel,
        abs_path=p,
        source=src,
        repo_root=tmp_path,
        capture_statements=capture,
        resolution_index=resolution_index,
    )
    return TypeScriptParser().parse_file(ctx)


def _heritage_index(**extends: str) -> TsAliasIndex:
    """A minimal repo-wide index carrying only class heritage (name → base), as the
    ``build_index`` pre-pass would produce for entities defined in other files."""
    return TsAliasIndex(
        base_dir="",
        paths={},
        class_heritage={
            n: ClassHeritage(extends=base, decorators=[]) for n, base in extends.items()
        },
    )


def _api_calls(rec: FileRecord):
    return [s for s in rec.statements if s.semanticType == "api_call"]


def test_hubspot_sdk_call_detected(tmp_path) -> None:
    rec = _parse(tmp_path, HUBSPOT_SRC, "src/shared/hubspot/client.ts")
    calls = _api_calls(rec)
    assert len(calls) == 1
    c = calls[0]
    assert c.framework == "hubspot"
    assert c.endpoint == "client.crm.objects.searchApi.doSearch"
    assert c.method is None  # honest-null: no HTTP verb at the SDK call site
    assert c.parentId in {f.id for f in rec.functions}  # parented to the calling function
    assert rec.framework == "hubspot"


def test_chargebee_sdk_call_via_typed_param(tmp_path) -> None:
    rec = _parse(tmp_path, CHARGEBEE_SRC, "src/shared/chargebee/client.ts")
    calls = _api_calls(rec)
    assert len(calls) == 1
    assert calls[0].framework == "chargebee"
    assert calls[0].endpoint == "client.customer.list"
    assert calls[0].method is None


def test_requires_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, HUBSPOT_SRC, "src/shared/hubspot/client.ts", capture=False)
    assert _api_calls(rec) == []


def test_no_sdk_import_is_inert(tmp_path) -> None:
    # Same call shape, but no SDK import → must NOT be tagged (byte guard).
    src = b"export async function f(client: any) { await client.customer.list({}); }"
    rec = _parse(tmp_path, src, "src/x.ts")
    assert _api_calls(rec) == []


def test_same_method_on_non_client_not_tagged(tmp_path) -> None:
    # Chargebee imported, but `.list()` is called on an object that is NOT a Chargebee client
    # (an array-ish local). Receiver must resolve to a client type → no false positive.
    src = b"""import Chargebee from 'chargebee';
export function f(repo: MyRepo) {
  const items = repo.customer.list({});
  return items;
}
"""
    rec = _parse(tmp_path, src, "src/x.ts")
    assert _api_calls(rec) == []


def test_unknown_operation_on_client_not_tagged(tmp_path) -> None:
    # A real Chargebee client, but calling a method that is not a known SDK operation.
    src = b"""import Chargebee from 'chargebee';
export async function f(client: Chargebee) { return client.customer.frobnicate({}); }
"""
    rec = _parse(tmp_path, src, "src/x.ts")
    assert _api_calls(rec) == []


# ts-force (Salesforce): a generated entity `extends RestObject` with a static retrieve()
# that funnels through RestObject.query<T>(T, qry) — the real hubspot-tools shape.
TSFORCE_ENTITY_SRC = b"""import { RestObject, QueryOpts, buildQuery } from 'ts-force';

export class Account extends RestObject {
  public static async retrieve(qryParam, opts?: QueryOpts): Promise<Account[]> {
    const qry = buildQuery(Account, qryParam);
    return await RestObject.query<Account>(Account, qry, opts);
  }
}
"""

# ts-force generic helper: client.query<T>(qry) where T is an unbound type parameter —
# the SObject is NOT knowable here (resolved at each caller). Endpoint must be honest-null.
TSFORCE_GENERIC_SRC = b"""import { Rest, RestObject } from 'ts-force';

export async function* queryAll<T extends RestObject>(type, qry: string) {
  const client = new Rest();
  const response = await client.query<T>(qry);
  yield* response.records;
}
"""


def test_tsforce_entity_query_detected(tmp_path) -> None:
    rec = _parse(tmp_path, TSFORCE_ENTITY_SRC, "src/shared/salesforce/entities/Account.ts")
    calls = _api_calls(rec)
    assert len(calls) == 1
    c = calls[0]
    assert c.framework == "salesforce"
    assert c.endpoint == "Account"  # SObject from the generic <Account>
    assert c.method is None
    assert c.dataAccessHint is None  # reclassified away from db_method_call/orm


def test_tsforce_generic_query_is_honest_null(tmp_path) -> None:
    rec = _parse(tmp_path, TSFORCE_GENERIC_SRC, "src/shared/salesforce/connection.ts")
    calls = _api_calls(rec)
    assert len(calls) == 1
    c = calls[0]
    assert c.framework == "salesforce"
    assert c.endpoint is None  # unbound <T> → SObject unknown → null, never the literal "T"


# ts-force cross-file read: the call site imports the entity from another file, so no
# `extends RestObject` class is present locally — the SObject is knowable only from the
# repo-wide heritage index. This is the real application shape (connection/provider files
# call `Entity.retrieve(...)`; the entity class lives under entities/).
TSFORCE_CROSSFILE_READ_SRC = b"""import { ProductFeature } from './entities/ProductFeature';

export async function loadFeatures(templateId: string) {
  return await ProductFeature.retrieve((f) => ({
    select: [f.select('productTemplateId')],
    where: [{ field: f.select('productTemplateId'), op: '=', val: templateId }],
  }));
}
"""


def test_tsforce_crossfile_read_resolves_from_repo_index(tmp_path) -> None:
    # ProductFeature is defined elsewhere (extends RestObject) — supplied via the repo index.
    idx = _heritage_index(ProductFeature="RestObject")
    rec = _parse(
        tmp_path, TSFORCE_CROSSFILE_READ_SRC, "src/create-entitlements.ts", resolution_index=idx
    )
    calls = [c for c in _api_calls(rec) if c.framework == "salesforce"]
    assert len(calls) == 1
    c = calls[0]
    assert c.endpoint == "ProductFeature"  # resolved from the repo-wide heritage index
    assert c.method is None


def test_tsforce_crossfile_read_without_index_is_absent(tmp_path) -> None:
    # No repo index → the entity is unknown here → absent beats wrong: nothing emitted for
    # `ProductFeature.retrieve` (receiver is not a locally-known entity, no <T>, not RestObject).
    rec = _parse(tmp_path, TSFORCE_CROSSFILE_READ_SRC, "src/create-entitlements.ts")
    assert [c for c in _api_calls(rec) if c.framework == "salesforce"] == []


def test_tsforce_ambiguous_repo_entity_not_resolved(tmp_path) -> None:
    # A simple name the index collapsed to None (conflicting bases across files) is NOT a
    # confirmed entity → not resolved (honest).
    idx = TsAliasIndex(base_dir="", paths={}, class_heritage={"ProductFeature": None})
    rec = _parse(
        tmp_path, TSFORCE_CROSSFILE_READ_SRC, "src/create-entitlements.ts", resolution_index=idx
    )
    assert [c for c in _api_calls(rec) if c.framework == "salesforce"] == []


# ts-force writes: instance methods on an SObject. The receiver variable's type is resolved
# to a known entity (`new Entity()` / `: Entity`); a non-entity receiver (a JS Set) must not
# be tagged. Entity defined elsewhere → supplied via the repo index.
TSFORCE_WRITE_SRC = b"""import { ProductFeature } from './entities/ProductFeature';

export async function sync(current: Set<string>) {
  const newFeature = new ProductFeature({ productTemplateId: 't' });
  await newFeature.insert();          // resolvable via `new ProductFeature()`
  current.delete('stale');            // a JS Set - must NOT be tagged salesforce
}
"""


def test_tsforce_write_resolves_receiver_type(tmp_path) -> None:
    idx = _heritage_index(ProductFeature="RestObject")
    rec = _parse(tmp_path, TSFORCE_WRITE_SRC, "src/create-entitlements.ts", resolution_index=idx)
    calls = [c for c in _api_calls(rec) if c.framework == "salesforce"]
    assert len(calls) == 1  # only the insert; the Set.delete is not tagged
    c = calls[0]
    assert c.endpoint == "ProductFeature"  # resolved from `new ProductFeature()`
    assert "insert" in c.text
    assert c.method is None


def test_tsforce_write_via_typed_param(tmp_path) -> None:
    idx = _heritage_index(Account="RestObject")
    src = b"""import { Account } from './entities/Account';
export async function save(acct: Account) { await acct.update(); }
"""
    rec = _parse(tmp_path, src, "src/provider.ts", resolution_index=idx)
    calls = [c for c in _api_calls(rec) if c.framework == "salesforce"]
    assert len(calls) == 1
    assert calls[0].endpoint == "Account"


def test_tsforce_write_unresolvable_receiver_is_absent(tmp_path) -> None:
    # A resolvable write (`new Account()`) sits beside one whose receiver is returned from a
    # helper (type not locally resolvable). The guard passes (via `new Account`), so this
    # genuinely exercises the skip: only the resolvable write is tagged, the other is absent
    # (absent beats wrong; we never guess the instance type).
    idx = _heritage_index(Account="RestObject")
    src = b"""import { Account } from './entities/Account';
import { fetchOne } from './helpers';
export async function save() {
  const known = new Account();
  await known.update();
  const a = await fetchOne();
  await a.update();
}
"""
    rec = _parse(tmp_path, src, "src/provider.ts", resolution_index=idx)
    calls = [c for c in _api_calls(rec) if c.framework == "salesforce"]
    assert len(calls) == 1
    assert calls[0].endpoint == "Account"


def test_tsforce_composite_bulk_write_detected(tmp_path) -> None:
    # `new CompositeCollection().update(accounts)` — bulk DML; the SObject is the element type
    # of the array argument (`accounts: Account[]`), not the receiver.
    idx = _heritage_index(Account="RestObject")
    src = b"""import { CompositeCollection } from 'ts-force';
export async function save(accounts: Account[]) {
  const bulk = new CompositeCollection();
  return await bulk.update(accounts);
}
"""
    rec = _parse(tmp_path, src, "src/provider.ts", resolution_index=idx)
    calls = [c for c in _api_calls(rec) if c.framework == "salesforce"]
    assert len(calls) == 1
    assert calls[0].endpoint == "Account"  # resolved from the array argument's element type
    assert calls[0].method is None


def test_tsforce_bulk_write_inline_arg_is_absent(tmp_path) -> None:
    # Inline array literal argument → element type is not a single declared symbol → skip
    # (absent beats wrong; we do not resolve per-element types).
    idx = _heritage_index(Account="RestObject")
    src = b"""import { CompositeCollection } from 'ts-force';
import { Account } from './entities/Account';
export async function save(a: Account, b: Account) {
  const bulk = new CompositeCollection();
  return await bulk.update([a, b]);
}
"""
    rec = _parse(tmp_path, src, "src/provider.ts", resolution_index=idx)
    assert [c for c in _api_calls(rec) if c.framework == "salesforce"] == []


def test_tsforce_query_more_pagination_detected(tmp_path) -> None:
    # queryMore<T>(response) is the pagination continuation of a read — another outbound
    # Salesforce call. With a concrete <Account>, the endpoint resolves like query<Account>.
    idx = _heritage_index(Account="RestObject")
    src = b"""import { Rest } from 'ts-force';
export async function* page(client: Rest, first) {
  let response = await client.queryMore<Account>(first);
  yield* response.records;
}
"""
    rec = _parse(tmp_path, src, "src/connection.ts", resolution_index=idx)
    calls = [c for c in _api_calls(rec) if c.framework == "salesforce"]
    assert len(calls) == 1
    assert calls[0].endpoint == "Account"
    assert calls[0].method is None


def test_tsforce_requires_import(tmp_path) -> None:
    # Same shape but no ts-force import → not tagged (byte guard).
    src = b"""class Account extends RestObject {
  static async retrieve() { return await RestObject.query<Account>(Account, 'x'); }
}
"""
    rec = _parse(tmp_path, src, "src/x.ts")
    assert [s for s in _api_calls(rec) if s.framework == "salesforce"] == []


def test_output_validates(tmp_path) -> None:
    for src, rel in (
        (HUBSPOT_SRC, "src/hs.ts"),
        (CHARGEBEE_SRC, "src/cb.ts"),
        (TSFORCE_ENTITY_SRC, "src/entities/Account.ts"),
        (TSFORCE_GENERIC_SRC, "src/connection.ts"),
    ):
        rec = _parse(tmp_path, src, rel)
        errors = list(
            Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
                json.loads(to_line(rec))
            )
        )
        assert not errors, errors
