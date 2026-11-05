"""Role table: docs/entry-point-roles/django.md."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

SETTINGS = "ROOT_URLCONF = 'project.urls'\n"
URLS = "from django.urls import path, re_path, include\n"


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _one(stmts, method, endpoint):
    found = [s for s in _eps(stmts) if s.method == method and s.endpoint == endpoint]
    assert len(found) == 1, [(s.method, s.endpoint, s.handler) for s in _eps(stmts)]
    return found[0]


def _project(urls: str, **files: str) -> dict[str, str]:
    return {"project/settings.py": SETTINGS, "project/urls.py": URLS + urls, **files}


def test_path_to_a_function_is_one_any_binding(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop import views\nurlpatterns = [path('orders/<int:pk>/', views.detail)]\n",
        **{"shop/__init__.py": "", "shop/views.py": "def detail(request, pk):\n    pass\n"}))
    r = _one(st, "ANY", "/orders/<int:pk>/")
    assert (r.routeKind, r.framework, r.handler, r.handlerLine, r.handlerPath, r.isRegex) == (
        "route", "django", "detail", 1, "shop/views.py", False)
    assert r.path == "project/urls.py"  # the record sits on the pattern, not the view


def test_require_decorators_give_one_binding_per_verb(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.views.decorators.http import require_http_methods, require_safe\n"
        "@require_http_methods(['GET', 'POST'])\ndef a(request):\n    pass\n"
        "@require_safe\ndef b(request):\n    pass\n"
        "urlpatterns = [path('a/', a), path('b/', b)]\n"))
    assert sorted((s.endpoint, s.method) for s in _eps(st)) == [
        ("/a/", "GET"), ("/a/", "POST"), ("/b/", "GET"), ("/b/", "HEAD")]


def test_re_path_literal_becomes_a_path_and_a_real_regex_is_flagged(analyze_files) -> None:
    st = analyze_files(_project(
        "def v(request):\n    pass\n"
        "urlpatterns = [re_path(r'^about/$', v), re_path(r'^posts/(?P<year>[0-9]{4})/$', v)]\n"))
    assert _one(st, "ANY", "/about/").isRegex is False
    assert _one(st, "ANY", "/posts/(?P<year>[0-9]{4})/").isRegex is True


def test_regex_joined_to_a_converter_has_no_address(analyze_files) -> None:
    st = analyze_files(_project(
        "urlpatterns = [re_path(r'^(?P<lang>[a-z]+)/', include('shop.urls'))]\n",
        **{"shop/__init__.py": "", "shop/urls.py": URLS + (
            "def v(request, pk):\n    pass\nurlpatterns = [path('item/<int:pk>/', v)]\n")}))
    assert [s.endpoint for s in _eps(st)] == [None]


def test_class_based_view_is_one_binding_per_method(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.views import View\n"
        "class Orders(View):\n"
        "    def get(self, request):\n        pass\n"
        "    def post(self, request):\n        pass\n"
        "urlpatterns = [path('orders/', Orders.as_view())]\n"))
    assert _one(st, "GET", "/orders/").handlerLine == 4
    assert _one(st, "POST", "/orders/").handler == "post"


def test_methods_inherited_from_a_generic_view_are_located_at_the_class(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.views.generic import CreateView\nfrom django.views import generic\n"
        "class NewOrder(CreateView):\n    pass\n"
        "class Old(generic.DeleteView):\n    def delete(self, request):\n        pass\n"
        "urlpatterns = [path('new/', NewOrder.as_view()), path('old/', Old.as_view())]\n"))
    assert sorted((s.method, s.handler, s.handlerLine) for s in _eps(st) if s.endpoint == "/new/") == [
        ("GET", "get", 4), ("POST", "post", 4), ("PUT", "put", 4)]
    assert sorted((s.method, s.handlerLine) for s in _eps(st) if s.endpoint == "/old/") == [
        ("DELETE", 7), ("GET", 6), ("POST", 6)]


def test_view_inherits_a_method_from_a_base_in_another_file(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop.views import Orders\nurlpatterns = [path('orders/', Orders.as_view())]\n",
        **{"shop/__init__.py": "",
           "shop/base.py": "from django.views import View\nclass BaseView(View):\n    def get(self, request):\n        pass\n",
           "shop/views.py": "from shop.base import BaseView\nclass Orders(BaseView):\n    pass\n"}))
    r = _one(st, "GET", "/orders/")
    assert (r.handler, r.handlerLine, r.handlerPath) == ("get", 3, "shop/base.py")


def test_dispatch_only_view_is_any(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.views import View\n"
        "class Hook(View):\n    def dispatch(self, request):\n        pass\n"
        "urlpatterns = [path('hook/', Hook.as_view())]\n"))
    assert _one(st, "ANY", "/hook/").handler == "dispatch"


def test_view_with_a_base_from_another_library_has_no_method(analyze_files) -> None:
    st = analyze_files(_project(
        "from contact_form.views import ContactFormView\n"
        "class Contact(ContactFormView):\n    pass\n"
        "urlpatterns = [path('contact/', Contact.as_view())]\n"))
    r = _one(st, None, "/contact/")
    assert (r.handler, r.handlerLine) == ("Contact", 3)


def test_django_view_used_directly_is_named_and_framework_served(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.views.generic import TemplateView, RedirectView\n"
        "urlpatterns = [path('', TemplateView.as_view(template_name='home.html')),\n"
        "               path('old/', RedirectView.as_view(url='/'))]\n"))
    home, old = _one(st, "GET", "/"), _one(st, "ANY", "/old/")
    assert (home.handler, home.handlerLine, home.handlerPath) == ("TemplateView", None, None)
    assert (old.handler, old.handlerLine) == ("RedirectView", None)


def test_include_is_wiring(analyze_files) -> None:
    st = analyze_files(_project(
        "urlpatterns = [path('api/', include('shop.urls')), path('v2/', include([path('ping/', ping)]))]\n"
        "def ping(request):\n    pass\n",
        **{"shop/__init__.py": "", "shop/urls.py": URLS + (
            "from . import views\nurlpatterns = [path('orders/', views.orders)]\n"),
           "shop/views.py": "def orders(request):\n    pass\n"}))
    assert sorted(s.endpoint for s in _eps(st)) == ["/api/orders/", "/v2/ping/"]
    assert _one(st, "ANY", "/api/orders/").path == "shop/urls.py"


def test_include_of_an_imported_module_and_a_namespace_tuple(analyze_files) -> None:
    st = analyze_files(_project(
        "from shop import urls as shop_urls\n"
        "urlpatterns = [path('a/', include(shop_urls)), path('b/', include(('blog.urls', 'blog')))]\n",
        **{"shop/__init__.py": "", "shop/urls.py": URLS + "def v(r):\n    pass\nurlpatterns = [path('x/', v)]\n",
           "blog/__init__.py": "", "blog/urls.py": URLS + "def w(r):\n    pass\nurlpatterns = [path('y/', w)]\n"}))
    assert sorted(s.endpoint for s in _eps(st)) == ["/a/x/", "/b/y/"]


def test_urlconf_included_twice_or_not_reached_has_no_address(analyze_files) -> None:
    st = analyze_files(_project(
        "urlpatterns = [path('v1/', include('shop.urls')), path('v2/', include('shop.urls'))]\n",
        **{"shop/__init__.py": "", "shop/urls.py": URLS + "def v(r):\n    pass\nurlpatterns = [path('x/', v)]\n",
           "lone/__init__.py": "", "lone/urls.py": URLS + "def w(r):\n    pass\nurlpatterns = [path('y/', w)]\n"}))
    assert sorted((s.handler, s.endpoint) for s in _eps(st)) == [("v", None), ("w", None)]


def test_root_urlconf_is_wiring(analyze_files) -> None:
    st = analyze_files(_project("def home(r):\n    pass\nurlpatterns = [path('', home)]\n"))
    _one(st, "ANY", "/")


def test_library_urls_are_a_mount(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.contrib import admin\n"
        "urlpatterns = [path('admin/', admin.site.urls), path('accounts/', include('allauth.urls'))]\n"))
    mounts = sorted((s.endpoint, s.handler, s.routeKind) for s in st if s.semanticType == "route")
    assert mounts == [("/accounts/", "allauth.urls", "mount"), ("/admin/", "admin.site.urls", "mount")]
    assert _eps(st) == []


def test_patterns_added_later_and_under_debug(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.conf import settings\ndef a(r):\n    pass\n"
        "urlpatterns = [path('a/', a)]\nurlpatterns += [path('b/', a)]\nurlpatterns.append(path('c/', a))\n"
        "if settings.DEBUG:\n    urlpatterns += [path('d/', a)]\n"))
    assert sorted(s.endpoint for s in _eps(st)) == ["/a/", "/b/", "/c/", "/d/"]


def test_i18n_patterns_have_no_address(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.conf.urls.i18n import i18n_patterns\ndef a(r):\n    pass\n"
        "urlpatterns = i18n_patterns(path('about/', a))\n"))
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("ANY", None)]


def test_static_is_none(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.conf import settings\nfrom django.conf.urls.static import static\n"
        "urlpatterns = [] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)\n"))
    assert [s for s in st if s.semanticType == "route"] == []


def test_function_view_decorators_are_guards(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.contrib.auth.decorators import login_required\n"
        "from django.views.decorators.csrf import csrf_exempt\n"
        "@csrf_exempt\n@login_required\n@team_only\ndef a(r):\n    pass\n"
        "urlpatterns = [path('a/', a)]\n"))
    r = _one(st, "ANY", "/a/")
    assert (r.guards, r.authRequired) == (["login_required", "team_only"], True)


def test_own_login_required_makes_no_claim(analyze_files) -> None:
    st = analyze_files(_project(
        "from project.auth import login_required\n@login_required\ndef a(r):\n    pass\n"
        "urlpatterns = [path('a/', a)]\n", **{"project/auth.py": "def login_required(f):\n    return f\n"}))
    assert (_one(st, "ANY", "/a/").guards, _one(st, "ANY", "/a/").authRequired) == (["login_required"], None)


def test_method_decorator_on_class_and_method_are_guards(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.contrib.auth.decorators import login_required\n"
        "from django.utils.decorators import method_decorator\nfrom django.views import View\n"
        "@method_decorator(login_required, name='dispatch')\n"
        "class A(View):\n"
        "    @method_decorator(audit)\n    def post(self, r):\n        pass\n"
        "urlpatterns = [path('a/', A.as_view())]\n"))
    r = _one(st, "POST", "/a/")
    assert (r.guards, r.authRequired) == (["login_required", "audit"], True)


def test_auth_mixins_are_guards(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin\n"
        "from django.views.generic import ListView\n"
        "class A(LoginRequiredMixin, ListView):\n    pass\n"
        "class B(UserPassesTestMixin, ListView):\n    pass\n"
        "urlpatterns = [path('a/', A.as_view()), path('b/', B.as_view())]\n"))
    a, b = _one(st, "GET", "/a/"), _one(st, "GET", "/b/")
    assert (a.guards, a.authRequired) == (["LoginRequiredMixin"], True)
    assert (b.guards, b.authRequired) == (["UserPassesTestMixin"], None)


def test_middleware_guards_every_route_when_settings_agree(analyze_files) -> None:
    mw = ("MIDDLEWARE = ['django.contrib.sessions.middleware.SessionMiddleware',\n"
          "              'django.contrib.auth.middleware.LoginRequiredMiddleware']\n")
    st = analyze_files({
        "project/settings/base.py": SETTINGS + mw,
        "project/settings/prod.py": "from .base import *\n" + mw,
        "project/urls.py": URLS + (
            "from django.contrib.auth.decorators import login_not_required\n"
            "def a(r):\n    pass\n@login_not_required\ndef b(r):\n    pass\n"
            "urlpatterns = [path('a/', a), path('b/', b)]\n"),
    })
    a, b = _one(st, "ANY", "/a/"), _one(st, "ANY", "/b/")
    assert a.guards == ["django.contrib.sessions.middleware.SessionMiddleware",
                        "django.contrib.auth.middleware.LoginRequiredMiddleware"]
    assert (a.authRequired, b.authRequired) == (True, None)


def test_middleware_settings_that_disagree_are_left_out(analyze_files) -> None:
    st = analyze_files({
        "project/settings/base.py": SETTINGS + "MIDDLEWARE = ['a.A']\n",
        "project/settings/dev.py": "from .base import *\nMIDDLEWARE += ['debug.D']\n",
        "project/urls.py": URLS + "def a(r):\n    pass\nurlpatterns = [path('a/', a)]\n",
    })
    assert _one(st, "ANY", "/a/").guards is None


def test_response_decorators_are_not_guards(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.views.decorators.cache import cache_page\n"
        "@cache_page(60)\ndef a(r):\n    pass\nurlpatterns = [path('a/', a)]\n"))
    assert _one(st, "ANY", "/a/").guards is None


def test_http_client_call_is_outbound(analyze_files) -> None:
    st = analyze_files(_project(
        "import requests\ndef a(r):\n    return requests.post('https://example.com/hook')\n"
        "urlpatterns = [path('a/', a)]\n"))
    assert [s.method for s in st if s.semanticType == "api_call"] == ["POST"]


def test_models_and_admin_are_none(analyze_files) -> None:
    st = analyze_files({
        "shop/models.py": "from django.db import models\nclass Order(models.Model):\n    total = models.IntegerField()\n",
        "shop/admin.py": "from django.contrib import admin\nfrom .models import Order\nadmin.site.register(Order)\n",
    })
    assert [s for s in st if s.semanticType == "route"] == []


def test_view_wrapped_in_the_urlconf(analyze_files) -> None:
    st = analyze_files(_project(
        "from functools import partial\nfrom django.contrib.auth.decorators import login_required\n"
        "from django.views.decorators.cache import cache_page\n"
        "def v(r, code=0):\n    pass\n"
        "urlpatterns = [path('a/', login_required(v)), path('b/', cache_page(60)(v)),\n"
        "               path('c/', partial(v, code=400))]\n"))
    a = _one(st, "ANY", "/a/")
    assert (a.handler, a.handlerLine, a.guards, a.authRequired) == ("v", 5, ["login_required"], True)
    assert (_one(st, "ANY", "/b/").handler, _one(st, "ANY", "/b/").guards) == ("v", None)
    assert _one(st, "ANY", "/c/").handler == "v"


def test_instance_of_a_view_class_is_any(analyze_files) -> None:
    st = analyze_files(_project(
        "from django.contrib.syndication.views import Feed\n"
        "class Latest(Feed):\n    pass\n"
        "class Hook:\n    def __call__(self, request):\n        pass\n"
        "urlpatterns = [path('rss/', Latest()), path('hook/', Hook())]\n"))
    assert (_one(st, "ANY", "/rss/").handler, _one(st, "ANY", "/rss/").handlerLine) == ("Latest", 3)
    assert (_one(st, "ANY", "/hook/").handler, _one(st, "ANY", "/hook/").handlerLine) == ("__call__", 6)


def test_lambda_view_is_anonymous(analyze_files) -> None:
    st = analyze_files(_project("urlpatterns = [path('ping/', lambda request: None)]\n"))
    r = _one(st, "ANY", "/ping/")
    assert (r.handler, r.handlerLine) == (None, 2)

