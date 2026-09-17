"""HotChocolate vocabulary — attribute and type names the framework itself reads.

Every entry here is **framework-enforced**: an attribute HotChocolate binds, or a type its
resolver compiler injects. Team naming conventions (a class called ``…Queries``, a parameter
type called ``…Input``) are deliberately absent — renaming those changes nothing at runtime,
so treating them as signals would fabricate routes. See the decision notes in
``.todo/hotchocolate-parser-decisions.md``.
"""

from __future__ import annotations

#: Root-declaring attributes → operation kind. The only in-file *enforced* statement that a
#: class is a schema root (the other is ``AddQueryType<T>()`` at the registration site).
ROOT_ATTRS = {
    "QueryType": "query",
    "MutationType": "mutation",
    "SubscriptionType": "subscription",
}

#: Adds this class's methods as fields of another type — the root (real operations) or a data
#: type (field resolvers). The argument decides which; see ``extend_target`` in routes.py.
EXTEND_ATTR = "ExtendObjectType"

#: Marks the parameter that carries the object being resolved — only meaningful on a data type.
PARENT_ATTR = "Parent"

#: Root operation types by their **schema** name. ``[ExtendObjectType("Query")]`` and
#: ``[ExtendObjectType(OperationTypeNames.Query)]`` name the schema type directly, and the roots
#: are called Query/Mutation/Subscription there by default — enforced, unlike a CLR class name.
ROOT_SCHEMA_NAMES = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}

#: Fluent style: a schema type declared by subclassing ``ObjectType<T>`` and building fields
#: inside ``Configure(IObjectTypeDescriptor<T>)``. Both name the described type T.
DESCRIPTOR_BASES = ("ObjectType", "ObjectTypeExtension")
DESCRIPTOR_PARAM_TYPES = ("IObjectTypeDescriptor", "IObjectTypeExtensionDescriptor")
CONFIGURE_METHOD = "Configure"

#: Builder calls inside ``Configure``: ``Field`` declares one, ``Ignore`` removes it from the
#: schema, ``Name`` renames it.
FIELD_CALL = "Field"
IGNORE_CALL = "Ignore"
NAME_CALL = "Name"

#: Declarative subscription: the method resolves an event pushed to a topic.
SUBSCRIBE_ATTR = "Subscribe"

#: Names the pub/sub topic a subscription consumes. Bare (no argument) means "the field name",
#: which is HotChocolate's own default — enforced, so it resolves rather than guesses.
TOPIC_ATTR = "Topic"

#: Removes a member from the schema entirely.
IGNORE_ATTR = "GraphQLIgnore"

#: Renames a field / marks a parameter as a GraphQL argument.
NAME_ATTR = "GraphQLName"

#: Authorization, class- or method-level → guards + authRequired.
AUTHORIZE_ATTR = "Authorize"

#: Parameter attributes that mark an injected dependency rather than a client argument.
INFRA_PARAM_ATTRS = frozenset({
    "Parent", "Service", "ScopedService",
    "EventMessage",
    "LocalState", "ScopedState", "GlobalState",
})

#: Parameter *types* the resolver compiler binds itself — never client arguments.
INFRA_PARAM_TYPES = frozenset({
    "CancellationToken", "IResolverContext", "ClaimsPrincipal",
    "ITopicEventReceiver", "ITopicEventSender",
})

#: Batching-loader types, matched on a parameter's **declared generic base** —
#: ``IDataLoader<int, Author>`` / ``BatchDataLoader<int, Author>``. A named subclass
#: (``AuthorDataLoader loader``) is *not* matched on its ``…DataLoader`` suffix: that is a team
#: convention, and the enforced fact lives in the subclass's base type, in another file.
#: Resolving those needs the repo heritage index — a known narrow case for now.
DATALOADER_TYPES = ("IDataLoader", "BatchDataLoader", "GroupedDataLoader", "CacheDataLoader")

#: Parameter attributes that positively identify a GraphQL *argument*. Since ``[Service]`` is
#: optional from v13 on, a bare ``CatalogDb db`` parameter is indistinguishable from a client
#: argument without knowing the DI registrations — so requestDTO is set only from these, and
#: left null otherwise (a documented gap, never a guessed type).
ARG_MARKER_ATTRS = frozenset({"GraphQLName", "GraphQLType", "DefaultValue"})

#: Request-pipeline attributes that reshape the wire type. Recorded on the statement's
#: ``decorators`` so the rewrite is visible without inventing a generated type name.
MIDDLEWARE_ATTRS = frozenset({
    "UsePaging", "UseOffsetPaging", "UseCursorPaging",
    "UseFiltering", "UseSorting", "UseProjection",
    "UseMutationConvention",
})

#: Cheap byte guard for ``claims``. Deliberately disjoint from ``csharp-graphql``'s
#: ``ObjectGraphType`` so the two parsers never contend for one file, and deliberately
#: excludes ``AddGraphQLServer`` / ``MapGraphQL`` so ``Program.cs`` stays with
#: ``csharp-aspnet`` and keeps its REST routes.
MARKERS: tuple[bytes, ...] = (
    b"HotChocolate",
    b"[QueryType]", b"[MutationType]", b"[SubscriptionType]",
    b"[ExtendObjectType",
    b"[Subscribe]",
    b"IObjectTypeDescriptor",
)
