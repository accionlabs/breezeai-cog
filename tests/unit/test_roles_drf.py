"""Role table: docs/entry-point-roles/drf.md."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

SETTINGS = "ROOT_URLCONF = 'project.urls'\n"
URLS = "from django.urls import path, include\nfrom rest_framework.routers import DefaultRouter\n"
VIEWS = (
    "from rest_framework import viewsets, generics, permissions\n"
    "from rest_framework.decorators import action, api_view\n"
)


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _one(stmts, method, endpoint):
    found = [s for s in _eps(stmts) if s.method == method and s.endpoint == endpoint]
    assert len(found) == 1, [(s.method, s.endpoint, s.handler) for s in _eps(stmts)]
    return found[0]


def _project(urls: str, views: str, settings: str = "") -> dict[str, str]:
    return {"project/settings.py": SETTINGS + settings, "project/urls.py": URLS + urls,
            "shop/__init__.py": "", "shop/views.py": VIEWS + views}


ROUTER = ("from shop.views import OrderViewSet\nrouter = DefaultRouter()\n"
          "router.register('orders', OrderViewSet)\nurlpatterns = [path('api/', include(router.urls))]\n")


def test_model_viewset_is_one_binding_per_action(analyze_files) -> None:
    st = analyze_files(_project(ROUTER, "class OrderViewSet(viewsets.ModelViewSet):\n    pass\n"))
    assert sorted((s.method, s.endpoint, s.handler) for s in _eps(st)) == [
        ("DELETE", "/api/orders/<pk>/", "destroy"), ("GET", "/api/orders/", "list"),
        ("GET", "/api/orders/<pk>/", "retrieve"), ("PATCH", "/api/orders/<pk>/", "partial_update"),
        ("POST", "/api/orders/", "create"), ("PUT", "/api/orders/<pk>/", "update")]
    r = _one(st, "GET", "/api/orders/")
    assert (r.framework, r.routeKind, r.handlerLine, r.handlerPath, r.path) == (
        "drf", "route", 3, "shop/views.py", "project/urls.py")


def test_viewset_actions_come_from_its_mixins_and_own_methods(analyze_files) -> None:
    st = analyze_files(_project(ROUTER, (
        "from rest_framework import mixins\n"
        "class OrderViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):\n"
        "    lookup_field = 'slug'\n"
        "    def retrieve(self, request, slug=None):\n        pass\n")))
    assert sorted((s.method, s.endpoint, s.handler, s.handlerLine) for s in _eps(st)) == [
        ("GET", "/api/orders/", "list", 4), ("GET", "/api/orders/<slug>/", "retrieve", 6)]


def test_extra_action_is_one_binding_per_method(analyze_files) -> None:
    st = analyze_files(_project(ROUTER, (
        "class OrderViewSet(viewsets.ViewSet):\n"
        "    @action(detail=True, methods=['post', 'delete'], url_path='pay-now')\n"
        "    def pay(self, request, pk=None):\n        pass\n"
        "    @action(detail=False)\n    def recent(self, request):\n        pass\n")))
    assert sorted((s.method, s.endpoint, s.handler) for s in _eps(st)) == [
        ("DELETE", "/api/orders/<pk>/pay-now/", "pay"), ("GET", "/api/orders/recent/", "recent"),
        ("POST", "/api/orders/<pk>/pay-now/", "pay")]


def test_router_without_trailing_slash(analyze_files) -> None:
    st = analyze_files(_project(
        ROUTER.replace("DefaultRouter()", "DefaultRouter(trailing_slash=False)"),
        "class OrderViewSet(viewsets.ReadOnlyModelViewSet):\n    pass\n"))
    assert sorted(s.endpoint for s in _eps(st)) == ["/api/orders", "/api/orders/<pk>"]


def test_regex_prefix_has_no_address(analyze_files) -> None:
    st = analyze_files(_project(
        ROUTER.replace("'orders'", r"r'users/(?P<uid>\d+)/orders'"),
        "class OrderViewSet(viewsets.ReadOnlyModelViewSet):\n    pass\n"))
    assert {s.endpoint for s in _eps(st)} == {None}


def test_router_included_by_urlpatterns_and_from_another_file(analyze_files) -> None:
    st = analyze_files({
        "project/settings.py": SETTINGS,
        "project/urls.py": URLS + "urlpatterns = [path('v1/', include('shop.urls'))]\n",
        "shop/__init__.py": "",
        "shop/routers.py": URLS + "router = DefaultRouter()\n",
        "shop/views.py": VIEWS + "class OrderViewSet(viewsets.ReadOnlyModelViewSet):\n    pass\n",
        "shop/api.py": "from shop.routers import router\nfrom shop.views import OrderViewSet\n"
                       "router.register('orders', OrderViewSet)\n",
        "shop/urls.py": "from shop.routers import router\nurlpatterns = router.urls\n",
    })
    assert sorted(s.endpoint for s in _eps(st)) == ["/v1/orders/", "/v1/orders/<pk>/"]
    assert {s.path for s in _eps(st)} == {"shop/api.py"}


def test_router_included_nowhere_has_no_address(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop.views import OrderViewSet\nrouter = DefaultRouter()\n"
        "router.register('orders', OrderViewSet)\nurlpatterns = []\n",
        "class OrderViewSet(viewsets.ReadOnlyModelViewSet):\n    pass\n"))
    assert {s.endpoint for s in _eps(st)} == {None} and len(_eps(st)) == 2


def test_generic_api_view_is_one_binding_per_method(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop.views import Orders\nurlpatterns = [path('orders/', Orders.as_view())]\n",
        "class Orders(generics.ListCreateAPIView):\n"
        "    serializer_class = OrderSerializer\n"
        "    def create(self, request):\n        pass\n"))
    get, post = _one(st, "GET", "/orders/"), _one(st, "POST", "/orders/")
    assert (get.handler, get.handlerLine, get.framework) == ("list", 3, "drf")
    assert (post.handler, post.handlerLine) == ("create", 5)


def test_api_view_with_own_get(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop.views import Me\nurlpatterns = [path('me/', Me.as_view())]\n",
        "from rest_framework.views import APIView\n"
        "class Me(APIView):\n    def get(self, request):\n        pass\n"))
    assert (_one(st, "GET", "/me/").handler, _one(st, "GET", "/me/").framework) == ("get", "drf")


def test_viewset_as_view_with_a_mapping(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop.views import OrderViewSet\n"
        "urlpatterns = [path('orders/', OrderViewSet.as_view({'get': 'list', 'post': 'create'}))]\n",
        "class OrderViewSet(viewsets.ModelViewSet):\n    pass\n"))
    assert sorted((s.method, s.handler) for s in _eps(st)) == [("GET", "list"), ("POST", "create")]


def test_api_view_function(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop.views import ping, hook\nurlpatterns = [path('ping/', ping), path('hook/', hook)]\n",
        "@api_view(['GET', 'POST'])\ndef ping(request):\n    pass\n"
        "@api_view()\ndef hook(request):\n    pass\n"))
    assert sorted((s.endpoint, s.method, s.framework) for s in _eps(st)) == [
        ("/hook/", "GET", "drf"), ("/ping/", "GET", "drf"), ("/ping/", "POST", "drf")]


def test_permission_classes_are_guards(analyze_files) -> None:
    st = analyze_files(_project(ROUTER, (
        "class IsOwner(permissions.BasePermission):\n    pass\n"
        "class OrderViewSet(viewsets.ModelViewSet):\n"
        "    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwner]\n")))
    get, post = _one(st, "GET", "/api/orders/"), _one(st, "POST", "/api/orders/")
    assert get.guards == ["permissions.IsAuthenticatedOrReadOnly", "IsOwner"]
    assert (get.authRequired, post.authRequired) == (None, True)


def test_permission_inherited_from_a_base_and_allow_any_is_not_a_guard(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop.views import Me, Open\nurlpatterns = [path('me/', Me.as_view()), path('open/', Open.as_view())]\n",
        "from rest_framework.views import APIView\n"
        "from rest_framework.permissions import IsAuthenticated, AllowAny\n"
        "class AuthMixin:\n    permission_classes = (IsAuthenticated,)\n"
        "class Me(AuthMixin, APIView):\n    def get(self, request):\n        pass\n"
        "class Open(APIView):\n    permission_classes = [AllowAny]\n    def get(self, request):\n        pass\n"))
    me, open_ = _one(st, "GET", "/me/"), _one(st, "GET", "/open/")
    assert (me.guards, me.authRequired) == (["IsAuthenticated"], True)
    assert (open_.guards, open_.authRequired) == (None, None)


def test_permission_classes_decorator_on_api_view(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop.views import ping\nurlpatterns = [path('ping/', ping)]\n",
        "from rest_framework.decorators import permission_classes\n"
        "@api_view(['GET'])\n@permission_classes([permissions.IsAdminUser])\ndef ping(request):\n    pass\n"))
    r = _one(st, "GET", "/ping/")
    assert (r.guards, r.authRequired) == (["permissions.IsAdminUser"], True)


def test_default_permission_classes_from_settings(analyze_files) -> None:
    st = analyze_files(_project(ROUTER, "class OrderViewSet(viewsets.ReadOnlyModelViewSet):\n    pass\n",
                                settings=("REST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES': "
                                          "['rest_framework.permissions.IsAuthenticated']}\n")))
    r = _one(st, "GET", "/api/orders/")
    assert (r.guards, r.authRequired) == (["rest_framework.permissions.IsAuthenticated"], True)


def test_serializer_class_is_the_dto_of_actions_drf_provides(analyze_files) -> None:
    st = analyze_files(_project(ROUTER, (
        "class OrderViewSet(viewsets.ModelViewSet):\n    serializer_class = OrderSerializer\n"
        "    def update(self, request, pk=None):\n        pass\n")))
    post, get = _one(st, "POST", "/api/orders/"), _one(st, "GET", "/api/orders/<pk>/")
    assert (post.requestDTO, post.responseDTO) == ("OrderSerializer", "OrderSerializer")
    assert (get.requestDTO, get.responseDTO) == (None, "OrderSerializer")
    assert _one(st, "DELETE", "/api/orders/<pk>/").responseDTO is None
    assert _one(st, "PUT", "/api/orders/<pk>/").requestDTO is None  # overridden


def test_get_serializer_class_leaves_the_dto_empty(analyze_files) -> None:
    st = analyze_files(_project(ROUTER, (
        "class OrderViewSet(viewsets.ModelViewSet):\n    serializer_class = OrderSerializer\n"
        "    def get_serializer_class(self):\n        return OrderSerializer\n")))
    assert {s.responseDTO for s in _eps(st)} == {None}


def test_default_router_root_view_is_none(analyze_files) -> None:
    st = analyze_files(_project(
        "router = DefaultRouter()\nurlpatterns = [path('api/', include(router.urls))]\n", ""))
    assert _eps(st) == []


def test_http_client_call_is_outbound(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop.views import ping\nurlpatterns = [path('ping/', ping)]\n",
        "import requests\n@api_view(['GET'])\ndef ping(request):\n    return requests.get('https://example.com')\n"))
    assert [s.method for s in st if s.semanticType == "api_call"] == ["GET"]


def _custom_router(body: str) -> dict[str, str]:
    files = _project(
        "from shop.views import OrderViewSet\nfrom project.routers import ShopRouter\nrouter = ShopRouter()\n"
        "router.register('orders', OrderViewSet)\nurlpatterns = [path('api/', include(router.urls))]\n",
        "class OrderViewSet(viewsets.ReadOnlyModelViewSet):\n    pass\n")
    files["project/routers.py"] = "from rest_framework.routers import DefaultRouter\n" \
                                  "class ShopRouter(DefaultRouter):\n" + body
    return files


def test_repo_router_subclass_that_adds_to_the_routes_is_a_standard_router(analyze_files) -> None:
    st = analyze_files(_custom_router(
        "    def __init__(self, *args, **kwargs):\n        super().__init__(*args, **kwargs)\n"
        "        self.routes[0].mapping.update({'put': 'bulk_update'})\n"
        "    def get_api_root_view(self, api_urls=None):\n        pass\n"))
    assert sorted((s.method, s.endpoint, s.handler) for s in _eps(st)) == [
        ("GET", "/api/orders/", "list"), ("GET", "/api/orders/<pk>/", "retrieve")]


def test_repo_router_subclass_of_a_subclass(analyze_files) -> None:
    files = _custom_router("    pass\n")
    files["project/urls.py"] = files["project/urls.py"].replace(
        "from project.routers import ShopRouter\nrouter = ShopRouter()",
        "from project.routers import ShopRouter\nclass ApiRouter(ShopRouter):\n    pass\nrouter = ApiRouter()")
    assert len(_eps(analyze_files(files))) == 2


def test_repo_router_subclass_that_changes_the_routes_is_not_one(analyze_files) -> None:
    for body in ("    routes = []\n",
                 "    def __init__(self):\n        super().__init__()\n        self.routes = []\n",
                 "    def get_routes(self, viewset):\n        return []\n",
                 "    def __init__(self):\n        super().__init__(trailing_slash=False)\n",
                 "    def __init__(self, slash=False):\n        super().__init__(slash)\n",
                 "    def __init__(self, slash=False):\n        DefaultRouter.__init__(self, slash)\n",
                 "    def get_urls(self):\n        return []\n",
                 "    @property\n    def urls(self):\n        return []\n"):
        assert _eps(analyze_files(_custom_router(body))) == [], body


def test_router_subclass_of_another_library_is_not_one(analyze_files) -> None:
    files = _custom_router("    pass\n")
    files["project/routers.py"] = ("from rest_framework_nested.routers import NestedSimpleRouter\n"
                                   "class ShopRouter(NestedSimpleRouter):\n    pass\n")
    assert _eps(analyze_files(files)) == []


def test_aliased_router_and_positional_trailing_slash(analyze_files) -> None:
    st = analyze_files(_project(
        ROUTER.replace("router = DefaultRouter()", "from rest_framework.routers import SimpleRouter as R\n"
                                                   "router = R(False)"),
        "class OrderViewSet(viewsets.ReadOnlyModelViewSet):\n    pass\n"))
    assert sorted(s.endpoint for s in _eps(st)) == ["/api/orders", "/api/orders/<pk>"]


def test_repo_router_made_with_positional_arguments_is_not_read(analyze_files) -> None:
    files = _custom_router("    def __init__(self, *args, **kwargs):\n        super().__init__(*args, **kwargs)\n")
    files["project/urls.py"] = files["project/urls.py"].replace("router = ShopRouter()", "router = ShopRouter(False)")
    assert _eps(analyze_files(files)) == []
