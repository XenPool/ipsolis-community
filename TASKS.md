# Ipsolis – Task Backlog

Format: `[open]` / `[done]` / `[blocked]`. Add new tasks at the top of their section.
Strategic roadmap up top; smaller polish/gap items in the middle; pre-existing infra
and historical "done" entries at the bottom.

---

## Strategic — Enterprise-class roadmap

These are the gaps that block ipSolis from being drop-in for a 5,000-seat regulated
enterprise. Order = priority (procurement-blocker first).

### [open] Admin RBAC — Prio 0 (show-stopper)
Today the admin UI is binary: `X-Admin-Key` or admin session = god mode.
Roles to design: `superadmin`, `admin`, `approver`, `auditor` (read-only),
`helpdesk` (revoke-only). Per-asset-type ACLs as a stretch goal so platform
owners can be delegated without seeing other teams' configurations.
SoD requirement: configurer of an asset type must not also approve their own
access requests against it.
- [ ] Roles enum + `admin_users` table (or `admin_role` column)
- [ ] Permission check decorator/dependency for each admin endpoint
- [ ] Admin UI — login flow shows role; nav items hide what the role can't reach
- [ ] Audit-log entry includes the role of the actor
- [ ] Migration to map all existing `X-Admin-Key` use to `superadmin`

### [open] External secret management — Prio 0 (show-stopper)
Today AD password / vSphere creds / SMTP password / Entra client secret all
sit in `app_config` as plaintext. The `is_secret=true` flag only hides them
in the UI. Add a `SecretBackend` abstraction with implementations for
`db` (current), `vault`, `azure_keyvault`, `aws_secretsmanager`. Resolution
goes through the backend on read — schema unchanged but values may be
references like `vault://secret/data/ipsolis/ad/password`.
- [ ] `SecretBackend` interface + `db` (no-op) + `vault` (HashiCorp Vault) impl
- [ ] Settings: backend choice + connection params
- [ ] `app_config.value` resolver routes `is_secret=true` rows through the backend
- [ ] One-shot migration tool: move existing plaintext secrets into the chosen backend

### [partial] API tokens with scopes — Prio 0
Slice 1 — table + ORM + bearer auth + Admin UI — **shipped 2026-04-26**.
Scope decorators and the ServiceNow webhook migration are split into
follow-up slices.

**Done — token core (2026-04-26):**
- Migration `0054_api_tokens.py` — `api_tokens` table with SHA-256 hash,
  prefix, JSON scopes, expiry, last-used, soft-delete revocation.
- ORM `app.models.api_token.ApiToken`.
- `app.utils.api_tokens` — `generate_raw_token()`, `create_token()`,
  `verify_raw_token()`, `mark_used()`, `status()`. Tokens are
  `secrets.token_urlsafe(32)` with a recognisable `xpat_` prefix; raw
  value never persisted.
- `app.utils.auth.require_admin_key` extended to accept
  `Authorization: Bearer xpat_…` alongside the legacy `X-Admin-Key`
  and admin session cookie. Stores attribution as
  `request.state.actor` = `token:<name>` / `admin:legacy_key` /
  `admin:session:<user>` so future audit entries can record who
  did what.
- API endpoints `POST /admin/api-tokens` (one-time raw reveal),
  `GET /admin/api-tokens` (list, prefix only), `DELETE /admin/api-tokens/{id}`
  (soft-delete sets `revoked_at`).
- Admin UI page `/ui/api-tokens` with list, create modal (name +
  expiry: 30/90/180/365/730/never), one-time reveal banner with copy
  button, and per-row revoke. Linked from the left nav above License.
- README + `docs/ENTERPRISE_FEATURES.md` updated with auth-paths
  section and the token lifecycle UX.
- Verified end-to-end: create returns plaintext once, list shows
  prefix only, bearer authenticates against admin endpoints,
  bogus tokens return 401, `last_used_at` updates after use,
  revocation returns 204 + immediately blocks further use, row
  preserved with `revoked` status.

**Done — scope catalog + decorators (2026-04-26):**
- 14-scope catalog in `app.utils.api_tokens.AVAILABLE_SCOPES` covering
  orders / asset_types / assets / approvals / audit / config / metrics /
  webhook plus the `admin:*` wildcard.
- `require_scopes(*needed)` factory in `app.utils.auth` — back-compat
  by design: legacy `X-Admin-Key` and admin sessions retain implicit
  `admin:*`, only bearer tokens are scope-gated. Missing scopes return
  HTTP 403 with a message listing both missing and granted scopes so
  integrations can self-diagnose.
- Token create endpoint accepts `scopes: list[str]`; unknown scopes
  filtered silently; empty list defaults to `["admin:*"]` for
  back-compat with slice-1 token UX.
- New `GET /admin/api-tokens/scopes` endpoint exposes the catalog so
  the UI renders checkboxes dynamically.
- Token list response includes `scopes`; UI renders them as badges
  (amber for `admin:*`, neutral for narrow scopes).
- Token create modal: scope picker with checkbox grid, defaults to
  `admin:*` selected, validates at-least-one before submitting.
- Representative endpoints scoped to demonstrate wiring:
  `GET /admin/audit-log` → `audit:read`,
  4× `POST/PUT/DELETE /admin/asset-types/*` → `asset_types:write`,
  `GET /admin/cost-report` → `orders:read`.
- Verified end-to-end: read-only token gets 200 on audit, 403 on
  asset-type create + cost-report (descriptive error). Legacy
  `X-Admin-Key` still grants full access.

**Done — ServiceNow webhook bearer auth (2026-04-26):**
- `POST /webhook/servicenow` accepts either `Authorization: Bearer xpat_…`
  (with `webhook:in` scope, checked first) or the legacy
  `X-Hub-Signature-256` HMAC. Either is sufficient; both paths are
  independent and the legacy one is preserved for back-compat.
- Bearer-path validation: token must exist, not be revoked / expired,
  and carry the `webhook:in` scope. Mismatches return 401 (bad token)
  or 403 (wrong scope) with descriptive bodies that name the token
  and list its granted scopes.
- Audit attribution: `triggered_by` records
  `api:servicenow_webhook (webhook:token:<name>)` for the bearer path
  and `api:servicenow_webhook (webhook:hmac)` for the legacy path —
  so revocation events in the audit log can be tied to specific
  integrations.
- `last_used_at` on the api_token row updates on every successful
  webhook delivery (same path as admin endpoints).
- Verified end-to-end: no auth / bogus bearer / wrong-scope / valid-bearer
  / valid-HMAC all return correct status codes and create or reject
  orders consistently. Audit log shows correct attribution per path.

**Done — Audit log viewer UI (2026-04-26):**
- New `/ui/audit-log` page rendering the existing `/admin/audit-log`
  JSON endpoint. Filter bar with entity-type dropdown, entity ID,
  triggered-by substring search, and from/until timestamp.
- Coloured actor badges: blue for `token:*`, green for
  `admin:session:*`, amber for `admin:legacy_key`, purple for
  `webhook:*`. Makes it instantly obvious whether a change was
  driven by an integration, an admin in the UI, or a fallback path.
- Expandable rows: each entry shows a one-line summary; expanding
  reveals the JSON `before` / `after` diff for the change.
- Pagination (50 per page, "Newer" / "Older" buttons) plus a
  "Reset" filter button.
- Nav entry between API Tokens and License.
- Verified: filtering for `triggered_by=token:` and
  `triggered_by=legacy_key` returns the expected subsets from the
  ~800 audit rows currently in the dev DB.

**Done — audit attribution everywhere (2026-04-26):**
- New `actor_by(request, label)` helper in `app.utils.audit` builds the
  `triggered_by` string from `request.state.actor` (set by
  `require_admin_key` / `_authenticate_webhook`). Falls back to plain
  `api:<label>` when no actor is on state, so the helper is safe to
  use on unauthenticated routes too.
- Updated all 12 mutating admin route call sites in `admin.py` to
  thread `request: Request` and use the helper:
  `create/update/delete config`, `create/update/clone/delete asset_type`,
  `create/update/delete asset`, `force_delete_asset`, `revoke_asset`.
- Webhook path already used an equivalent format; left as-is.
- Verified end-to-end: token-driven `PUT /admin/config/...` produced
  `api:update_config (token:audit-attrib-test)`; legacy-key write to
  the same endpoint produced `api:update_config (admin:legacy_key)`.
  An auditor can now trace every change back to the specific
  credential (token name, admin session user, or legacy key).

**Still to do — separate slices:**
- [ ] Wider scope rollout to the rest of the `/admin/*` surface
      (mechanical, decorator-only).
- [ ] Audit attribution on `/orders` API and portal-side flows
      (today they use hardcoded labels because there's no shared
      auth context — would need a portal-user actor wrapper).
- [ ] Optional: hard-delete vs. soft-delete policy (today everything
      is soft-deleted; some tenants will want a "purge revoked tokens
      older than 90 days" Beat task).

### [partial] Tamper-evident audit + SIEM export — Prio 0
SIEM streaming side **shipped 2026-04-26** (Splunk HEC adapter). Tamper-
evident DB-grant revocation on `audit_log` is split into a separate slice
because it touches role grants on a live table and is best paired with the
RBAC work.

**Done — SIEM streaming (2026-04-26):**
- Worker module `worker/tasks/modules/siem_export.py` — Splunk HEC
  payload builder + POST sender, stdlib `urllib`, no external deps,
  graceful TLS-verify toggle for self-signed labs.
- Beat task `worker/tasks/workflows/siem_streamer.py` — runs every
  minute, fetches `audit_log WHERE id > :last LIMIT :batch_size`,
  POSTs in HEC format, advances `siem.last_id` only on 2xx.
- Cursor + observability state stored in `app_config`:
  `siem.last_id`, `siem.last_error`, `siem.last_success_at`.
- Migration `0053_seed_siem_config.py` seeds 9 `siem.*` keys.
- API endpoints `POST /admin/config/siem/test` + `GET /admin/config/siem/status`.
- Admin UI: new **Compliance** tab in Settings with mode, endpoint,
  HEC token, batch size, TLS verify, Save / Send Test / Refresh
  Status buttons, plus a live status panel showing cursor / backlog /
  last error / last success.
- README + `docs/ENTERPRISE_FEATURES.md` updated with Splunk HEC
  setup walkthrough.
- Verified end-to-end: connection-refused returns graceful failure,
  cursor doesn't advance on failure, status surface reflects errors,
  payload preview matches HEC's expected newline-delimited JSON
  format with `event` / `sourcetype` / `host` / `time` envelope.

**Still to do — separate slice:**
- [ ] Migration: revoke DELETE/UPDATE on `audit_log` from the app DB role.
      Best done together with admin RBAC (a real `auditor` role needs
      `audit_log` SELECT but never write).
- [ ] Microsoft Sentinel adapter (HEC-compatible — small `build_sentinel_payload` + `post_sentinel`).
- [ ] Generic webhook adapter with HMAC signing for arbitrary SIEMs.
- [ ] Streaming-failure email alert via the existing health-alert path
      (currently surfaced only in `siem.last_error` and the UI).

### [open] Multi-instance HA — Prio 0 (show-stopper)
Single api / single worker / single Postgres / single Beat. Beat especially
is a SPOF — default Celery Beat doesn't lock, two beats = duplicate
dispatches. Need documented multi-replica deployment.
- [ ] Replace default Celery Beat with `celery-singleton` or Redis-locked beat
- [ ] Document Postgres standby setup (logical replication or pgBackRest)
- [ ] Multi-replica api: ensure session storage is Redis-backed (currently
      cookie-signed — already stateless, just verify)
- [ ] Multi-replica worker: prefork already works, just document scaling
- [ ] Health probe that detects "Beat is alive somewhere" via Redis heartbeat

---

## Differentiators (Prio 1) — table-stakes for upper-mid market

### [open] Access certification campaigns — Prio 1
Quarterly "managers must re-confirm their team's access" workflow with email
reminders, escalation, auto-revoke on no-response. Hard requirement for ISO27001 / SOX / PCI audits.
- [ ] `certification_campaigns` table (created_at, scope, due_at, status)
- [ ] Beat task: scan active orders matching campaign scope, create review tasks
- [ ] Manager portal page: list pending reviews with one-click confirm/revoke
- [ ] Email reminders T-7d / T-1d / overdue; escalation to manager's manager

### [partial] Approval-flow sophistication — Prio 1
Reminder slice **shipped 2026-04-26**. The bigger pieces (escalation,
delegation, N-of-M, conditional rules) remain.

**Done — approval reminders (2026-04-26):**
- Migration `0055_approval_reminders.py` — `last_reminded_at` +
  `reminder_count` on `order_approvals`, plus three `approval.*`
  config keys.
- Beat task `tasks.workflows.approval_reminders.scan_and_remind` runs
  hourly (`crontab(minute=15)` to spread load away from other Beat
  tasks). Picks pending approvals where
  `COALESCE(last_reminded_at, created_at) < NOW() - reminder_after_hours`
  and `reminder_count < max_reminders`.
- Refactored `dynamic_runner.send_approval_requests` per-approval
  block into a shared helper `deliver_approval_notification()` so
  both initial dispatch and reminders use the same email + Teams
  card path. Reminders bump the card title to "Reminder (n): …" so
  recipients can tell it's a nudge.
- Config: `approval.reminders_enabled` (default true),
  `approval.reminder_after_hours` (default 24),
  `approval.max_reminders` (default 3).
- Settings UI: new "Approval Reminders" section in the E-Mail tab
  with status / hours / cap inputs.
- Verified end-to-end: synthetic 48-hour-old approval picked up,
  Teams card delivered to live workflow webhook, reminder counter
  advanced, second run within cutoff window correctly skipped,
  disabled mode skips silently.

**Done — approval delegation / OOO routing (2026-04-26):**
- Migration `0058_approval_delegations.py` — `approval_delegations`
  table with approver/delegate emails+names, from/until window,
  reason, created_by, revoked_at, plus a covering index
  `(approver_email, from_at, until_at)` so the lookup on every
  order-creation is single-index-scan cheap.
- ORM `app.models.approval_delegation.ApprovalDelegation` with a
  CHECK constraint guaranteeing `until_at > from_at`.
- Resolver `app.utils.approval_delegation.resolve_active_delegate`
  finds the most-recent matching active delegation for an email
  (case-insensitive), filters out revoked rows and rows whose
  window doesn't cover NOW(), returns the row or `None`.
- Wired into both portal flows that create approval rows: the
  initial order (`portal_create_order`) and the
  modify/extend re-approval (`portal_modify_order`). Each
  call site uses an inline `_make_approval` helper that checks
  for an active delegation and routes to the deputy when one
  matches; logged at INFO so re-routes are visible in worker logs.
- Admin endpoints `GET/POST/DELETE /admin/approval-delegations`
  with `approvals:read` / `approvals:write` scope gates.
  Validation: 422 when `until_at <= from_at` or when delegate
  email equals approver email.
- Admin UI page `/ui/approval-delegations` with create modal
  (defaults to "tomorrow 09:00 → 14 days later 17:00" in local
  TZ), per-row revoke button, status badges
  (active / scheduled / expired / revoked).
- Audit log captures every create/revoke with `actor_by()` so
  the audit trail names which credential set up the delegation.
- Verified end-to-end: resolver returns the right delegate
  inside the FastAPI process; standalone-Python invocation
  hits a known mapper-init quirk that doesn't affect the
  actual request path.

**Done — approval escalation (2026-04-26):**
- Migration `0059_approval_escalation.py` — adds
  `order_approvals.escalated_at` column, seeds the
  `approval.escalation_email` config key (default empty),
  seeds a new `approval_escalated` email template with full
  variable set (original approver name+email, requester, asset,
  reminder_count, etc.).
- ORM `OrderApproval.escalated_at` mapped.
- `notif.send_approval_escalated()` — new notifications path that
  loads the seeded template, renders branded HTML, sends to a list
  of escalation addresses. Returns silently when no addresses are
  configured.
- `scan_and_remind` Beat task now does both jobs in a single tick:
  reminders for rows below the cap, escalations for rows at or above
  it. The escalation query filters `escalated_at IS NULL` so each
  approval escalates **at most once** — subsequent ticks skip it.
- Settings UI (E-Mail tab → Approval Reminders): new
  "Escalation contact(s)" field accepting comma-separated emails.
  Helper text explains the once-per-approval semantics.
- Verified end-to-end: synthetic approval at `reminder_count=3` with
  `escalated_at=NULL` → first scan returns
  `reminded: 0, escalated: 1`, sets `escalated_at`; second scan
  correctly skips it (`escalated: 0`).

**Still to do:**
- [ ] Schema: `approval_rules` JSONB on asset_type extending current `approval_owners`
- [ ] Runtime evaluator that resolves rules → approval steps
- [ ] UI: rule-builder (avoid full DSL; predefined patterns)
- [ ] Escalation v2: optionally **assign** the escalated approval
      to the contact (creating a new approval row with their email)
      so they can decide directly via the existing token URL —
      currently they get a heads-up only and have to intervene
      operationally.
**Done — self-service portal delegation (2026-04-26):**
- New router `app.routes.portal_delegations` exposes
  `GET /portal/delegations` (HTML page),
  `GET /portal/api/delegations` (list mine),
  `POST /portal/api/delegations` (create mine),
  `DELETE /portal/api/delegations/{id}` (revoke mine).
- Identity enforcement: every write coerces ``approver_email`` to
  the SSO-authenticated user's email. A portal user **cannot**
  re-route another user's approvals even by tampering with the
  payload. Cross-user revoke attempts return 404 (not 403) so we
  don't leak delegation existence.
- Anonymous mode (`entra.mode = disabled`) returns 403 from the
  write endpoints — no real identity to delegate from.
- Portal sidebar: new "Delegations" entry under "My Approvals",
  visible only when the user has had at least one approval
  (matching the existing `has_any_approvals` gate).
- New template `portal/delegations.html` mirrors the admin page UX
  but pre-fills the approver-email server-side and offers no
  ability to manage other users' rows.
- Audit trail: each portal-driven create / revoke records
  ``portal:<email>`` as ``triggered_by``.
- i18n complete: 21 new keys added to all 5 locales (en/de/fr/es/it),
  validator confirms 167 keys per locale.
- Verified: routes return 302 (portal-login redirect) without a
  session; 167 i18n keys present in every locale.
- [ ] Auto-decline policy after extended inactivity (opt-in)

### [open] HR feed + SCIM — Prio 1
Auto-deprovision on `LeaverEvent` from Workday/SAP HR; SCIM in/out so Okta /
Ping / SailPoint can drive ipSolis as an authoritative target.
- [ ] SCIM 2.0 endpoint (`/scim/v2/Users`, `/scim/v2/Groups`)
- [ ] HR webhook receiver with vendor-specific adapters
- [ ] Leaver flow: revoke all active orders for the user, audit

### [done] Observability — Prometheus `/metrics` — Prio 1 (2026-04-26)
Standard Prometheus text-format endpoint at `/metrics`. OpenTelemetry tracing
deferred to a separate slice (different dep tree, optional).
- HTTP request count + latency histogram per route template, labelled by
  method / route / status class. Path templates (`/orders/{order_id}`) are
  used so cardinality stays bounded; static / locale paths are bucketed
  to `/static/*` and `/locales/*` so per-file lookups don't blow it up.
- Business gauges refreshed on each scrape (cheap indexed `count GROUP BY`):
  - `ipsolis_orders_in_status{status}` — orders by lifecycle status
  - `ipsolis_approvals_pending` — pending approval rows
  - `ipsolis_pool_assets{asset_type, status}` — pool size per definition
- `metrics.enabled = false` flips the endpoint to 404 — toggle in
  `app_config`. No built-in auth on the endpoint; restrict via reverse
  proxy if exposed beyond the cluster perimeter.
- New module: `api/app/utils/metrics.py` (CollectorRegistry, gauge
  refresher, route-label helpers).
- New route: `api/app/routes/metrics.py`.
- Middleware: `record_request_metrics` in `main.py` records duration via
  `time.perf_counter()` after the response is built. `/metrics` itself
  doesn't count toward the request rate.
- Migration: `0052_seed_metrics_config.py` seeds `metrics.enabled = true`.
- Dep added: `prometheus-client==0.21.0`.
- Verified: real data from a running instance — orders/approvals/pool
  gauges populate correctly; disable toggle returns 404; re-enable
  returns 200; histograms have non-zero values for sample requests.

### [partial] Observability — OpenTelemetry tracing — Prio 1
API-side auto-instrumentation **shipped 2026-04-26**. Celery worker
instrumentation, sample dashboards, and the queue-depth gauge remain.

**Done — API tracing (2026-04-26):**
- Migration `0060_seed_otel_config.py` seeds 5 `otel.*` config keys
  (`enabled`, `service_name`, `endpoint`, `headers`, `console_exporter`).
- `app.utils.tracing.setup_tracing()` reads the config and configures
  the global `TracerProvider`. Two exporter modes that compose:
  OTLP HTTP (production target — Jaeger / Tempo / SigNoz / Honeycomb /
  any OTel collector) and a console exporter for local verification.
- `app.utils.tracing.instrument_app(app, engine)` wires
  `FastAPIInstrumentor` and `SQLAlchemyInstrumentor` after the provider
  is installed; both auto-emit spans for HTTP requests and DB queries.
- Lifespan hook in `main.py` reads `otel.*` from `app_config` at
  startup, calls setup + instrument, logs whether tracing is active.
- New deps: `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-http`,
  `opentelemetry-instrumentation-fastapi`,
  `opentelemetry-instrumentation-sqlalchemy` — all pinned to 1.29.0
  / 0.50b0. HTTP exporter chosen over gRPC to avoid the grpcio
  compile dependency.
- Settings UI: new "OpenTelemetry Tracing" card in the Compliance tab
  with status / service-name / endpoint / headers (secret) / console
  exporter checkbox. Save handler `PUT`s every key; restart message
  reminds operator that tracing wires up at API startup.
- Verified end-to-end: enabled tracing + console exporter → restart
  → confirmed real spans emitted to API stdout including FastAPI
  request kind and SQLAlchemy DB query kind with correct resource
  attributes (service.name, service.version).

**Done — Celery worker tracing (2026-04-26):**
- New `worker/tasks/tracing.py` mirrors the api's setup module —
  reads `otel.*` from `app_config` via a one-shot psycopg2 query,
  configures `TracerProvider`, hooks `CeleryInstrumentor` and
  `SQLAlchemyInstrumentor`. Pinned to the same OTel version (1.29.0
  / 0.50b0) as the api so propagated trace context parses identically
  on both sides of the Celery boundary.
- Worker service name auto-derived as ``ipsolis-worker`` (or
  ``<custom>-worker`` when admin set a custom service name) so
  trace UIs show distinct services for api vs. worker.
- Setup invoked at module-import time in
  `worker/tasks/__init__.py`, before workers fork — required for
  the Celery instrumentor to hook signals correctly.
- Bug fixed in `_load_otel_config_sync()`: now strips both
  `postgresql+asyncpg://` (api) and `postgresql+psycopg2://`
  (worker) URL prefixes before handing the DSN to psycopg2.
- New deps in `worker/requirements.txt`:
  `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-http`,
  `opentelemetry-instrumentation-celery`,
  `opentelemetry-instrumentation-sqlalchemy`.
- Verified end-to-end: enabled tracing + console exporter →
  restarted worker → triggered a task → confirmed spans emitted
  with `service.name: ipsolis-worker` (distinct from
  `service.name: ipsolis-api`). When both sides run with the same
  OTLP collector, an http-dispatched runbook produces a single
  distributed trace.

**Still to do:**
- [ ] `ipsolis_celery_queue_depth{queue}` Prometheus gauge (needs
      Redis LLEN per queue; orthogonal to tracing)
- [ ] Sample Grafana dashboards: provisioning latency p50/p95,
      queue depth, error rate

### [partial] Cost / chargeback per asset type — Prio 1
Reporting side **shipped 2026-04-26**. Per-order cost projection on the
order detail page and threshold-based alerts deferred.

**Done — schema + report (2026-04-26):**
- Migration `0056_asset_type_cost.py` — adds `monthly_cost NUMERIC(12,2)`,
  `currency VARCHAR(3)`, `cost_center VARCHAR(100)` (all nullable so
  legacy definitions stay untracked).
- ORM `AssetType.monthly_cost / currency / cost_center`.
- Pydantic schemas (Create / Update / Read) carry the new fields.
- Admin form: new "Cost & Chargeback" section between Classification
  and Lifecycle, with monthly cost input, currency dropdown
  (EUR/USD/GBP/CHF/JPY/CAD/AUD/SEK/DKK/NOK/PLN), and cost-center text.
  Section nav updated to include the new anchor.
- Admin route: `GET /admin/cost-report?fmt=json|csv` — aggregates
  active orders (same status set as capacity enforcement) per
  (cost_center × asset_type × currency), returns rows + per-cost-center
  totals. CSV export with `Content-Disposition: attachment`.
- Admin UI: new `/ui/cost-report` page with summary cards + detail
  table + CSV download. Linked from the left nav between Maintenance
  and License.
- Audit snapshot updated to capture cost field changes.
- README + `docs/ENTERPRISE_FEATURES.md` updated with the field
  definitions and report behaviour.
- Verified end-to-end: empty report when no definitions priced,
  correct active-order counts and projected totals after seeding
  two test definitions, CSV export with right content-type and
  attachment filename.

**Done — AD-driven consumer breakdown (2026-04-26):**
- Migration `0057_order_requester_attributes.py` — six new columns on
  `orders` (`requester_sam_account`, `requester_department`,
  `requester_cost_center`, `requester_company`, `requester_employee_id`,
  `requester_title`) plus five `ad.attribute.*` config keys with
  sensible defaults.
- `ad_lookup._msldap_lookup` extended to fetch the configured HR
  attributes alongside identity, with empty-mapping entries filtered
  out so we don't ask AD for a literal "" attribute.
- `portal.portal_create_order` snapshots the AD attributes onto the
  Order row on creation (best-effort — AD outage doesn't block the
  order). `MODIFY` and `DELETE` orders inherit the snapshot from the
  original provision order so chargeback stays internally consistent.
- Cost report rewritten to query active orders directly, exposing
  three aggregation views via JSON: provider (asset cost_center),
  consumer (requester cost_center), department (requester department).
  Untracked but priced asset definitions still surface as 0-row
  entries in the provider view so admins can spot misconfigured
  ones.
- CSV export switched to per-order detail (18 columns) — order id,
  status, dates, full requester identity (email, name, sAMAccount,
  employee id, title, department, cost center, company), asset type,
  provider cost center, currency, unit cost, monthly total.
- Cost report UI: three view tabs (By provider / By consumer cost
  center / By department), summary cards swap based on selected
  view, detail table only renders for the provider view.
- Settings → Active Directory: new "AD Attribute Mapping
  (Chargeback)" card with 5 inputs (department / cost center /
  company / employee ID / title). Save handler `PUT`s each key.
- Verified end-to-end: AD attrs populated on a real provisioned
  order, JSON shows by_consumer_cost_center and by_consumer_department
  populated correctly, CSV carries all requester fields per order.

**Still to do:**
- [ ] Per-order cost projection on the portal order detail page
      (`monthly_cost × months_requested`).
- [ ] Threshold alerting — email/Teams when projected monthly per
      cost center crosses a configurable amount.
- [ ] Historical view: report by date range, not just "currently
      active" (would need a snapshot table or order-status time series).
- [ ] FX conversion for mixed-currency cost centers (today the
      summary cards keep currencies separate).
- [ ] Webhook / `/orders` API: also do AD lookup for non-portal
      order creation paths so externally-driven orders get the same
      snapshot.

---

## Polish & smaller gaps (Prio 2)

### [done] `max_per_user` for pooled types — Prio 2 (2026-04-25)
Per-user quota now enforced everywhere a PROVISION order can be created
(public API, ServiceNow webhook, self-service portal). Quota covers personal
and pooled assignment models; `dedicated_shared` is exempt because everyone
shares a single instance.
- UI: `max_per_user` input lifted out of the personal-only section in the
  asset-definition form; visible for `assigned_personal` + `capacity_pooled`,
  hidden only for `dedicated_shared`. Helper text explains the active-status
  set the count is taken over.
- Runtime: new `enforce_max_per_user()` in `api/app/utils/capacity.py`
  returns HTTP 409 with a descriptive detail when the user is at the limit.
- Wired into `api/app/routes/orders.py` (after `enforce_pool_capacity`),
  `api/app/routes/webhook.py` (ServiceNow path), and
  `api/app/routes/portal.py` (renders error inline via `_render_error`).
- Bonus correctness fix: `_ACTIVE_STATUSES` in `capacity.py` now includes
  `PENDING_APPROVAL` and `SCHEDULED` — closes a hole that let scheduled and
  approval-pending orders bypass both pool capacity *and* the per-user quota.
- Counting uses case-insensitive `user_email` match so Outlook-style casing
  variants don't yield a fresh slot.

### [done] `is_active` flag on asset definitions — Prio 2 (2026-04-25)
Admins can now deprecate without delete. Inactive types are hidden from the
portal catalog (`/portal/orders/new`) but stay visible in the admin list with
an "Inactive" badge so historical orders, audit, and runbook configs stay coherent.
- Migration `0049_asset_type_is_active.py` — adds `is_active BOOLEAN NOT NULL DEFAULT true` column.
- ORM `AssetType.is_active` (`api/app/models/asset.py`).
- Pydantic `AssetTypeCreate` / `AssetTypeUpdate` / `AssetTypeRead` carry `is_active`.
- Admin route POST/PUT/clone honor the field; clone preserves the source's flag.
- Audit snapshot `_type_snap()` includes `is_active` so deprecation events are diffable.
- Form: new "Active" checkbox with explainer in the Identity section, default-checked.
- List: "Inactive" badge + 60% row opacity on deprecated rows.
- Portal: catalog list / re-render error path filter `WHERE is_active = true`.
- Verified end-to-end: PUT `is_active=false` removes from catalog, admin list keeps it with badge.

### [done] Long-form `help_text` per asset definition (markdown) — Prio 2 (2026-04-25)
Admins can now write a multi-paragraph note in markdown that requesters see
when they pick the type on `/portal/orders/new` — separate from the one-line
catalog description. Used for things requesters need *before* ordering:
pre-installed software, expected provision time, support contact, license terms.
- Migration `0050_asset_type_help_text.py` — adds `help_text TEXT` column.
- ORM `AssetType.help_text` (`api/app/models/asset.py`).
- Pydantic Create/Update/Read schemas carry `help_text`.
- Admin form: new textarea below Description in the Identity section, with a
  helper line listing supported markdown features. JSON payload includes
  `help_text` on both create and update; clone preserves it.
- Audit `_type_snap()` includes `help_text` so revisions show up in the audit log.
- Rendering: `api/app/utils/markdown_render.py` uses python-markdown +
  bleach with a strict allowlist (`p, br, strong, em, code, pre, blockquote,
  ul, ol, li, h1-h6, a, hr`; `a` only keeps `href`/`title`; protocols
  limited to `http/https/mailto`). Linkified hrefs auto-set
  `target="_blank" rel="noopener noreferrer"`.
- Filter registered as `| markdown` on the shared Jinja env in
  `templates_instance.py`. Used in `portal/order_new.html` via
  `{{ t.help_text | markdown | safe }}`.
- Portal: per-type panel that toggles when the asset is selected — same
  pattern as the attribute section. Hidden when the selected type has no help.
- Styling: hand-tuned CSS scoped to `.help-md` (paragraphs, headings,
  lists, code, blockquote) — Tailwind via CDN doesn't ship the typography
  plugin, so we don't rely on `prose-*` classes.
- New deps: `markdown==3.7`, `bleach==6.2.0` in `api/requirements.txt`.
- Verified: XSS attempts (`<script>`, `<img onerror>`, `javascript:` href)
  are stripped by the bleach pass; round-trip via direct SQL update + render.

### [done] Microsoft Teams approval cards — Prio 2 (2026-04-25)
Approvers now receive an Adaptive Card in Teams alongside the email when a
request needs sign-off. The card has a single "Review request →" button
that opens a tokenized confirmation page with no portal login required.
Slack adapter is deferred — same token + endpoint is reusable when needed.

**Architecture**: Microsoft Teams **Workflows** webhook (no Azure Bot
registration, no Graph permissions). Admin creates a Workflow once per
target channel/chat with the template "Post to a channel when a webhook
request is received", pastes the URL into Settings → E-Mail → Microsoft
Teams. Card delivery is done by the worker, best-effort, never blocks the
order on Teams misconfiguration.

**Why not bot/GET-auto-approve**: GET-based one-click would let Outlook /
Teams link previewers prefetch and accidentally approve. Bot Framework
needs a publicly reachable bot endpoint and Microsoft App ID/Secret —
overkill for the value delta over the link-to-confirmation-page UX.

**Components**:
- `api/app/utils/approval_token.py` — HMAC-SHA256 stateless token, signed
  with `API_SECRET_KEY` (rotating that env var invalidates all outstanding
  links — usually the right thing on incident response). 14-day TTL.
- `api/app/utils/approval_decision.py` — shared decision-recording helper;
  portal route and tokenized route both call it so the two paths can never
  drift on what counts as "approved" or how downstream effects fire.
- `api/app/routes/approvals_external.py` — `GET /approve/{token}` renders
  the confirmation page; `POST /approve/{token}` records the decision.
  No portal session required. Status pages for already-decided / expired /
  invalid token cases.
- `api/app/templates/approve_confirm.html` + `approve_status.html` —
  standalone branded pages, dark-mode aware, work without Entra SSO.
- `api/app/utils/teams_notify.py` + `worker/tasks/modules/teams_notify.py` —
  the worker copy duplicates the token signer + card builder verbatim
  (separate Docker images, no cross-image imports). Cross-verified that a
  token minted in the worker validates on the API endpoint.
- `worker/tasks/workflows/dynamic_runner.py` — `send_approval_requests`
  posts the card after sending the email when `teams.mode = enabled` and
  `teams.webhook_url` is set. Failures are logged at WARNING and don't
  abort the email loop.
- `api/alembic/versions/0051_seed_teams_config.py` — seeds `teams.mode`
  (default `disabled`) and `teams.webhook_url` (`is_secret=true`).
- `api/app/routes/admin.py` — `POST /admin/config/teams/test` posts a
  test card to the configured webhook so admins can verify the workflow
  end-to-end before enabling.
- `api/app/templates/ui/settings.html` — new "Microsoft Teams — Approval
  Cards" section in the E-Mail tab with Mode dropdown, Webhook URL field,
  Save + Send Test Card buttons, and a setup hint.
- `api/app/routes/portal.py` — refactored `portal_decide_approval` to
  delegate to the shared helper (40 lines deleted, 2 added).
- `api/app/main.py` — registers the new router.

**Verified**:
- Token round-trip works in both directions; tampered/expired/garbage
  tokens all reject cleanly.
- API endpoint serves 200 for valid pending approval, 200 for already-
  decided, 404 for missing-approval-row (cleanup / cascade), 410 for
  invalid/expired token. Each path renders its own status page so the
  approver can tell what happened.
- Test endpoint returns descriptive error for missing/disabled config and
  network errors (no 500s on misconfiguration).
- Worker can import the mirror module; cross-verified token validates
  on the API side (shared `API_SECRET_KEY` from `.env`).
- Adaptive Card includes a Teams `@mention` (`msteams.entities`) of the
  approver — verified live, fires a Windows system-tray banner on the
  approver's client. Approver's display name is also used as the
  `<at>...</at>` placeholder so the body renders gracefully even when
  a Workflow template strips entities.

### [partial] Field-level data classification — Prio 3
Slice 1 — schema (in JSON), admin UI tagging, portal badges, audit
trail capture — **shipped 2026-04-26**. Approval routing and
retention-policy enforcement remain.

**Done — classification tagging (2026-04-26):**
- `asset_types.config` per-attribute JSON gains a new optional
  `classification` field. Allowed values: `""` (public default),
  `internal`, `pii`, `phi`, `pci`. No DB migration needed — the
  column was already JSON.
- Admin form (`asset_type_form.html`): each attribute row now has
  a "Classification" sub-row with a 5-option dropdown and an
  inline hint. Both the existing-data branch and the addAttrRow JS
  factory include the field. The submit serializer reads it and
  attaches `classification` to the JSON entry only when set.
- Admin list (`asset_types.html`): each rendered attribute key is
  followed by a small classification badge — amber for PII, red for
  PHI/PCI, neutral for `internal`. Public attributes stay
  badge-free.
- Portal (`order_new.html`): attribute labels render a matching
  badge with a tooltip explaining the classification when the
  requester is filling in the form. PII shows a shield icon and
  amber badge; PHI/PCI show red badges with cross/card icons.
- Audit log automatically captures the classification because
  `_type_snap()` already serialises `t.config` verbatim — every
  asset-type create/update/clone audit row carries the per-attribute
  classification.
- Verified end-to-end: tagged `manager_email` as PII and
  `cost_center` as internal on a real asset definition, confirmed
  admin list renders the badges and JSON persists correctly.

**Still to do:**
- [ ] Approval routing: orders containing PII/PHI/PCI fields
      automatically include an extra approval step (e.g. compliance
      officer) — needs the approval-rules schema.
- [ ] Audit retention: separate retention windows per
      classification (e.g. PHI = 7 years, public = 90 days);
      requires a Beat task that prunes old rows by joining their
      asset-type config.
- [ ] Settings: per-classification policy switches (e.g. "PII fields
      always trigger manager approval", "PHI requires owner-of-record
      acknowledgement").

### [done] Catalog search & filter in the portal — Prio 3 (2026-04-25)
Pure client-side filter on `/portal/orders/new`: a search input matches
against name + description + help_text (lowercased), and a category dropdown
narrows by the existing `AssetCategory` enum. The controls auto-hide when
there are six or fewer definitions to avoid clutter on small catalogs.
- Server pre-renders `data-search` and `data-category` on every card so
  filtering is one DOM pass — no fetches, no extra round-trip.
- "No definitions match" empty state replaces the grid when nothing matches.
- "Clear" link appears once any filter is set, resets both controls.
- If the user already had a card selected and the new filter hides it,
  the selection is cleared and the help / attribute / user-list panels reset
  so a stale `asset_type_id` can't be submitted.
- i18n: 9 new keys (`catalog_search_placeholder`, `catalog_filter_all`,
  five `catalog_filter_*` category labels, `catalog_no_match`,
  `catalog_clear_filters`) added across all five locales (en/de/fr/es/it).
  `tools/validate_locales.py` confirms 143 keys per locale, all aligned.

### [done] Dashboard pool-capacity warnings — Prio 3 (2026-04-26)
A capacity-pressure band that auto-renders above the status tiles when
any active asset pool is at ≥80% fill, listed by severity with direct
links to the affected definition / pool view. Surfaces capacity
problems before users hit a 409 from the existing per-pool quota
enforcement.

- New `_pool_warnings(db)` helper in `api/app/routes/ui.py` —
  computes per-asset-type fill in two batched queries (one for
  active orders on capacity_pooled types, one for AssetPool grouped
  by `(asset_type_id, status)`); no N+1 regardless of catalog size.
- `assigned_personal` / `dedicated_shared` types: anything not in
  `Free` status counts as a consuming slot — busy, reserved,
  maintenance, Failed, Reinstall all keep the row from satisfying
  a new request, so the operator sees real availability pressure.
- `capacity_pooled` types: count active orders against
  `pool_capacity` using the same status set as quota enforcement.
- Severity: ≥80% → `warning` (amber), ≥95% → `critical` (red).
  Banner copy adapts: "N pools at critical capacity, M approaching",
  "N pools at critical capacity", or "M pools approaching capacity".
- Each warning row is a clickable link — `pooled` → asset-definition
  edit page (where capacity is configured); `personal/shared` →
  the asset-pool list filtered to that type.
- Inactive asset definitions are excluded — they can't accept new
  orders so flagging them as "full" is noise.
- Renders inside the existing `fragments/pool_summary.html` so it
  participates in the existing dashboard auto-refresh path; no
  schema or migration needed.
- Verified: a real pool with 2/2 personal VDIs renders as
  `1 pool at critical capacity · Personal VDI Host · 100% (2/2)`
  in red/critical styling.

### [done] In-app setup checklist — Prio 3 (2026-04-26)
Replaced the originally-planned "guided tour" with a persistent setup
checklist on the dashboard — more useful for both first-run setup
*and* ongoing operational health checks (e.g. someone deletes the only
asset definition → the relevant item flips back to ☐). No external JS
library; pure server-side detection from current DB state.

- New endpoint `GET /admin/setup/state` returns 9 checklist items
  (6 essential, 3 recommended) with `done` / `label` / `hint` / `link` /
  `tier` per item plus per-tier and overall summaries.
- Items: app branding, SMTP, AD, Entra ID, asset definitions exist,
  asset pool has assets, Teams card delivery, SIEM streaming,
  per-integration API token issued.
- Dashboard card with circular progress ring and percent badge,
  auto-expands when there's anything incomplete, collapses when
  everything is green.
- "Hide until next setup change" persists a signature of the current
  done-state in `localStorage`. If the state later changes (regression
  or new config), the signature mismatch re-shows the card.
- Each pending row is a direct link to the relevant settings tab
  anchor (e.g. `/ui/settings#ad`) so admins skip the navigation step.

---

## Pre-existing open tasks

### [open] Entra ID Connect / Cloud Sync setup — infrastructure (no code change needed)
Sync `xenpool.local` on-prem users to the Entra ID tenant so they can use portal SSO with
their existing domain credentials. Pure Windows Server / Azure infrastructure task.
- [ ] Install Entra ID Connect (or Entra Cloud Sync agent) on a domain-joined server
- [ ] Configure UPN suffix (`xenpool.de`) for synced accounts
- [ ] Verify synced users can log into the portal (no code change required)

### [open] Cloud group management via Microsoft Graph — future
Extend `target_executor` to manage Entra cloud-only security groups for asset types
that define `{"type": "entra_group", "group_id": "..."}` targets. Requires
Microsoft Graph API integration (separate sprint).

---

## Done

### [done] Portal Authentication — Entra ID SSO (2026-03-23)
- `msal` added to `api/requirements.txt`
- `SessionMiddleware` added to `main.py` (signed cookie, 8h TTL)
- `api/app/utils/entra.py` — MSAL helper (auth URL, token exchange, domain check)
- `api/app/routes/auth.py` — `/portal/login`, `/portal/auth/callback`, `/portal/logout`
- `api/app/routes/portal.py` — `require_portal_auth` dependency on all routes; when `entra.mode = disabled` the portal is open with a shared anonymous identity
- `base_portal.html` — user name chip + Sign out link in nav bar
- `portal/auth_error.html` — error page for login failures
- `api/app/templates/ui/settings.html` — "Entra ID / Azure AD" section in Identity & Directory tab
- `POST /admin/config/entra/test` — verifies credentials via client-credentials token flow
- Migration 0019 — seeds 6 `entra.*` config keys (`entra.mode` defaults to `disabled`)

### [done] Beat-Scheduler → migrate to dynamic_runner (2026-03-23)
- `check_expiring_assets` now creates a `delete` order per expired asset
  (copies `provisioned_state` from the provision order for deterministic revoke)
  and dispatches `dynamic_runner.run` instead of the hardcoded `vdi_reclaim.run`
- Original provision order is immediately set to `expired`; the new delete
  order progresses through `dynamic_runner` with the asset type's configured
  runbook/strategy
- Note: a `delete` runbook must be configured per asset type in the Admin UI
  for `runbook_only` / `composite` asset types; `group_only` types work without

### [done] Legacy Workflow Cleanup — Prio 1b (2026-03-23)
- `check_expiring_assets` moved into `dynamic_runner.py`; beat_schedule updated
- Deleted: `vdi_provision.py`, `vdi_modify.py`, `vdi_reclaim.py`
- Removed from `__init__.py`: legacy includes + task_routes entries

### [done] Basic Tests (Happy Path) — Prio 3 (2026-03-24)
- `pytest>=8.0.0` + `pytest-asyncio` added to `api/requirements.txt`
- `api/tests/conftest.py` — adds `worker/` to sys.path
- `api/tests/test_happy_path.py` — 14 tests, 31 total passing
- `docker-compose.yml`: added `./api/tests` and `./worker` volume mounts
- Run: `docker compose exec api python -m pytest tests/ -v`

### [done] SCCM VDI Group Configuration Script (2026-03-23)
- `scripts/sccm/Configure-VDI-Groups.ps1` — executed during SCCM Task Sequence setup
- Creates RDP/ADM groups in `OU=VDI,OU=XenPool GmbH,DC=xenpool,DC=local`
- Dual-channel logging: Windows Event Log + `C:\Windows\debug\Configure-VDI-Groups.log`

### [done] XenServer Script Library — VMware conversions (2026-03-16)
- `XenServer - VM reboot or startup (gracefully)` (ID 10)
- `XenServer - VM change boot order (disk-cd-net)` (ID 11) — `hvm_boot_params["order"]="cdn"`
- `XenServer - VM change boot order (net-cd-disk)` (ID 12) — `"ndc"`
- `XenServer - VM shutdown (gracefully)` (ID 13) — CleanShutdown + HardShutdown fallback
- `XenServer - VM stop (force)` (ID 14) — HardShutdown with retry logic

### [done] XCP-ng / XenServer Hosting Infrastructure (2026-03-16)
- Settings page: vSphere + XenServer credential sections
- Migration 0017: seeds `vsphere.*` and `xenserver.*` config keys
- Module editor: auto-injects hosting vars
- `dynamic_runner`: exposes `config.xenserver.*` / `config.vsphere.*`
- PS preamble: SSL cert bypass injected globally

### [done] PS Module Manual Upload — non-Gallery SDKs (2026-03-16)
- Migration 0018: `source_type` + `upload_data BYTEA` columns on `ps_modules`
- API: `POST /admin/ps-modules/{id}/upload`
- Worker: `_install_from_upload()` — extracts zip to `~/.local/share/powershell/Modules/`

### [done] Pool Capacity Enforcement + Display (2026-03-16)
- `api/app/utils/capacity.py`: `enforce_pool_capacity()` — HTTP 409 if pool full
- Orders + webhook routes: pre-flight capacity check for PROVISION actions
- Asset types list: shows `X / Y in use` with color coding for capacity_pooled types
