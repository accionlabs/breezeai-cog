# Apex (Salesforce) — entry-point roles

Parser: `src/breezeai_cog/parsers/apex/` (`routes.py` entry points, `detect.py` call classification, `jobs.py` schedules and events) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_apex.py`

Apex runs inside Salesforce, so control enters it in platform-specific ways: an HTTP request to a
REST class, a Lightning component calling an `@AuraEnabled` method, a SOAP call, a record change
or event message that fires a trigger, or a schedule. Names and keywords are case-insensitive
(`@AuraEnabled` = `@auraenabled`); test code (`@IsTest` classes and methods, `testMethod`) and
fixture files never produce an entry point.

| Construct | Role | Record | Why |
|---|---|---|---|
| `@HttpGet` / `@HttpPost` / `@HttpPut` / `@HttpPatch` / `@HttpDelete` on a method of a `@RestResource(urlMapping='/x/*')` class | Binding | `route`, `framework: apex-rest`, `routeKind: route`, the verb, `endpoint: /services/apexrest/x/*` (the platform's fixed prefix + the mapping as written), `handler` = the method, `responseDTO` = its return type (none for `void`) | One handler per verb, at a URL |
| `@RestResource(urlMapping=…)` on a class | Wiring | — (its mapping is in each verb's `endpoint`) | Names no handler of its own |
| `@AuraEnabled` on a method (also `cacheable=true`) | Binding | `route`, `framework: apex-aura`, `routeKind: rpc`, `method: RPC`, `endpoint: Class.method` — the form a Lightning component imports (`@salesforce/apex/Class.method`) | A component calls exactly this method by name |
| `webservice` method | Binding | `route`, `framework: apex-soap`, `routeKind: rpc`, `method: RPC`, `endpoint: Class/method` | A SOAP operation of the class's generated WSDL |
| `trigger T on Account (before insert, …)` | Binding | `eventbus_consumer`, `framework: apex-trigger`, `method: EVENT`, `endpoint` = the sObject, `handler` = the trigger, `text` = its header | The platform runs it when records of that sObject change — from the UI, the API, data loads or Apex |
| `trigger T on Order_Event__e (after insert)` (Platform Event), `on AccountChangeEvent` / `on X__ChangeEvent` (Change Data Capture) | Binding | as above, `framework: apex-platform-event`, `endpoint` = the channel | Runs when a message arrives on that channel |
| `System.schedule(name, cron, new Job())` | Binding | `timer`, `framework: apex-schedule`, `method: PERIODIC`, `endpoint` = the cron literal, `handler: execute`; `handlerLine` its line, and `handlerPath` when the job class is in another file. A job in a variable uses its declared type; a cron that is not a literal leaves `endpoint` unset | Names one job and its schedule (the Quartz `scheduleJob` shape) |
| A class that `implements Schedulable` | None | — (it is the handler) | No schedule of its own; one set only in Salesforce Setup is not in the code |
| `EventBus.publish(new X__e(…))`, or of a variable / list declared as `X__e` | Outbound | `eventbus_publish`, `framework: apex-platform-event`, `endpoint` = the channel, no `method`; `endpoint` unset when the type is not visible | Sends to the channel a Platform Event trigger consumes |
| `new Http().send(req)` / `h.send(req)` with `h` declared `Http` | Outbound | `api_call`, `method` / `endpoint` from the `req.setMethod` / `req.setEndpoint` in force at the call; unset when a branch could change them. A Named Credential URL is kept as written (`callout:ERP/orders/{id}`) | An HTTP callout |
| `System.enqueueJob`, `Database.executeBatch`, a `@future` method | None | — | Our own code starting our own code |
| Inline `[SELECT …]` / `[FIND …]`, `Database.query` and the other query methods, DML (`insert x;`, `Database.update(…)`) | — | `query_statement` (no `method` when inline; the called method otherwise) / `db_method_call` with the DML verb, `dataAccessHint: salesforce` | Data access, not entry points |

## Values beyond the Target Spec

These follow the spec's fields but use values its lists do not include yet:

| Field | Value | Meaning |
|---|---|---|
| `File.language` | `apex` | |
| `framework` | `apex-rest`, `apex-aura`, `apex-soap`, `apex-trigger`, `apex-platform-event`, `apex-schedule` | one label per way Apex is entered or sends |
| `dataAccessHint` | `salesforce` | SOQL / SOSL / DML: the platform's own data layer, not an ORM |
| `Function.type` | `trigger` | the trigger declaration: it has no class (not a `method`) and no caller (not a `function`) |
| `visibility` | `global` | visible to other namespaces and packages — wider than `public` (required for `webservice`, global REST classes and managed-package APIs) |
| `Class.metadata.sharing` | `with` / `without` / `inherited` | whether the class's queries and DML respect the running user's record access; the Apex form of definer / invoker rights |
| `Function.metadata` on a trigger | `sObject`, `events` (`["before insert", "after update"]`) | which records, which changes and before or after the save — kept on the Function so semantic search finds them |

Open address forms: `Class.method` for Aura (the RPC form elsewhere is `Service/Operation`), an
sObject such as `Account` as a consumer's channel, and `callout:` Named Credential endpoints.
