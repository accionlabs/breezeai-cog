"""In-house TypeScript code-first GraphQL parser: operation detection (local-decorator
signature), SDL-string vs method-name naming, @FieldResolver exclusion, auth guards,
capture-gating, selection (incl. not stealing the framework's own decorators.ts), and
schema validity."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.parsers.typescript_graphql.parser import GraphQLParser
from breezeai_cog.parsers.typescript_nbs_graphql.parser import NbsGraphQLParser
from breezeai_cog.schemas import FileRecord

# Grounded on the real bespoke framework: @Service class (no @Resolver), operation
# decorators imported from a project-relative decorators module, SDL-string + bare forms,
# a @FieldResolver (field resolver, not an op), and @RequireScopes auth.
RESOLVER = b'''import { Service } from '@nbs/typedi';
import { Arg, Ctx, Query, Mutation, Subscription, FieldResolver, RequireScopes, resolversFromService } from '../decorators';
import { GraphQLTypes } from '../decorators';

@Service()
@GraphQLTypes(`input ConsolidateInput { id: ID! }`)
export class ConsolidateResolver {
  @Mutation('consolidate(input: ConsolidateInput!): ID')
  async consolidate(@Ctx() ctx, @Arg('input') input): Promise<string> { return ''; }

  @Query('products(input: ProductsInput!): [Product!]!')
  @RequireScopes('read:products')
  public products(@Arg('input') input) { return []; }

  @Query()
  availableModels() { return []; }

  @Subscription('watchLocks(keys: [String!]!): Lockout')
  watchLocks() { return null; }

  @FieldResolver()
  computedField() { return 1; }
}
'''

# The framework's OWN decorators module: DEFINES the decorators (export const Query = …),
# never APPLIES them, and does not import them from a relative decorators module.
DECORATORS_MODULE = b'''import { Container } from '@nbs/typedi';
export const Query = (gqlType?: string) => (t, k, d) => {};
export const Mutation = (gqlType?: string) => (t, k, d) => {};
export function resolversFromService(x) { return x; }
'''


def _parse(src, name, *, capture=True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=".", capture_statements=capture)
    return NbsGraphQLParser().parse_file(ctx)


def _routes(rec: FileRecord):
    return [s for s in rec.statements if s.semanticType == "route"]


def test_operations_detected() -> None:
    rec = _parse(RESOLVER, "consolidate.resolver.ts")
    ops = {(s.method, s.endpoint): s for s in _routes(rec)}
    assert ("MUTATION", "consolidate") in ops       # SDL-string leading id
    assert ("QUERY", "products") in ops             # SDL-string leading id
    assert ("QUERY", "availableModels") in ops      # bare @Query() → method name
    assert ("SUBSCRIPTION", "watchLocks") in ops
    for s in ops.values():
        assert s.framework == "graphql"
    assert ops[("QUERY", "products")].routeKind == "query"
    assert rec.framework == "graphql"


def test_field_resolver_excluded() -> None:
    rec = _parse(RESOLVER, "consolidate.resolver.ts")
    assert "computedField" not in {s.endpoint for s in _routes(rec)}


def test_auth_guards() -> None:
    rec = _parse(RESOLVER, "consolidate.resolver.ts")
    products = next(s for s in _routes(rec) if s.endpoint == "products")
    assert products.authRequired is True and "RequireScopes" in products.guards
    # an op with no scope decorator has no guards
    assert next(s for s in _routes(rec) if s.endpoint == "consolidate").guards is None


def test_ops_parented_to_handlers() -> None:
    rec = _parse(RESOLVER, "consolidate.resolver.ts")
    fn_ids = {f.id for f in rec.functions}
    assert all(s.parentId in fn_ids for s in _routes(rec))


def test_routes_require_capture() -> None:
    rec = _parse(RESOLVER, "consolidate.resolver.ts", capture=False)
    assert _routes(rec) == [] and rec.framework is None


def test_claims_signature() -> None:
    p = NbsGraphQLParser()
    assert p.claims("r.ts", RESOLVER) is True
    # the framework's own decorators.ts must NOT be claimed (defines, never applies)
    assert p.claims("decorators.ts", DECORATORS_MODULE) is False
    # a @nestjs/graphql file (imports from @nestjs/, not a relative decorators module)
    nest = b"import { Resolver, Query } from '@nestjs/graphql';\n@Resolver()\nclass R { @Query() f(){} }"
    assert p.claims("r.ts", nest) is False


def test_selection() -> None:
    registry.clear()
    for parser in (TypeScriptParser(), GraphQLParser(), NbsGraphQLParser()):
        registry.register(parser)
    assert registry.select("consolidate.resolver.ts", RESOLVER).name == "typescript-nbs-graphql"
    assert registry.select("decorators.ts", DECORATORS_MODULE).name == "typescript"  # base fallback
    registry.clear()


def test_output_validates() -> None:
    rec = _parse(RESOLVER, "consolidate.resolver.ts")
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
