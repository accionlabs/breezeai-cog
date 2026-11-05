"""What Django REST framework's own views, viewsets, routers and permissions do, as tables —
read from DRF's documentation, never from the repository.

* :data:`API_VIEWS` — the generic API views: each verb and the action method it calls
  (``ListCreateAPIView``: ``GET`` → ``list``, ``POST`` → ``create``).
* :data:`VIEWSET_ACTIONS` — the actions each viewset and mixin provides, and
  :data:`ACTION_ROUTES` how a router addresses them.
* :data:`CERTAIN_PERMISSIONS` / :data:`CERTAIN_UNSAFE` — the permission classes that reject an
  anonymous caller: always, or on the methods that change data.
"""

from __future__ import annotations

FRAMEWORK = "drf"
MODULE = "rest_framework"

_LIST, _CREATE, _RETRIEVE = (("GET", "list"),), (("POST", "create"),), (("GET", "retrieve"),)
_UPDATE, _DESTROY = (("PUT", "update"), ("PATCH", "partial_update")), (("DELETE", "destroy"),)

#: ``rest_framework.generics`` views → (verb, action) pairs.
API_VIEWS: dict[str, tuple[tuple[str, str], ...]] = {
    "ListAPIView": _LIST,
    "CreateAPIView": _CREATE,
    "RetrieveAPIView": _RETRIEVE,
    "DestroyAPIView": _DESTROY,
    "UpdateAPIView": _UPDATE,
    "ListCreateAPIView": _LIST + _CREATE,
    "RetrieveUpdateAPIView": _RETRIEVE + _UPDATE,
    "RetrieveDestroyAPIView": _RETRIEVE + _DESTROY,
    "RetrieveUpdateDestroyAPIView": _RETRIEVE + _UPDATE + _DESTROY,
}
#: DRF bases that handle no verb themselves: a view built on them handles what it defines.
API_BASES = frozenset({"APIView", "GenericAPIView", "ViewSet", "GenericViewSet", "ViewSetMixin"})

#: Viewsets and mixins (``rest_framework.viewsets`` / ``.mixins``) → the actions they provide.
VIEWSET_ACTIONS: dict[str, tuple[str, ...]] = {
    "ModelViewSet": ("list", "create", "retrieve", "update", "partial_update", "destroy"),
    "ReadOnlyModelViewSet": ("list", "retrieve"),
    "ListModelMixin": ("list",),
    "CreateModelMixin": ("create",),
    "RetrieveModelMixin": ("retrieve",),
    "UpdateModelMixin": ("update", "partial_update"),
    "DestroyModelMixin": ("destroy",),
}
#: The classes that make a view a viewset (dispatch by action, routed by a router).
VIEWSET_BASES = frozenset({"ViewSet", "GenericViewSet", "ViewSetMixin", "ModelViewSet",
                           "ReadOnlyModelViewSet"})

#: A router's standard actions → (verb, whether the address has the lookup).
ACTION_ROUTES: dict[str, tuple[str, bool]] = {
    "list": ("GET", False),
    "create": ("POST", False),
    "retrieve": ("GET", True),
    "update": ("PUT", True),
    "partial_update": ("PATCH", True),
    "destroy": ("DELETE", True),
}

ROUTERS = frozenset({"rest_framework.routers.DefaultRouter", "rest_framework.routers.SimpleRouter"})
#: Router methods that build the URLs and the verb → action mapping: a subclass that overrides one
#: is not a standard router.
ROUTER_URL_METHODS = frozenset({"get_routes", "get_urls", "get_method_map", "get_lookup_regex",
                                "_get_dynamic_route", "urls"})

#: Permission classes that reject an anonymous caller on every method.
CERTAIN_PERMISSIONS = frozenset({
    "rest_framework.permissions.IsAuthenticated",
    "rest_framework.permissions.IsAdminUser",
    "rest_framework.permissions.DjangoModelPermissions",
    "rest_framework.permissions.DjangoObjectPermissions",
})
#: …and the ones that do on the methods that change data only.
CERTAIN_UNSAFE = frozenset({
    "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    "rest_framework.permissions.DjangoModelPermissionsOrAnonReadOnly",
})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Grants every call: not a guard.
ALLOW_ANY = "rest_framework.permissions.AllowAny"

#: The verbs whose request carries a payload (``requestDTO``) and the ones whose response does
#: not (``responseDTO``).
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
NO_RESPONSE_BODY = frozenset({"DELETE"})


def permission_certain(origins: list[str], method: str | None) -> bool:
    """Whether DRF permission classes (by origin) certainly reject an anonymous ``method`` call."""
    return any(o in CERTAIN_PERMISSIONS or (o in CERTAIN_UNSAFE and method in UNSAFE_METHODS)
               for o in origins)
