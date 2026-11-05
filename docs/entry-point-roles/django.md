# Django — entry-point roles

Parser: `src/breezeai_cog/parsers/python_django/` (`python-django`; URL includes, `ROOT_URLCONF` and settings across files: the `django.urlconf` fact; views declared in other files: the `django.views` fact) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_django.py`

The binding is the URL pattern (`path(…)` in `urlpatterns`), not the view: a view has no address of
its own until a pattern names it. The record sits on the `path(…)` call and leads to the view in
its own file (`handlerPath`, `handlerLine`). Django REST framework views reached through
`path(…)` follow the same rows with `framework: drf` — see [DRF](drf.md).

**Address.** `/` + the prefixes of the `include()`s that lead to the pattern from the root
urlconf (`ROOT_URLCONF`) + the pattern, the way Django concatenates them (`api/` + `orders/<int:pk>/`
→ `/api/orders/<int:pk>/`). Converters are kept as written. A urlconf the code does not reach from a
root — or reaches from two places — has an unknown address: its patterns' `endpoint` is empty.

| Construct | Role | Record | Why |
|---|---|---|---|
| `path(route, view_function)` in `urlpatterns` | Binding | `route`, `method: ANY`, `routeKind: route`, `endpoint` (above), `handler` = the function | One handler for every verb |
| …a function decorated `@require_http_methods(["GET", "POST"])` / `@require_GET` / `@require_POST` / `@require_safe` | Binding, one per verb | `method` = each verb (`require_safe` → `GET`, `HEAD`) | The verbs it accepts |
| `re_path(regex, view)` / `url(regex, view)` | Binding | as above. A regex with no regex syntax is a path (`^about/$` → `/about/`); otherwise `isRegex: true` and the regexes are joined without their `^` / `$` anchors. A regex joined to a converter (`<int:pk>`) → `endpoint` empty | Same binding, written as a regex |
| `path(route, MyView.as_view())` — a class-based view | Binding, one per HTTP method it handles | `method` = the verb, `handler` = the method (`get`, `post`, …) where it is declared — the class or a base class in the repo. A method the class inherits from a Django generic view is named by the method and located at the view class: `TemplateView`, `ListView`, `DetailView` and the date views → `GET`; `FormView`, `CreateView`, `UpdateView` → `GET`, `POST`, `PUT`; `DeleteView` → `GET`, `POST`, `DELETE`; `RedirectView` → `ANY`. A class that only overrides `dispatch` → `ANY`, `handler: dispatch`. A class whose methods the code does not show (a base from another library) → one record, no `method`, `handler` = the class | `as_view()` dispatches each verb to its method |
| `path(route, TemplateView.as_view(template_name=…))` — a Django view used directly | Binding | `method` from the table above, `handler` = the Django view (`TemplateView`), not located (framework-served) | A real URL served by Django's code |
| `path(prefix, include("shop.urls"))` / `include(shop_urls)` / `include([...])` / `include((patterns, "ns"))` | Wiring | statement stays plain; `prefix` joined into the included patterns. A urlconf included at two places is served at each: its patterns' `endpoint` is empty | Points at a urlconf, not a handler |
| `ROOT_URLCONF = "project.urls"` in a settings module | Wiring | — (that urlconf is served from `/`) | The root of every address |
| `path(prefix, include("allauth.urls"))` / `path("admin/", admin.site.urls)` — URLs of a library | Wiring | `route`, `method: ANY`, `routeKind: mount`, `endpoint` = the prefix, `handler` = the included module as written | Its routes aren't in the repo: the mount is the only record of the address |
| `urlpatterns += [...]`, `urlpatterns.append(path(…))`, patterns under `if settings.DEBUG:` | Binding | as `path(…)` above | More patterns of the same urlconf |
| `i18n_patterns(path(…))` | Binding | as above, `endpoint` empty | The language prefix is chosen at run time |
| `static(settings.MEDIA_URL, …)` | None | — | Serves files in development only |
| Decorators on a function view — `@login_required`, `@permission_required(…)`, `@user_passes_test(…)`, the app's own | Guard | names in `guards`, outermost first. `authRequired: true` only for Django's `login_required`, `permission_required` and `staff_member_required` (imported from `django.contrib`) | They run before the view |
| `@method_decorator(login_required, name="dispatch")` on a class / `@method_decorator(x)` on a method | Guard | `login_required` / `x` in `guards` of the class's / method's records; `authRequired` as above | Same check, on a class-based view |
| `LoginRequiredMixin`, `PermissionRequiredMixin`, `UserPassesTestMixin` as a base | Guard | the mixin in `guards`; the first two set `authRequired: true` | Runs in `dispatch`, before the method |
| `MIDDLEWARE = [...]` in settings — only when every settings module in the repo that sets it agrees and none changes it later (`+=`, `.append`) | Guard | each middleware, as written, in `guards` of every route (outermost). `LoginRequiredMiddleware` (Django 5.1) sets `authRequired: true`, unless the view is `@login_not_required` | Runs before every view |
| `@csrf_exempt`, `@cache_page`, `@never_cache`, `@vary_on_headers`, `@xframe_options_*`, `@sensitive_post_parameters` | None | — | Change how the response is built, not who may call |
| `requests` / `httpx` call | Outbound | `api_call` | Our code calling out |
| Models, forms, admin `register`, signals, management commands | None | — | Not inbound HTTP |
