# Ipsolis – Task Backlog

Format: `[open]` / `[done]` / `[blocked]`. Add new tasks at the top of their section.
Strategic roadmap up top; smaller polish/gap items in the middle; pre-existing infra
and historical "done" entries at the bottom.

---

## Strategic — Enterprise-class roadmap

These are the gaps that block ipSolis from being drop-in for a 5,000-seat regulated
enterprise. Order = priority (procurement-blocker first).

### [partial] Admin RBAC — Prio 0 (show-stopper)
Slice 1 — per-user accounts, role ladder, first-run setup, role-gated
admin user CRUD — **shipped 2026-04-26**. Comprehensive role-gating
across the rest of `/admin/*`, per-asset-type ACLs, and SoD
enforcement (configurer ≠ approver) split into follow-up slices.

**Done — RBAC slice 1 (2026-04-26):**
- Migration `0069_admin_users_rbac.py` adds `admin_users` (id, username
  unique, password_hash, role, is_active, created_at, updated_at,
  last_login_at, created_by). Username is normalised to lowercase at
  write time so the unique index doesn't need funcidx-LOWER.
- Five-tier role enum in `app.utils.rbac.ROLE_HIERARCHY`:
  `superadmin > admin > approver > auditor > helpdesk`.
  `role_at_least(actual, required)` is the single source of truth for
  privilege comparisons — every other role check delegates to it.
- Password hashing in `app.utils.password` (PBKDF2-HMAC-SHA256, 600k
  iterations per OWASP 2023, stdlib only — no bcrypt/passlib build
  dependency). Self-describing string format
  `pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>` so a future argon2id
  migration is a verify-then-rehash on next login.
- Login flow rewritten in `routes/admin_auth.py`:
  * Form takes username + password.
  * Empty username + password matching `settings.ADMIN_API_KEY` falls
    through as the **legacy back-compat path**: virtual superadmin
    session, attributed as `admin:legacy_key`. Existing scripts and
    bookmarked admin sessions don't break on upgrade.
  * Username + password matched against `admin_users` (active rows
    only). On success, `last_login_at` is updated and the session
    carries `admin_user`, `admin_role`, `admin_via=user`.
  * **First-run setup**: when `admin_users` is empty, the login page
    renders a "Create first administrator" form instead of the
    sign-in form. Submitting it creates the first superadmin and
    auto-logs them in. Idempotent against races (re-checks the count
    on the setup POST).
- `require_role(required)` dependency factory in `app.utils.rbac`:
  reads `request.session["admin_role"]`, gates by the ladder, raises
  HTTP 403 with a descriptive message naming both the actor's role
  and the required role. Bypass paths: legacy key (virtual
  superadmin) and bearer tokens (governed by scopes, not roles).
- Audit attribution updated: `actor_by()` now reads
  `admin_role` from the session and emits
  `admin:session:<user>:<role>` (e.g. `admin:session:alice:superadmin`)
  so audit-log filters can match on both *who* and *with what
  authority*.
- Admin user CRUD route + Pydantic schemas in
  `routes/admin_users.py` (gated to `superadmin`):
  list / create / update (role + activation + password rotation) /
  hard-delete. Self-protection guards: a superadmin cannot demote /
  deactivate / delete *themselves*, and the last active superadmin
  is never the last (any of those operations on the last
  superadmin fail with 409). Soft revocation (`is_active=false`)
  preserves the audit trail; hard delete is for test rows.
- Admin UI page at `/ui/admin-users` (linked in nav, superadmin-only
  — non-superadmins see a "Only superadmins can view this" empty
  state when the underlying API returns 403). Inline role dropdown,
  reactivate/deactivate, password reset, delete. New-user modal with
  role selector defaulting to `admin`.
- Nav hiding in `base.html`: Audit Log nav entry hides for roles
  below auditor; Admin Users nav entry hides for non-superadmins.
  Sidebar footer shows the signed-in user + role badge.
- Three role gates applied as proof-of-wiring (broader rollout is
  slice 2): `POST/PUT/DELETE /admin/asset-types*` → `admin`+,
  `GET /admin/audit-log` → `auditor`+,
  `/admin/admin-users*` → `superadmin`.
- Smoke-tested end-to-end:
  * First-run setup created `alice` (superadmin) with a 118-char
    PBKDF2 hash. Login page now renders the regular sign-in form
    instead of the setup form.
  * Login as alice → 303 → `/admin/admin-users` returns the user list.
    Wrong password → 401 with descriptive error.
  * `bob` (admin role) created via API; bob can hit asset-type endpoints
    (passes role gate) but is 403'd from `/admin/admin-users`
    (descriptive message names the gap).
  * `carol` (auditor) created; her asset-type create attempt returns
    403 with `Role 'auditor' is below the required 'admin'`.
  * Legacy `X-Admin-Key` continues to grant unrestricted access (returned
    full admin-user list), proving back-compat.
  * Self-protection guards: alice (sole superadmin) blocked from
    self-demote (409), self-deactivate (409), self-delete (409).
    Promoting bob to superadmin doesn't unlock alice's self-demote
    (deliberate — avoids accidental session lockout).
  * Audit row attribution: `api:create_admin_user (admin:session:alice:superadmin)`.

**Still to do — RBAC slice 2:**
- [ ] Comprehensive role gating across the rest of `/admin/*` —
      runbooks, modules, maintenance, standalone runbooks, license,
      seed-export, cost-report. Most slot in as
      `dependencies=[require_role("admin")]` mechanically; auditor
      read paths for endpoints that have separate GET/PUT need
      careful per-route classification.
- [ ] Per-asset-type ACL grants (`admin_user_asset_type_grants`)
      so platform owners can be delegated without seeing other
      teams' configs.
- [ ] SoD enforcement: configurer of an asset type must not also
      approve their own access requests against it. Likely a check
      at order-creation time using the audit trail.
- [ ] Bearer-token role binding (today scopes are orthogonal to roles
      — a token with `admin:*` is implicitly superadmin-equivalent;
      slice 2 binds tokens to a specific role for clearer authz).
- [ ] Self-service "change my password" page for non-superadmins.
- [ ] Forced password rotation policy + lockout-on-N-failed-attempts.

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

**Done — wider scope rollout (2026-04-26):**
- Scope decorators now cover the rest of `/admin/*`'s `app_config`,
  asset, and email-template surface in `routes/admin.py`:
  `config:read` on GET `/config`, GET `/config/{key}`,
  GET `/config/siem/status`, GET `/email-templates`,
  GET `/email-templates/{event_key}`; `config:write` on
  POST/PUT/DELETE `/config`, PUT `/email-templates/{event_key}`;
  `assets:read` on GET `/assets`; `assets:write` on POST `/assets`,
  POST `/assets/bulk`, PUT `/assets/{id}`, DELETE `/assets/{id}`,
  POST `/assets/{id}/force-delete`, POST `/assets/{id}/revoke`.
- Operational test endpoints (`/config/ad/test`, `/config/entra/test`,
  `/config/teams/test`, `/config/email/test`, `/config/sccm/test`,
  `/config/siem/test`) are intentionally left scope-free — they are
  diagnostic actions only meaningful from the admin UI session, never
  driven by an integration token.
- `admin_approval_delegations.py` already carried `approvals:read` /
  `approvals:write`; verified no further changes needed there.
- Smoke-tested with two narrowly-scoped tokens: `config:read`-only
  → 200 on GET `/config` + GET `/config/siem/status`, 403 on POST
  `/config` and GET `/assets`; `assets:read`-only → 200 on GET
  `/assets`, 403 on POST `/assets` and GET `/config`. Error bodies
  name the token, missing scope, and granted scopes as designed.
- Legacy `X-Admin-Key` and admin sessions retain implicit `admin:*`
  by design — UI flows and existing scripts continue working.

**Done — audit attribution on /orders + portal flows (2026-04-26):**
- New `portal_actor_by(current_user, label)` helper in
  `app.utils.audit` mirrors the admin-side `actor_by(request, label)`
  contract. Output formats:
  * Authenticated portal user: `api:<label> (portal:user:<email>)`
  * Anonymous portal mode (Entra disabled): `api:<label> (portal:anonymous)`
  * Empty / missing email: `api:<label> (portal:user:unknown)`
  * No `current_user` dict at all: `api:<label>` (clean fallback)
  Email lower-cased so audit-log filters can match without case juggling.
- Portal mutation routes audit rows now record who drove the change.
  Three previously-silent mutations now emit audit rows:
  * `POST /portal/orders/new` → `order` `created`
  * `POST /portal/orders/{id}/change` → `order` `created` (with
    `ctx="modify_of:<orig_id>"`)
  * `POST /portal/orders/{id}/cancel` → both branches: scheduled
    cancellation logs `order` `status_changed` on the original;
    active cancellation logs the new DELETE order's `created` plus
    the original's `status_changed`. Two rows, same actor.
  All four routes pull classification via `classify_for_asset_type_id()`
  so per-class retention windows apply uniformly across portal +
  admin paths.
- `apply_approval_decision()` reworked to emit per-decision audit rows.
  Each individual approve / decline becomes an `order_approval`
  audit row (with `rule_name` and `comment` in the snapshot) so the
  trail captures each voter even before quorum is met. The order's
  status transition (`status_changed` on decline,
  `approved_and_dispatched` on quorum-met) gets its own row using the
  same actor. New `actor=` kwarg on the helper; portal route passes
  `portal_actor_by(current_user, "decide_approval")`,
  signed-token route passes `api:approval_token (approver:<email>)`.
  Default fallback preserves back-compat for any in-flight callers.
- `/orders/` API router got a non-raising soft-auth dependency
  `attribute_actor_if_present()` that mirrors `require_admin_key`'s
  three-credential recognition (legacy key / session / bearer token)
  but never raises on missing or invalid creds — keeps the public
  ServiceNow contract unchanged. Three audit call sites switched
  from hardcoded `api:create_order` etc. to `actor_by(request, ...)`:
  POST `/orders/`, PATCH `/orders/{id}`, DELETE `/orders/{id}`.
- `portal_delegations.py` aligned: `aaudit(by=...)` for create + revoke
  switched from the ad-hoc `f"portal:{email}"` to `portal_actor_by()`
  so portal-driven delegation rows now consistent with the rest of
  the audit log.
- Verified end-to-end:
  * Anonymous `POST /orders/` → `api:create_order` (no actor —
    fallback is unchanged).
  * `POST /orders/` with `Authorization: Bearer xpat_…` → audit row
    `api:create_order (token:smoke-orders-actor-2)`. Soft-auth path
    correctly captured the token without 401-ing on missing scopes.
  * `portal_actor_by()` produces the right strings for all five
    cases (real user / anonymous / no email / None / mixed-case).

**Still to do — separate slices:**
- [ ] Optional: hard-delete vs. soft-delete policy (today everything
      is soft-deleted; some tenants will want a "purge revoked tokens
      older than 90 days" Beat task).

### [partial] Tamper-evident audit + SIEM export — Prio 0
SIEM streaming side **shipped 2026-04-26** (Splunk HEC + Microsoft
Sentinel adapters). Tamper-evident DB-grant revocation on `audit_log`
is split into a separate slice because it touches role grants on a
live table and is best paired with the RBAC work.

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

**Done — Tamper-evident audit_log (2026-04-26):**
- Migration `0062_audit_log_append_only.py` installs three
  BEFORE-statement triggers on `audit_log` (DELETE / UPDATE /
  TRUNCATE) that raise an exception unless the transaction sets
  `ipsolis.allow_audit_mutation = 'true'` via `SET LOCAL`.
- Default-deny posture: nobody — including an operator with full
  DB credentials — can quietly mutate audit history. Errors are
  loud and self-documenting (the message names the bypass GUC).
- Documented escape hatch for retention pruning so future
  classification-driven retention work can implement it cleanly.
- Triggers are FOR EACH STATEMENT (single fire per statement
  regardless of row count), implemented via a shared
  `audit_log_no_mutate()` plpgsql function.
- Verified end-to-end: INSERT works, DELETE/UPDATE/TRUNCATE all
  blocked with descriptive errors, bypass via `SET LOCAL` works
  within a single transaction, default-deny returns immediately
  after the bypass commit, and the app-level audit-write flow
  (config PUT → new audit row) is unaffected.

**Done — Microsoft Sentinel adapter (2026-04-26):**
- New `build_sentinel_payload()` + `post_sentinel()` in
  `worker/tasks/modules/siem_export.py`, mirrored on the API side in
  `api/app/utils/siem_export.py`. Uses Azure Monitor's HTTP Data
  Collector API (workspace_id + base64 shared key, HMAC-SHA256 signed
  per request — stdlib `hmac`/`hashlib`, no Azure SDK dependency).
  `validate=True` on the base64 decode so a pasted-with-typos shared
  key fails with a descriptive error instead of producing a wrong
  signature that Sentinel rejects with an opaque 403.
- `Log-Type` header drives the custom table name — Sentinel
  materialises ingest into `{log_type}_CL` (default `IpsolisAudit_CL`)
  on first event; no schema registration needed. The
  `time-generated-field: timestamp` header tells Sentinel to use our
  audit-log timestamp as the row time, not ingest time.
- Migration `0065_seed_sentinel_siem_config.py` seeds three new
  `app_config` keys: `siem.workspace_id` (plain), `siem.shared_key`
  (secret), `siem.log_type` (default `IpsolisAudit`). Existing siem.*
  values are not touched. `siem.format` description updated to list
  both adapters.
- Streamer Beat task picks up the new branch on `siem.format == 'sentinel'`,
  with the same cursor / retry / status semantics as Splunk. Per-format
  precondition checks on missing creds give tighter error messages
  instead of round-tripping out to a misconfigured endpoint.
- Settings UI (Compliance tab) gets a Format dropdown that swaps
  between Splunk and Sentinel field groups via `onSiemFormatChange()`,
  syncs visible fields with the saved format on page load, and persists
  both adapter inputs so admins can flip back without retyping.
  Per-adapter help cards explain where to find each set of credentials.
- Send Test Event button is wired through `/admin/config/siem/test`
  with the new keys; same flow as Splunk.
- README + `docs/ENTERPRISE_FEATURES.md` updated: setup walkthrough,
  full table of stored config keys, note that Microsoft supports the
  Data Collector API through Sept 2026 with a future slice planned
  for the newer Logs Ingestion API (DCE/DCR).
- Smoke-tested end-to-end:
  * Invalid base64 shared key → `Shared key is not valid base64: Only
    base64 data is allowed`.
  * Empty workspace_id → `Workspace ID or shared key is missing.`
  * Valid base64 + bogus workspace GUID → DNS-fails on the
    `{guid}.ods.opinsights.azure.com` resolution (proves URL builder).
  * Streamer dispatched to the Sentinel branch, batched 20 audit rows,
    failed cleanly on the bogus endpoint, kept the cursor at 0 for
    retry, recorded `siem.last_error`, did not advance.
  * Switched format back to splunk_hec mid-test and the original
    Splunk error path returned its old "Endpoint URL or HEC token is
    missing." message — no regression.

**Done — generic HMAC-signed webhook adapter (2026-04-26):**
- New `build_webhook_payload()` + `post_webhook()` in
  `worker/tasks/modules/siem_export.py`, mirrored on the API side in
  `api/app/utils/siem_export.py`. Sends the same flat JSON array of
  events that Sentinel uses; signs the raw body with HMAC-SHA256 and
  emits the digest in a configurable header (default
  `X-Hub-Signature-256: sha256=<hex>`, GitHub-compatible — receivers
  can reuse `hmac.compare_digest` against a recomputed digest with no
  vendor-specific library required).
- Always-emitted headers: `Content-Type: application/json`,
  `User-Agent: ipsolis-siem/1.0`, `X-Ipsolis-Event: audit.batch`,
  plus the configured signature header. Operators can supply
  additional headers as a JSON object in
  `siem.webhook_extra_headers` (e.g.
  `{"DD-API-KEY":"…","Authorization":"Bearer …"}`) — useful for
  Datadog, Sumo, Splunk-cloud, or homegrown receivers that want a
  static auth header alongside HMAC verification. Malformed JSON in
  that field is logged and ignored at runtime so a single typo can't
  silently break streaming.
- Migration `0068_seed_webhook_siem_config.py` seeds four new
  `app_config` keys: `siem.webhook_url` (plain), `siem.webhook_secret`
  (secret), `siem.webhook_signature_header` (default
  `X-Hub-Signature-256`), `siem.webhook_extra_headers` (JSON).
  `siem.format` description updated to list all three adapters.
- Streamer Beat task gets the third branch on `siem.format == 'webhook'`,
  with the same cursor / retry / status semantics as Splunk and
  Sentinel. Per-format precondition checks added so missing
  webhook credentials surface a tight error instead of round-tripping
  out to nothing.
- Settings UI (Compliance tab) format dropdown gains a third option
  "Generic Webhook (HMAC-signed)"; the existing format-toggle
  generalised to a config-driven loop so future adapters can be
  wired without further JS surgery. Per-adapter help cards explain
  HMAC verification with a copy-pasteable Python snippet for receivers.
- README + `docs/ENTERPRISE_FEATURES.md` updated: third adapter in
  feature description, full setup walkthrough including HMAC
  verification example, table of new config keys, hints for Elastic /
  Datadog / Sumo / Loki receivers.
- Smoke-tested end-to-end with a stdlib HMAC-verifying listener:
  * "Send Test Event" with the listener inside the api container →
    `Webhook accepted test event (HTTP 200)`. Listener log confirmed
    the sent `X-Hub-Signature-256` digest matched its independent
    recompute, the `X-Datadog-Test: hello` extra header was
    propagated, and the JSON-array payload contained 1 event.
  * Worker streamer pass with the listener inside the worker
    container forwarded a 40-event batch in a single POST,
    advanced the cursor to id 878, and the listener confirmed
    HMAC-match on the larger payload.
  * Switched format back to `splunk_hec` mid-test → no regression on
    the existing adapters.

**Still to do — separate slice:**
- [ ] Sentinel via the newer Logs Ingestion API (DCE / DCR / service
      principal) — mainly for tenants that have already switched off
      the Data Collector API or who want enriched DCR transformations.
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

**Done — N-of-M approvals (2026-04-26):**
- Migration `0061_asset_type_min_approvals.py` adds an
  `INTEGER NULL` column. NULL / 0 / >= total rows means "all
  required" (legacy default); set N for any-N-of-M semantics.
- ORM `AssetType.min_approvals_required` mapped; Pydantic Create/
  Update/Read schemas carry the field; audit `_type_snap()` includes
  it so changes are diffable.
- Runtime evaluator in `apply_approval_decision`: after recording
  an approve, counts approved rows, looks up the asset type's
  threshold, and either dispatches the order (threshold met) or
  logs the progress (still waiting). When the threshold is met it
  marks remaining pending rows as `superseded` — a new status
  string that disappears from pending lists, doesn't attract
  reminders / escalations, and can't be retroactively acted on.
- Decline is still a hard veto regardless of N — keeps a clear
  accountability path even with soft N-of-M policies.
- Admin form: new "Minimum approvals required" input next to the
  approval-owners block, blank/0 placeholder = "all". JS submitter
  sends the integer (or null) on save.
- Verified end-to-end: synthetic order with 3 application_owner
  rows + `min_approvals_required=2` → 1st approve = waiting, 2nd
  approve = threshold met, 3rd row = `superseded`,
  ``threshold_met=True`` correctly triggered
  ``_post_approval_dispatch``.

**Done — conditional approval rules slice 1 (2026-04-26):**
- Migration `0064_asset_type_approval_rules.py` adds an
  `approval_rules` JSON column on `asset_types`. Each rule is a dict
  ``{name, condition: {field, op, value}, approvers: [{email, name}]}``.
- Evaluator in `app.utils.approval_rules`:
  * `build_context(order, asset_type)` materialises the dict the
    rule conditions evaluate against.
  * `evaluate_rules(rules, ctx)` walks the list, returns a deduped
    set of approver dicts to add. Each result includes
    ``rule_name`` so the audit trail / UI can show which rule
    triggered the inclusion.
  * `_matches()` honours six fields (`duration_days`,
    `monthly_cost`, `has_pii`, `has_phi`, `has_pci`,
    `requester_department`) and six operators (`>`, `>=`, `<`,
    `<=`, `==`, `contains`). Malformed conditions are logged at
    WARNING and skipped — a hand-edited JSON typo can never block
    order creation.
- Wired into the portal order-creation flow alongside the
  manager / owner approvals: rule-derived approvers go through the
  same `_make_approval()` helper (so delegation re-routing applies),
  approver_type is set to `rule:<truncated rule name>` so the audit
  trail names the rule, and `seen_emails` deduplication prevents
  the rule from creating a second approval row when the same
  person is already covered as manager / owner.
- `order.status` is auto-promoted to `pending_approval` if rules
  trigger when the static toggles were off — so an asset definition
  can rely entirely on rules without setting `requires_manager_approval`.
- ORM, schemas (Create/Update/Read), audit `_type_snap()` all carry
  the new field; admin route handles create / update / clone.
- Admin UI rule builder in the asset-definition form (Approval
  section): a list of rows with name + field + op + value +
  approver-emails (CSV) + remove button, plus an "+ Add rule"
  factory. Submit serializer drops incomplete rows so partial
  edits don't ship as broken rules.
- Verified end-to-end: evaluator unit tests (no-trigger, single,
  double, malformed-rule cases) all behave correctly; round-trip
  through `PUT /admin/asset-types/{id}` persists rules in the JSON
  column verbatim.

**Done — conditional approval rules slice 2 (2026-04-26):**
- Boolean composition: ``_eval_condition()`` now recognises compound
  nodes ``{"op": "and"|"or"|"not", "clauses": [...]}`` alongside the
  slice-1 leaf shape, recursing up to 8 levels. ``and`` is vacuously
  True on empty clauses; ``or`` is False on empty clauses; ``not``
  inverts a single clause. Leaf shape is preserved unchanged so all
  existing rules round-trip.
- Custom-attribute conditions: ``build_context()`` flat-maps every
  ``order.config`` key under ``attr.<key>`` so a rule can reference
  ``attr.cost_center`` or ``attr.justification`` with the same six
  operators. ``contains`` against a list-valued attr (e.g.
  MULTI_ENUM) iterates members instead of stringifying the list.
- Per-rule N-of-M: optional ``min_approvals_required`` on the rule
  itself. Migration ``0066_order_approval_rule_quorum.py`` adds two
  columns to ``order_approvals``:
  * ``rule_name`` (200 chars, NULL on manager / owner rows) carries
    the full untruncated rule name — the existing ``approver_type``
    column is capped at 30 chars and only holds a short prefix.
  * ``rule_threshold`` (int, NULL fold-in-with-global) freezes the
    quorum at order-creation time so subsequent admin edits to the
    asset-type rules don't shift the order's decision logic
    mid-flight.
- Decision logic in ``apply_approval_decision()`` rebuilt: each rule
  with its own threshold forms a private quorum bucket; manager /
  owner / no-threshold-rule approvers fold into a single "global"
  bucket gated by ``asset_type.min_approvals_required``. ``threshold_met``
  is true iff every bucket meets its quorum. Per-bucket thresholds
  are clamped to the bucket size so a rule asking for more approvers
  than it has can't create an unfulfillable quorum. Pending approvals
  are only superseded once every bucket is satisfied — no premature
  "approved" while another bucket still needs decisions.
- Rule builder UI rebuilt as a card-per-rule editor: name + ALL/ANY
  combinator + per-rule quorum input in the header; conditions
  stacked vertically with their own ``+ Add condition`` button; field
  input is a free-text ``<input list="approval-rule-fields">``
  backed by a datalist that includes built-ins plus every
  ``attr.<key>`` from the asset type's ``config`` so admins get
  autocomplete for known custom attributes. Saved rules with deeply-
  nested conditions render with an inline warning, since the simple
  card editor only round-trips top-level clauses.
- Save serializer collapses 1-leaf rules to the flat slice-1 shape
  (cleaner JSON for trivial rules) and emits the compound shape only
  when 2+ leaves exist. Per-rule quorum sent as
  ``min_approvals_required`` only when set.
- README updated; ``docs/ENTERPRISE_FEATURES.md`` left as-is (rules
  weren't called out there in slice 1 — adding a section is a future
  doc-polish slice).
- Verified end-to-end:
  * Evaluator unit tests for leaf, AND, OR, NOT, attr-fields,
    nested compounds, threshold-bearing rules — all green.
  * Bucket-decision smoke tests (single global, mgr+rule, multi-rule
    with clamped threshold, asset-type N-of-M crossed with rule
    N-of-M) — all green.
  * Round-trip via ``PUT /admin/asset-types/{id}``: persisted shape
    matches what the evaluator consumes, rule with
    ``{op:"and",clauses:[duration>30, attr.cost_center contains EU]}``
    + per-rule quorum=1 fires correctly against a synthetic order
    in the EU cost center, and the same rule does NOT fire when
    cost_center is flipped to US (proves attr.* lookup).
  * Admin UI form renders the card structure, datalist with attr
    fields, combinator + quorum inputs.

**Still to do — slice 3 (deferred):**
- [ ] Visual editor for deeply-nested compounds (today's UI flattens
      to 1 level + warning; tree editor would close the gap for
      power users).
- [ ] Per-bucket reminder optimisation: stop nagging approvers in a
      bucket whose quorum is already met but whose siblings are still
      pending (today they get reminders until the *whole* order
      crosses the line).
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

**Done — Celery queue depth gauge (2026-04-26):**
- New `ipsolis_celery_queue_depth{queue}` Prometheus gauge in
  `app.utils.metrics`. Refreshed on every `/metrics` scrape via
  `redis.asyncio` `LLEN` against the four known queues
  (`default`, `provision`, `reclaim`, `notifications`).
- Resilient: missing/non-Redis broker → gauges cleared (no error);
  per-queue LLEN failures logged at WARNING and skipped without
  affecting other queues.
- Verified live: pushed 3 synthetic messages to `provision`,
  next scrape reported `provision=3.0`; cleared the queue, next
  scrape reported `provision=0.0`. Pre-existing `default=2.0`
  matched real Beat-scheduled tasks waiting in the broker.

**Done — Grafana dashboard + Prometheus alerts (2026-04-26):**
- New `docs/grafana/ipsolis-overview.json` — 9-panel dashboard
  (request rate, error rate, p95 latency, pending approvals stats;
  request-rate-by-route + latency-percentiles timeseries; orders by
  status + asset-pool composition; Celery queue depth). Uses
  `${DS_PROMETHEUS}` template variable so it imports against any
  Prometheus datasource UID without editing.
- New `docs/grafana/prometheus-alerts.yaml` — 6 alert rules across
  3 groups: HTTP (high 5xx, slow p95), business (approval backlog,
  Celery queue warning + critical), pool capacity. Uses the labels
  we already emit, so no extra recording rules needed.
- New `docs/grafana/README.md` — Prometheus scrape config snippet,
  Grafana import walkthrough, threshold rationale, plus a section
  on wiring Tempo / Jaeger as a separate datasource for the OTel
  traces we ship.
- Cross-linked from `docs/ENTERPRISE_FEATURES.md` so admins find
  it from the main feature docs.
- Verified the JSON dashboard parses (9 panels) and the YAML alerts
  parse (6 rules / 3 groups) — no field-shape regressions.

The full observability story (Prometheus metrics + business gauges +
Celery queue depth + OpenTelemetry api/worker tracing + ready-to-import
Grafana dashboard + Prometheus alert rules) is now end-to-end complete.

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

**Done — audit retention pruning slice 1 (2026-04-26):**
- Migration `0063_seed_retention_config.py` seeds three keys:
  `retention.audit_log_days` (window, 0 = disabled),
  `retention.last_run_at`, `retention.last_pruned` (auto-managed
  status fields).
- New Beat task `worker/tasks/workflows/audit_retention.py:prune_old_rows`,
  scheduled daily at 03:00 Europe/Berlin via crontab. Reads the
  window, opens a transaction, sets the documented
  `ipsolis.allow_audit_mutation` GUC via `SET LOCAL`, and DELETEs
  rows past the window with a CTE that returns the count.
- Status fields updated atomically with the prune so the Settings
  UI can show "Last run: <ts> · Pruned: <N> rows".
- Settings UI (Compliance tab → "Audit Log Retention" card):
  retention-days input + status panel showing last run + last pruned.
- Verified end-to-end: 5 stale rows + 5 fresh rows seeded → set
  window to 30 days → prune ran → returned `pruned: 5`, only
  fresh rows survived; status fields updated correctly; direct
  DELETE outside the prune transaction still blocked by the
  tamper-evident trigger (bypass is properly txn-scoped).

**Done — per-classification retention slice 2 (2026-04-26):**
- Migration `0067_audit_log_classification.py` adds a
  `classification` column on `audit_log` (default `internal`,
  indexed) and seeds three new windows + a status field:
  `retention.pii_days`, `retention.phi_days`, `retention.pci_days`,
  `retention.last_pruned_by_class` (auto-managed JSON breakdown).
  Backfilled existing rows to `internal` inside a `SET LOCAL`
  bypass transaction so the immutability triggers from 0062 don't
  block the migration.
- Classification is set at write time (not at prune time, as
  originally sketched in slice-1 notes). The strictest of any
  attribute on the touched asset type wins (`pci > phi > pii > internal`)
  via shared `classify_asset_type()` / `classify_for_asset_type_id()`
  helpers in `app.utils.audit`. Classifying at write time freezes
  each row's retention class against subsequent attribute edits on
  the type — the row's regulatory category is determined by the
  type's state at the moment of the audited change, not the type's
  state at prune time.
- Wired into all high-value audit writes: asset_type CRUD (4 sites),
  asset CRUD (5 sites), order create/update/cancel (3 sites),
  webhook order create. Other audit writes default to `internal` —
  config / approval delegation / api token / etc. fall under the
  global window. `waudit()` (worker side) gets the same kwarg.
- Beat task rewritten to iterate buckets: one DELETE per
  classification scoped via `SET LOCAL ipsolis.allow_audit_mutation`
  + COMMIT, so a single huge bucket can't starve the others. The
  global window applies to `internal` + NULL only; per-class
  windows apply to that class only and **do not fall back to the
  global default** when set to 0 — explicit opt-in to retention so
  PII/PHI/PCI rows are never accidentally dropped under the
  catch-all. `retention.last_pruned_by_class` records the per-class
  count for ops visibility.
- Settings UI (Compliance tab → "Audit Log Retention" card)
  rebuilt: default window unchanged, plus a sub-card with three
  per-class day inputs (PII / PHI / PCI). Status panel renders the
  last-run-by-class breakdown when non-empty.
- Verified end-to-end:
  * Asset type 16 with one `pii`-tagged attribute → audit rows
    from `PUT /admin/asset-types/16` come out with
    `classification='pii'`. Pre-existing rows backfilled to
    `internal`. Total counts match.
  * Backdated 5 internal rows + 1 PII row by 30 days. With
    `audit_log_days=1, pii_days=0`: prune deleted 5 internal rows,
    PII row preserved. With `audit_log_days=1, pii_days=14`: PII
    row (30d > 14d) was deleted in the next pass; per-class JSON
    `{"internal":0,"pii":1}` matches.
  * Tamper-evident triggers still hold outside the prune
    transaction — direct DELETE/UPDATE without the GUC bypass
    raises the original error.

**Still to do — slice 3 (out of scope here):**
- [ ] Approval routing: orders containing PII/PHI/PCI fields
      automatically include an extra approval step (e.g. compliance
      officer). The conditional-approval-rules engine already covers
      this with `has_pii / has_phi / has_pci` fields — slice is more
      about defaults / discoverability than mechanism.
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
