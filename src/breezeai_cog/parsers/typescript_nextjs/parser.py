"""Retired: Next.js is no longer a one-per-file framework parser.

It owned ``app/**/route.*`` and ``pages/api/**`` files, so a route file another parser also
claims — Apollo's ``app/api/graphql/route.ts`` (GraphQL), a route importing a ``@nestjs/…``
client (NestJS), an ``@angular/…`` import — lost its routes to the selection. Route handlers are
now the additive ``nextjs-routes`` detector (``routes.py``), like the pages (``pages.py``) and
Express: they run in ``TypeScriptParser.extract`` for every TS file, whatever parser owns it.
The ``nextjs`` framework capability is carried by the detectors' ``frameworks``.
"""
