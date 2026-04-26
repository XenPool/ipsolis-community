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

**Still to do — separate slices:**
- [ ] Scope catalog + per-endpoint scope decorator
      (`orders:read`, `orders:write`, `asset_types:read`, `webhook:in`,
      etc.). The `scopes` column is already JSON-shaped so this lands
      without a migration.
- [ ] ServiceNow webhook secret migration to a bearer token.
- [ ] Audit log enrichment: record `triggered_by` from
      `request.state.actor` so token-driven actions are attributable
      by token name.
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

**Still to do:**
- [ ] Schema: `approval_rules` JSONB on asset_type extending current `approval_owners`
- [ ] Runtime evaluator that resolves rules → approval steps
- [ ] UI: rule-builder (avoid full DSL; predefined patterns)
- [ ] Escalation: after N reminders, notify a configured backup
      approver (e.g. manager's manager, app-owner team distribution
      list) instead of just stopping
- [ ] Delegation: per-user "I'm OOO, route to <user> until <date>"
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

### [open] Observability — OpenTelemetry tracing — Prio 1 (split off)
- [ ] OpenTelemetry tracing with auto-instrumentation for FastAPI, Celery, SQLAlchemy
- [ ] Sample Grafana dashboards: provisioning latency p50/p95, queue depth, error rate
- [ ] `ipsolis_celery_queue_depth{queue}` (needs Redis LLEN per Celery queue)

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

**Still to do:**
- [ ] Per-order cost projection on the portal order detail page
      (`monthly_cost × months_requested`).
- [ ] Threshold alerting — email/Teams when projected monthly per
      cost center crosses a configurable amount.
- [ ] Historical view: report by date range, not just "currently
      active" (would need a snapshot table or order-status time series).
- [ ] FX conversion for mixed-currency cost centers (today the
      summary cards keep currencies separate).

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
  decided, 410 for invalid/expired token.
- Test endpoint returns descriptive error for missing/disabled config and
  network errors (no 500s on misconfiguration).
- Worker can import the mirror module; cross-verified token validates
  on the API side (shared `API_SECRET_KEY` from `.env`).

### [open] Field-level data classification — Prio 3
Tag fields as PII / PHI / PCI; drive approval routing and audit retention.

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

### [open] In-app onboarding / guided tour — Prio 3
First-run admin walkthrough; drop-in for new admins.

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
