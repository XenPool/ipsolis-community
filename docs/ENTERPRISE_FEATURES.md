# Enterprise & operability features

This page covers the per-feature setup for capabilities that go beyond the
default install. Everything below is community-licensed unless an
explicit *Enterprise license* note appears.

**Catalog & portal**

- [Per-user quota (`max_per_user`)](#per-user-quota-max_per_user)
- [Active / inactive flag on asset definitions](#active--inactive-flag-on-asset-definitions)
- [Long-form help text per asset definition (markdown)](#long-form-help-text-per-asset-definition-markdown)
- [Catalog search and category filter](#catalog-search-and-category-filter)
- [Field-level data classification](#field-level-data-classification)
- [Per-order cost projection on the portal](#per-order-cost-projection-on-the-portal)

**Approvals**

- [Microsoft Teams approval cards](#microsoft-teams-approval-cards)
- [Approval reminders](#approval-reminders)
- [Approval escalation](#approval-escalation)
- [Approval delegation (admin + portal self-service)](#approval-delegation-admin--portal-self-service)
- [N-of-M approvals + conditional rules](#n-of-m-approvals--conditional-rules)
- [Auto-decline on extended inactivity](#auto-decline-on-extended-inactivity)

**Observability**

- [Prometheus `/metrics` endpoint](#prometheus-metrics-endpoint)
- [OpenTelemetry tracing (api + worker)](#opentelemetry-tracing-api--worker)

**Compliance & audit**

- [SIEM audit-log streaming (Splunk HEC + Microsoft Sentinel + Generic Webhook)](#siem-audit-log-streaming-splunk-hec--microsoft-sentinel--generic-webhook)
- [Tamper-evident audit log + retention](#tamper-evident-audit-log--retention)

**Authentication & access control**

- [Per-integration API tokens](#per-integration-api-tokens)
- [Admin RBAC (roles, ACL grants, SoD, password policy)](#admin-rbac-roles-acl-grants-sod-password-policy)
- [External secret management (HashiCorp Vault + CyberArk CCP/AIM)](#external-secret-management-hashicorp-vault--cyberark-ccpaim)

**Operations**

- [PowerShell modules — Linux compatibility](#powershell-modules--linux-compatibility)
- [HA Beat scheduler (multi-replica with celery-redbeat)](#ha-beat-scheduler-multi-replica-with-celery-redbeat)
- [Setup checklist + pool capacity warnings on the dashboard](#setup-checklist--pool-capacity-warnings-on-the-dashboard)

**Finance**

- [Cost report / chargeback](#cost-report--chargeback)

---

## Per-user quota (`max_per_user`)

Caps how many active orders a single user can hold against one asset
definition. Active orders are counted across `pending`,
`pending_approval`, `scheduled`, `processing`, `provisioning`,
`provisioned`, and `delivered` states — so users can't bypass the limit
by stacking scheduled or awaiting-approval orders.

**Where to set it**

Admin UI → *Asset Definitions* → edit a definition → field
*Max. Instances per User*. Visible for assignment models
*Personal* and *Pooled*; hidden for *Shared* (one shared instance has
no per-user concept).

**What requesters see**

Order creation returns HTTP 409 with a descriptive message
(`Per-user limit reached: alice@example.com already holds 2/2 instances
of this asset definition.`). The portal renders this inline on the
order page.

**Enforcement points**

- Public order API (`POST /orders`)
- ServiceNow webhook (`POST /webhook/servicenow`)
- Self-service portal form

---

## Active / inactive flag on asset definitions

Lets operators *deprecate* a definition without deleting it. The
definition disappears from the portal catalog so users can no longer
request new instances, but stays visible in the admin list with an
"Inactive" badge — historical orders, audit trail, runbook configs all
remain coherent.

**Where to set it**

Admin UI → *Asset Definitions* → edit a definition → checkbox
*Active* in the Identity section.

---

## Long-form help text per asset definition (markdown)

Admins can attach a multi-paragraph note in markdown to every asset
definition. Requesters see it on the order page when they pick the
type — a panel above the attribute selectors with the rendered help
text. Use it for:

- Pre-installed software, expected provision time
- Who to contact for support
- License terms, eligibility caveats
- Links to internal docs

**What's allowed**

Standard markdown: paragraphs, headings, bold/italic, inline code, code
blocks, lists, links, blockquotes, horizontal rules. Output is sanitized
through a `bleach` allowlist; scripts, inline styles, `javascript:` URLs,
and `onerror=` attributes are stripped before rendering.

**Where to set it**

Admin UI → *Asset Definitions* → edit a definition → field
*Help text (markdown)* in the Identity section.

---

## Catalog search and category filter

Pure client-side search + category dropdown above the asset definition
grid on `/portal/orders/new`. Auto-hidden for catalogs with six or
fewer definitions (just clutter for small catalogs).

**Match scope**

Each card carries `data-search` (lowercased name + description +
help_text) and `data-category` (the AssetCategory enum value). Filter
applies in one DOM pass — no server round-trip. If the user already
had a card selected and the new filter hides it, the selection is
cleared and dependent panels reset, so a stale `asset_type_id` cannot
be submitted.

**i18n**

Search placeholder, category labels, "no match" empty state, and
"Clear" link are all localized. Available in en/de/fr/es/it.

---

## Microsoft Teams approval cards

When an order needs sign-off, the worker posts an Adaptive Card to a
Microsoft Teams channel/chat in addition to the email notification.
The card has a *Review request →* button that opens a tokenized
confirmation page with no portal login required — works from any
client (Outlook web, mobile mail, Teams, browser).

### Architecture (why this works on M365 Business Pro)

- **Microsoft Teams Workflows webhook** — admin creates the workflow
  once per target channel/chat. No Azure Bot registration, no Microsoft
  Graph permissions, no Azure AD app for messaging.
- **Signed approval token** — HMAC-SHA256 signed with `API_SECRET_KEY`
  (rotating that env var invalidates all outstanding links — the right
  thing on incident response). 14-day TTL. Token format is identical
  in the worker and the API so a token minted by the worker validates
  on the API endpoint.
- **No GET-based one-click approve** — Outlook and Teams link
  previewers prefetch URLs, so one-click GET would let them
  accidentally approve. The card opens a confirmation page where the
  approver picks Approve or Decline (with optional comment).

### One-time admin setup

In Microsoft Teams:

1. Open the channel where approvals should appear (a dedicated
   *#approvals* channel works well).
2. Click `…` → **Workflows** on the channel.
3. Choose the template **"Post to a channel when a webhook request
   is received"**.
4. Walk through the wizard — it generates an HTTPS webhook URL.
5. Copy the webhook URL.

In ip·Solis Admin UI:

1. Go to *Settings* → *E-Mail* tab.
2. Find the **Microsoft Teams — Approval Cards** section.
3. Set *Mode* to `Enabled — send Teams card alongside email`.
4. Paste the workflow webhook URL into *Teams Workflow Webhook URL*.
5. Click **Save Settings**, then **Send Test Card** to verify
   end-to-end before enabling for real approvals.

### Operational behavior

- Card delivery is best-effort. If Teams delivery fails (network,
  expired workflow, etc.), the order is **not** held up — the email
  still goes out and the warning is logged.
- The signed `/approve/{token}` endpoint is open (no Entra SSO
  required). Tampering, expiry, and already-decided cases all return
  appropriate status pages with no information leakage.
- Cards include a Teams `@mention` of the approver
  (`msteams.entities` block in the Adaptive Card) with the approver's
  UPN as the entity id. Teams renders this as a real mention and fires
  a banner / system-tray notification on the approver's client. Without
  this the Workflow-authored channel post would arrive silently.
- If your Workflow template strips `msteams.entities`, the body falls
  back to plain text using the approver's display name — still
  readable, just no banner notification. The fix is to either choose
  a Workflow template that forwards entities, or change the channel's
  per-user notification setting to "All new posts → Banner & feed".

### Stored config keys

| Key | Purpose | Stored as |
|---|---|---|
| `teams.mode` | `disabled` or `enabled` | plain |
| `teams.webhook_url` | Teams Workflows webhook URL | secret |

---

## Approval reminders

A Beat task scans every hour for `pending` approvals that haven't been
acted on in the configured window and re-sends both the email and the
Teams card (if configured). The original signed approval link from
`/approve/{token}` is reused, so the reminder works exactly like the
initial notification — one click in either channel still lands on the
no-login confirmation page.

### Why this matters

Approvals stack up in inboxes. A 24-hour SLA on access requests is
common in regulated environments; without automatic nudges, ip·Solis
relied on the original email being seen the same day. Reminders close
that gap without operator intervention.

### Behaviour

- **Cadence**: Beat task runs hourly at minute 15. A pending approval
  qualifies when `COALESCE(last_reminded_at, created_at)` is older
  than `approval.reminder_after_hours` (default 24).
- **Cap**: each approval gets at most `approval.max_reminders`
  reminders (default 3). After the cap is hit the row is left alone —
  the request stays pending until somebody acts on it directly or
  cancels the order.
- **Channels**: identical delivery path to the original notification.
  Email always; Teams card only when `teams.mode = enabled`. The
  Adaptive Card title is bumped to *"Reminder (n): access request
  awaiting approval"* so recipients can tell it's a nudge, not a
  duplicate.
- **Tracking**: per-row `reminder_count` and `last_reminded_at`
  columns on `order_approvals`.

### Where to configure

Admin UI → *Settings* → *E-Mail* tab → *Approval Reminders* section:

| Field | Default | Notes |
|---|---|---|
| Status | Enabled | `disabled` skips the Beat task silently |
| Reminder after (hours) | 24 | 1–720 |
| Max reminders | 3 | 0 disables nudges entirely without flipping the master switch |

### Stored config keys

| Key | Purpose |
|---|---|
| `approval.reminders_enabled` | `true`/`false` master switch |
| `approval.reminder_after_hours` | Hours since the last notification before a reminder fires |
| `approval.max_reminders` | Cap per approval row |

---

## Prometheus `/metrics` endpoint

Standard Prometheus text-format metrics at `GET /metrics`. Disabled by
returning a 404 when `metrics.enabled = false` in `app_config`.

### Exposed metrics

| Metric | Type | Labels |
|---|---|---|
| `ipsolis_http_requests_total` | counter | `method`, `route`, `status_class` |
| `ipsolis_http_request_duration_seconds` | histogram | `method`, `route` |
| `ipsolis_orders_in_status` | gauge | `status` |
| `ipsolis_approvals_pending` | gauge | — |
| `ipsolis_pool_assets` | gauge | `asset_type`, `status` |

### Cardinality protection

Route labels use the **registered FastAPI path template**
(e.g. `/orders/{order_id}`), not the actual path, so a million distinct
order IDs don't produce a million time series. High-volume static
paths collapse to `/static/*` and `/locales/*`. Unmatched paths
collapse to `<unmatched>`. `/metrics` itself is excluded so scrapes
don't trivially inflate the request rate.

### Scrape config

```yaml
scrape_configs:
  - job_name: ipsolis
    metrics_path: /metrics
    scrape_interval: 30s
    static_configs:
      - targets: ['ipsolis.your-host:8000']
```

### Ready-to-import dashboards + alerts

`docs/grafana/` ships a turnkey set:

* [`ipsolis-overview.json`](grafana/ipsolis-overview.json) — Grafana
  dashboard with 9 panels covering HTTP rate / errors / p95 latency,
  request rate by route, latency percentiles, orders by status, asset
  pool composition, Celery queue depth.
* [`prometheus-alerts.yaml`](grafana/prometheus-alerts.yaml) — 6 sample
  alert rules (high error rate, p95 latency, approval backlog, queue
  backlog warning + critical, pool near capacity). Drop into your
  Prometheus `rule_files`.
* [`grafana/README.md`](grafana/README.md) — import walkthrough +
  threshold rationale.

### Authentication

The endpoint has no built-in auth — restrict via reverse proxy when
exposed beyond the cluster perimeter:

```nginx
location /metrics {
    allow 10.0.0.0/8;        # internal monitoring net
    deny  all;
    proxy_pass http://api:8000/metrics;
}
```

### Business gauge cost

Gauges are refreshed on each scrape with three indexed `count GROUP BY`
queries against `orders`, `order_approvals`, and a join of
`asset_pool` + `asset_types`. At a 30 s scrape interval this is
negligible; cache layering would only help at sub-second scrape
intervals which Prometheus typically doesn't use.

---

## SIEM audit-log streaming (Splunk HEC + Microsoft Sentinel + Generic Webhook)

Every `audit_log` row (every order/asset/asset-type/approval mutation)
gets forwarded to a configured SIEM endpoint. Three adapters today:
**Splunk HEC**, **Microsoft Sentinel** (Azure Monitor Data Collector
API), and a **generic HMAC-signed JSON webhook** (Elastic, Datadog,
Sumo, Loki, anything that consumes signed JSON). New back-ends can be
added by implementing a new `build_*_payload` and `post_*` pair in
`worker/tasks/modules/siem_export.py` and dispatching on `siem.format`
in the streamer.

### Architecture

- **Beat task** — `tasks.workflows.siem_streamer.stream_audit_log` runs
  every minute and forwards new audit rows in batches of up to
  `siem.batch_size` (default 200).
- **Persistent cursor** — `siem.last_id` in `app_config` records the
  last audit row id successfully accepted by the SIEM. The streamer
  only advances the cursor after the SIEM acknowledges with a 2xx;
  on failure the cursor stays put and the next tick retries the
  same batch. At-least-once delivery is the contract — Splunk HEC
  dedupes on the event id we send, so duplicates are safe.
- **Status surface** — `GET /admin/config/siem/status` returns the
  cursor, current backlog (rows pending), last error message, and last
  successful batch timestamp. The Settings → Compliance UI shows
  these and refreshes on demand.

### Splunk HEC setup

1. **Splunk Web** → Settings → Data Inputs → HTTP Event Collector.
2. Create a new token. Sourcetype is automatically set by the
   streamer to `ipsolis:audit`; index can be left at the HEC default
   or set explicitly.
3. Copy the token. The endpoint URL is your Splunk collector,
   typically `https://splunk.example.com:8088/services/collector/event`.

### Microsoft Sentinel setup

ip·Solis uses the Azure Monitor **HTTP Data Collector API** (HMAC-signed
shared key). It's the simpler ingestion path — no service principal, no
Data Collection Endpoint / Data Collection Rule, no schema registration.
The custom log table (`{Log-Type}_CL`, default `IpsolisAudit_CL`) is
created in the Log Analytics workspace on first ingest. Microsoft
supports the Data Collector API through September 2026; a future slice
will add the newer Logs Ingestion API for installs that need it.

1. Azure portal → your **Log Analytics workspace** → *Settings → Agents
   → Log Analytics agent instructions*.
2. Copy **Workspace ID** and **Primary key** (or secondary key).
3. Decide on a *Log Type* — letters/digits only, ≤100 chars. Default
   `IpsolisAudit` materialises as `IpsolisAudit_CL` in Sentinel/KQL.

### Generic webhook setup

The generic webhook adapter targets any HTTPS receiver that accepts
a JSON array of events. ipSolis signs every batch with HMAC-SHA256
over the raw body and sends the digest in a header (default
`X-Hub-Signature-256: sha256=<hex>` — GitHub-compatible, so receivers
can reuse standard verification libraries). Tested shapes for
common back-ends:

* **Elastic ingest pipeline / generic HTTP** — point `webhook_url` at
  your `_bulk` or custom ingest endpoint; verify HMAC in the receiver.
* **Datadog Logs HTTP intake** — set `webhook_url` to
  `https://http-intake.logs.datadoghq.com/api/v2/logs`, add
  `{"DD-API-KEY":"<key>"}` to *Extra Headers*. Datadog ignores the
  HMAC header but ipSolis sends it anyway — set the secret to any
  value; receivers that don't verify simply discard it.
* **Sumo HTTP source** — `webhook_url` is the Sumo collector URL
  (already carries auth in the URL); HMAC adds defense in depth.
* **Loki Push API** — `webhook_url` is `/loki/api/v1/push`; the
  receiver expects a different body shape today, so this needs a
  small adapter helper for full integration. Plain webhook works
  for any custom receiver.

The receiver verifies the signature like this (Python):

```python
import hmac, hashlib
def verify(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

1. Spin up your receiver and capture its URL + HMAC secret.
2. (Optional) Decide which extra static headers it needs (Bearer
   token, vendor API key) and prepare a small JSON object.

### ipSolis setup

Admin UI → *Settings* → *Compliance* tab → *SIEM — Audit Log Streaming*:

1. Set *Mode* to `Enabled`.
2. Set *Format* to `Splunk HEC`, `Microsoft Sentinel`, or
   `Generic Webhook (HMAC-signed)`. The form swaps to the matching
   credential fields.
3. Paste the credentials — *Endpoint URL* + *HEC Token* (Splunk),
   *Workspace ID* + *Shared Key* + *Log Type* (Sentinel), or
   *Webhook URL* + *Shared Secret* + optional *Signature Header* /
   *Extra Headers (JSON)* (webhook).
4. Adjust *Batch size* and *Verify TLS* as needed (defaults are sane:
   200 / verify on).
5. Click **Save Settings**, then **Send Test Event** — a single
   synthetic `siem_test` event is posted; success means the SIEM
   accepts your payload format and authentication.
6. Enable the master switch and watch the live status panel:
   `Backlog: 41 rows pending` → `(caught up)` after the next
   minute's Beat tick.

### Stored config keys

| Key | Purpose | Stored as |
|---|---|---|
| `siem.enabled`          | `true`/`false` master switch | plain |
| `siem.format`           | `splunk_hec`, `sentinel`, or `webhook` | plain |
| `siem.endpoint_url`     | Splunk HEC endpoint URL | plain |
| `siem.token`            | Splunk HEC token | secret |
| `siem.workspace_id`     | Sentinel: Log Analytics workspace GUID | plain |
| `siem.shared_key`       | Sentinel: workspace shared key (base64) | secret |
| `siem.log_type`         | Sentinel: custom log table name (no `_CL` suffix; default `IpsolisAudit`) | plain |
| `siem.webhook_url`      | Webhook: HTTPS receiver URL | plain |
| `siem.webhook_secret`   | Webhook: HMAC-SHA256 shared secret | secret |
| `siem.webhook_signature_header` | Webhook: header name carrying `sha256=<hex>` (default `X-Hub-Signature-256`) | plain |
| `siem.webhook_extra_headers`    | Webhook: JSON object of additional headers (vendor API keys etc.) | plain |
| `siem.batch_size`       | Max events per minute (1–1000) | plain |
| `siem.verify_tls`       | Verify endpoint TLS cert | plain |
| `siem.last_id`          | Auto: last forwarded audit_log id | plain |
| `siem.last_error`       | Auto: most recent failure message | plain |
| `siem.last_success_at`  | Auto: ISO timestamp of last success | plain |

### Operational notes

- The streamer **never raises** — failed batches are logged at WARNING,
  recorded in `siem.last_error`, and retried next tick. The Beat
  scheduler is unaffected by individual ticks.
- If the cursor (`siem.last_id`) needs manual repositioning (e.g. you
  want to backfill from the beginning into a fresh Splunk index),
  set it directly via `PUT /admin/config/siem.last_id` with
  `{"value": "0"}`.
- Tamper-evidence at the database layer is enforced via three
  BEFORE-statement triggers on ``audit_log`` (DELETE / UPDATE /
  TRUNCATE) installed by migration ``0062``. Mutations fail with
  a descriptive error unless the session sets
  ``ipsolis.allow_audit_mutation = 'true'`` inside the transaction —
  a documented escape hatch for legitimate maintenance like
  retention pruning. The app's normal INSERT path is unaffected.

  ```sql
  -- Legitimate maintenance pattern:
  BEGIN;
  SET LOCAL ipsolis.allow_audit_mutation = 'true';
  DELETE FROM audit_log WHERE timestamp < NOW() - INTERVAL '7 years';
  COMMIT;
  ```

  Combined with SIEM streaming, this gives you defense in depth:
  the local row is hard to mutate quietly, and even if it were
  mutated the SIEM has the original copy outside the app's blast
  radius.

---

## Per-integration API tokens

Replaces the single shared `X-Admin-Key` with named, expiring,
revocable bearer tokens. Each token is stored only as a SHA-256 hash;
the raw value is shown to the admin **once** at creation and never
again. Tokens carry a recognisable `xpat_` prefix so they're easy to
spot in logs and config files.

### What you get

- **Per-integration credentials** — give ServiceNow, Prometheus,
  cron jobs, and one-off scripts each their own token instead of
  sharing one secret across the fleet.
- **Revocation** — disable a leaked or rotated token in one click;
  the row stays for historical audit attribution.
- **Last-used tracking** — see when each token was last accepted, so
  stale tokens are easy to clean up.
- **Expiry** — pick a horizon (30 / 90 / 180 / 365 / 730 days, or
  never). Past expiry the token returns 401 even if not explicitly
  revoked.
- **Audit attribution** — `request.state.actor` records `token:<name>`
  for tokenized requests; future work will surface this in audit log
  entries.

### Authentication paths

After this slice the admin auth dependency accepts any of:

1. `X-Admin-Key: <ADMIN_API_KEY>` — legacy env shared key, still works
   as a fallback so existing integrations don't break on upgrade.
2. Admin session cookie — the browser UI flow.
3. `Authorization: Bearer xpat_…` — the new per-integration tokens.

### Where to manage tokens

Admin UI → **API Tokens** in the left nav.

- Click **+ New token** → enter a name, pick an expiry, click Create.
- A one-time reveal banner shows the raw token. **Copy it now** — it
  is never shown again. The list afterward only shows the first six
  characters as a prefix, plus the SHA-256-derived row metadata.
- Click **Revoke** on any active row to disable it immediately.

### Stored fields

| Column | Purpose |
|---|---|
| `name` | Free-form label, used for audit attribution |
| `token_hash` | SHA-256 of the raw token (unique index) |
| `token_prefix` | First 6 chars of the raw token (UI display only) |
| `scopes` | JSON array — currently always `["admin:*"]`, scope decorators land in a follow-up slice |
| `created_by` | Actor that issued the token (e.g. `admin:legacy_key`, `admin:session:alice`) |
| `created_at` / `expires_at` / `last_used_at` / `revoked_at` | Lifecycle timestamps |

### Slice scope (what's in vs. what's not)

**In:** Table + ORM + create/list/revoke endpoints + Admin UI page +
Bearer header acceptance + last-used tracking + soft-delete revocation.

### Scopes

Each token carries a list of scopes that gate which endpoints it can
reach. The catalog lives in `app.utils.api_tokens.AVAILABLE_SCOPES`:

| Scope | What it allows |
|---|---|
| `admin:*` | Wildcard — full access. Equivalent to legacy `X-Admin-Key`. |
| `orders:read` / `orders:write` | List/view orders / create-update-cancel orders |
| `asset_types:read` / `asset_types:write` | List/view / edit asset definitions |
| `assets:read` / `assets:write` | View / manage the asset pool |
| `approvals:read` / `approvals:write` | View / decide on pending approvals |
| `audit:read` | Read the audit log |
| `config:read` / `config:write` | Read / modify application settings |
| `metrics:read` | Scrape the Prometheus `/metrics` endpoint |
| `webhook:in` | Inbound webhook receiver (ServiceNow et al.) |

When the bearer-token path is used, the request must carry every scope
the route declares. Missing scopes return **HTTP 403** with a
descriptive message listing the missing and granted scopes.

Legacy `X-Admin-Key` and admin sessions are intentionally
**unconstrained** — they implicitly carry `admin:*` so existing
integrations and the UI keep working on upgrade.

### How to gate an endpoint

```python
from app.utils.auth import require_scopes

@router.get(
    "/audit-log",
    dependencies=[require_scopes("audit:read")],
)
async def list_audit_log(...): ...
```

Combine multiple scopes by passing several positional args; all are
required (`require_scopes("orders:read", "audit:read")`).

### Endpoints scoped today

A representative set is gated to demonstrate the wiring; the rest still
accept any authenticated bearer token:

| Endpoint | Required scope |
|---|---|
| `GET /admin/audit-log` | `audit:read` |
| `POST/PUT/DELETE /admin/asset-types/*` | `asset_types:write` |
| `POST /admin/asset-types/{id}/clone` | `asset_types:write` |
| `GET /admin/cost-report` | `orders:read` |

Adding scopes to the rest of the admin surface is mechanical
(decorator-only); rolling them out gradually keeps the back-compat
guarantee intact.

### ServiceNow webhook — bearer-token auth

`POST /webhook/servicenow` accepts **either** of two auth paths
(checked in this order):

1. `Authorization: Bearer xpat_…` with the `webhook:in` scope. Issue
   the token from Admin UI → API Tokens with only `webhook:in` ticked,
   give it a recognisable name (e.g. `servicenow-int`), copy the raw
   token once, paste it into the ServiceNow integration's HTTP-headers
   config. Revoke it from the UI to instantly cut access.
2. `X-Hub-Signature-256: sha256=<HMAC>` against `WEBHOOK_SECRET_TOKEN`
   from `.env`. Kept for back-compat with existing integrations.

The audit log records which path authenticated each request:

| Auth path | `triggered_by` value |
|---|---|
| Bearer token | `api:servicenow_webhook (webhook:token:<name>)` |
| HMAC fallback | `api:servicenow_webhook (webhook:hmac)` |

So when "who triggered this?" comes up in a compliance review, the
audit trail names the specific token (and therefore the specific
integration) instead of just the catch-all `WEBHOOK_SECRET_TOKEN`.

### Audit attribution everywhere

The same attribution applies to **every** mutating admin endpoint:
asset definitions, asset pool, configuration. Each `audit_log` row's
`triggered_by` now carries the route label *and* the calling
credential, formatted as `api:<route> (<actor>)`:

| Caller | Actor portion |
|---|---|
| Per-integration bearer token | `token:<name>` (e.g. `token:servicenow-int`) |
| Admin session (browser UI) | `admin:session:<user>` |
| Legacy `X-Admin-Key` from `.env` | `admin:legacy_key` |
| ServiceNow webhook with HMAC | `webhook:hmac` |

The actor lookup goes through `actor_by(request, label)` in
`app.utils.audit`, which reads `request.state.actor` (populated by
`require_admin_key` and `_authenticate_webhook`). Routes that don't
have an authentication context fall back to plain `api:<label>` so
the helper is safe to use everywhere.

Sample rows after a token write followed by a legacy-key write:

```
id  | action  | triggered_by
----+---------+------------------------------------------------
804 | updated | api:update_config (admin:legacy_key)
803 | updated | api:update_config (token:audit-attrib-test)
```

**Not yet:**

- Wider rollout of scope decorators (only the most commonly-integrated
  endpoints carry them today; the rest still accept any authenticated
  bearer token regardless of scope).

---

## Cost report / chargeback

Three optional fields on every asset definition feed a monthly cost
report:

| Field | Purpose |
|---|---|
| `monthly_cost` | Per-instance monthly cost (e.g. an M365 E5 seat at €12.50) |
| `currency` | ISO 4217 code (defaults to EUR; USD/GBP/CHF/JPY/CAD/AUD/SEK/DKK/NOK/PLN supported in the dropdown) |
| `cost_center` | Free-form label, e.g. `CC-IT-2100`, `RnD/Platform` |

Definitions without `monthly_cost` are excluded from the report so
legacy entries don't surface as 0 €.

### What the report shows

Admin UI → **Cost Report** in the left nav (`/ui/cost-report`):

- **Summary cards** at the top — projected monthly spend per
  (cost center × currency).
- **Detail table** — one row per (cost center, asset definition):
  unit cost, active orders, unique users, projected monthly total.
- **Download CSV** button — `GET /admin/cost-report?fmt=csv` returns
  a CSV the same data spreadsheet-friendly.

### What "active" means

Counts every order in `pending`, `pending_approval`, `scheduled`,
`processing`, `provisioning`, `provisioned`, or `delivered` status —
the same set used by capacity / per-user quota enforcement.
Cancelled, rejected, expired, revoked, and failed orders never count.

### Where to set the fields

Admin UI → *Asset Definitions* → edit a definition → **Cost & Chargeback**
section (between Classification and Lifecycle). Saved via the same
`PUT /admin/asset-types/{id}` endpoint, picked up by the next report
load.

### Useful patterns

- **Personal VDIs** — set `monthly_cost` to the actual hosting cost
  per VM, `cost_center` to the user's department. Report shows
  exactly which department is consuming how much VDI capacity.
- **SaaS license types** — set `monthly_cost` to per-seat list price,
  `cost_center` to the function paying the bill (HR for Workday,
  Finance for Concur, IT for collaboration tools).
- **Pooled licenses** — `active_orders` reflects current usage out of
  `pool_capacity`; multiplying gives a usage-adjusted projected
  monthly total rather than just the negotiated maximum.

### Consumer breakdown — AD-driven chargeback

The cost center on the asset definition tells you who **provides** the
service. To see who **consumes** it, ipSolis also snapshots a
configurable set of AD attributes from the requester onto each order
at creation time:

| Order column | Default AD attribute | Override config key |
|---|---|---|
| `requester_sam_account` | `sAMAccountName` | (not configurable — always sAM) |
| `requester_department`  | `department`       | `ad.attribute.department` |
| `requester_cost_center` | _(blank by default — set it)_ | `ad.attribute.cost_center` |
| `requester_company`     | `company`          | `ad.attribute.company` |
| `requester_employee_id` | `employeeID`       | `ad.attribute.employee_id` |
| `requester_title`       | `title`            | `ad.attribute.title` |

Where to configure:

> Admin UI → *Settings* → *Active Directory* → **AD Attribute Mapping (Chargeback)** card.

Most enterprises store cost centers in a custom attribute (commonly
`extensionAttribute1` through `extensionAttribute15`); set the
`cost_center` mapping accordingly and new orders pick it up
immediately. Existing orders remain unchanged — the snapshot is the
historical truth, even if the user later moves teams.

The cost report dashboard then offers three views:

| View | Aggregation key | Question it answers |
|---|---|---|
| **By provider** | asset definition's `cost_center` | "What is each platform team producing?" |
| **By consumer cost center** | requester's `requester_cost_center` | "What is each business unit consuming?" |
| **By department** | requester's `requester_department` | "How does spend split by department, regardless of cost-center taxonomy?" |

### CSV format

`GET /admin/cost-report?fmt=csv` returns one row per active order with
the full requester snapshot, perfect for finance / HR reconciliation:

```
Order ID, Status, Created at, Requested from, Requested until,
User email, User name, sAMAccountName, Employee ID, Title,
Department, Requester cost center, Company,
Asset type, Provider cost center, Currency, Unit monthly cost, Monthly total
```

Open in Excel / Sheets and pivot however you need — by `Department`,
by `Asset type`, by `Provider cost center`, or any combination.

### Operational notes

- AD lookup at order creation is best-effort: if AD is unreachable or
  the user can't be resolved, the order is still created (so users
  aren't blocked). Affected fields stay NULL on that order.
- Renew (`MODIFY`) and cancel (`DELETE`) orders inherit the snapshot
  from the original provision order — re-querying AD on every status
  change would risk drifting the snapshot if the user changed teams.
- Attribute names are read from `app_config` on every request, so
  rotating from `department` to a custom attribute lands on the next
  order without restart.
- The same snapshot helper (`app.utils.ad_lookup.snapshot_requester_attrs`)
  runs on **all three creation paths** — self-service portal, public
  `POST /orders/`, and the ServiceNow webhook — so externally-driven
  orders feed the same consumer-side rows the portal does.

---

## Field-level data classification

Tag each user-supplied attribute on an asset definition as
`internal`, `pii`, `phi`, or `pci`. The portal renders matching
warning badges (amber for PII, red for PHI / PCI, neutral for
`internal`) next to the field on the order form, and the
classification flows into every audit row touching that asset type.

### What it changes

- **Portal**: requesters see at a glance which fields are sensitive
  before they fill them in.
- **Audit log**: each row's `classification` column is set at
  *write time* from the strictest class declared on the touched
  asset type's attributes (PCI > PHI > PII > internal). Writing-time
  classification freezes the regulatory category against subsequent
  attribute edits — the row's class is determined by the type's
  state at the moment of the audited change, not at prune time.
- **Per-class retention** (see [Tamper-evident audit log + retention](#tamper-evident-audit-log--retention))
  uses the column to keep PII / PHI / PCI rows for a longer window
  than routine config changes.

### Where to set it

Admin UI → *Asset Definitions* → edit a definition → in each
attribute row, pick a value from the **Classification** dropdown.
Unset = `internal` (the default).

---

## Per-order cost projection on the portal

When the asset definition is priced (see
[Cost report / chargeback](#cost-report--chargeback)), the portal's
order detail page renders the projected total for the request:

```
Monthly cost      12.50 EUR
Projected total   37.04 EUR    ← (hover for "90 days · 2.96 months")
```

Hidden when the asset type has no `monthly_cost` set, or when the
order has no `requested_from`/`requested_until` window. Months use
the calendar average (30.4375 days per month) so the figure
matches the per-order CSV that finance pivots.

No configuration — appears automatically once `monthly_cost` is
populated.

---

## Approval escalation

When an approval has burned through all its reminders without a
decision, ip·Solis fires **one** notification to the configured
escalation contact(s) so an operator can intervene. Each approval
escalates *at most once*; subsequent ticks ignore already-escalated
rows.

### Where to configure

Admin UI → *Settings* → *E-Mail* tab → *Approval Reminders* →
**Escalation contact(s)** field. Comma-separated email addresses;
leave blank to disable escalation entirely.

### Stored config keys

| Key | Purpose |
|---|---|
| `approval.escalation_email` | Comma-separated escalation contacts (blank disables) |

### Operational notes

- Triggered when `reminder_count >= max_reminders` AND
  `escalated_at IS NULL`. The same Beat task that nudges
  reminders also handles escalations in a single tick.
- Approval URL in the escalation email points the contact at the
  admin UI's `/ui/orders` page, not a signed-token approve page —
  the escalation contact intervenes operationally rather than
  deciding on the approver's behalf.
- The seeded `approval_escalated` email template carries the full
  variable set (original approver name+email, requester, asset,
  reminder count, etc.) — customise it via *Settings → E-Mail
  Templates* (Enterprise license).

---

## Approval delegation (admin + portal self-service)

Re-route approval requests to a deputy while the assigned approver
is out. Two surfaces, identical mechanics:

| Surface | Who manages it | Use when |
|---|---|---|
| Admin UI → `/ui/approval-delegations` | Admins / helpdesk | Setting up delegations on behalf of users |
| Portal → `/portal/delegations` | The approver themselves | Self-service "I'm on vacation" |

### Where to configure

- **Admin**: *Approval Delegations* in the left nav. New
  delegation modal asks for approver email + delegate email +
  from/until window + optional reason.
- **Portal**: the user clicks *Delegations* under *My Approvals*
  (visible only to users who have ever had at least one
  approval). The form is pre-filled with their identity and
  cannot be tampered with — server-side coercion enforces that
  every portal-driven write addresses the SSO-authenticated user.

### Behaviour

- Applied at order-creation time. New approvals during the
  window are addressed to the delegate; existing in-flight
  approvals are not retroactively re-routed.
- The most-recent matching active delegation wins. Revoked or
  out-of-window delegations are ignored.
- Audit trail: every create/revoke records who set up the
  delegation. Portal-driven rows show
  `portal:user:<email>`; admin-driven rows show
  `admin:session:<user>:<role>`.
- Cross-user portal revoke attempts return 404 (not 403) so
  the existence of someone else's delegation isn't leaked.

### Stored config keys

None — delegation rows live in the `approval_delegations` table.

---

## N-of-M approvals + conditional rules

Two complementary controls that together cover most real-world
approval policies without code.

### N-of-M

Set `min_approvals_required` per asset definition so any N of M
configured approvers can satisfy the order:

| `min_approvals_required` | Behaviour |
|---|---|
| `NULL`, `0`, or ≥ total approvers | "All required" (legacy default) |
| Positive integer < total | "Any N of M" — first N approves wins |

Once the threshold is met, the remaining pending approvals
transition to status `superseded` so they disappear from pending
lists, no longer attract reminders / escalations / auto-decline,
and can't be acted on retroactively. **Decline still vetoes
regardless of N** — a single rejection always rejects the order.

### Conditional rules

JSONB rule list per asset definition adds extra approvers when a
condition matches the order. Rules:

```json
{
  "name": "EU cost center: compliance must approve long-running",
  "condition": {
    "op": "and",
    "clauses": [
      {"field": "duration_days",       "op": ">",       "value": 30},
      {"field": "attr.cost_center",    "op": "contains","value": "EU"}
    ]
  },
  "approvers": [
    {"email": "compliance@example.com", "name": "Compliance"}
  ],
  "min_approvals_required": 1
}
```

- **Condition shape**: leaf `{field, op, value}` or compound
  `{op: "and"|"or"|"not", clauses: [...]}` nested up to 8 levels.
- **Built-in fields**: `duration_days`, `monthly_cost`,
  `has_pii`, `has_phi`, `has_pci`, `requester_department`.
- **Custom-attribute fields**: `attr.<key>` for any user-supplied
  attribute on the asset type's `config`. Auto-suggestions in the
  rule builder come from the asset type's own attribute list.
- **Operators**: `>`, `>=`, `<`, `<=`, `==`, `contains`.
- **Per-rule N-of-M**: optional `min_approvals_required` on the
  rule itself. Rule-driven approvers form an isolated quorum
  group; manager / owner / no-quorum-rule approvers fold into the
  asset-type-level group. The order is unblocked only when *every*
  group meets its threshold.
- **Malformed rules** (typo in `op`, unknown `field`) are logged at
  WARNING and skipped — a hand-edited JSON typo can never block
  order creation.

### Where to configure

Admin UI → *Asset Definitions* → edit a definition → **Approval**
section → rule builder card. The rule editor shows a free-text
field input with an autocomplete datalist of built-in fields plus
all `attr.*` keys from the asset type. Deeply-nested compounds (3+
levels) round-trip correctly but the simple card editor only edits
top-level clauses; an "edit JSON" mode is on the roadmap.

---

## Auto-decline on extended inactivity

Opt-in policy: pending approvals past a configurable age are
declined by the system on the requester's behalf, marking the
order as `rejected` and emailing the requester with the configured
message. Closes the third lever in the staleness story alongside
reminders and escalation.

### Behaviour

- **Cadence**: daily Beat task at 03:30 Europe/Berlin
  (`tasks.workflows.approval_auto_decline.scan_and_auto_decline`).
- **Selection**: pending approvals where
  `created_at < NOW() - auto_decline_after_days days` AND the
  parent order isn't already `rejected` / `cancelled`. At most one
  stale approval per order is declined per tick (`DISTINCT ON
  (order_id)`); the existing veto-on-decline semantics propagate
  the rejection to the order, so handling siblings in the same
  tick would just write redundant audit rows.
- **Effects**: approval row → `status='declined'` + `decided_at` +
  the configured comment; order → `status='rejected'` +
  populated `error_message`; two audit rows (`order_approval` +
  `order`) attributed to `system:auto_decline`; rejection email
  queued via the existing `send_approval_result_email` task so the
  requester gets the same message a human-driven decline produces.
- **Off by default** — leave `auto_decline_enabled = false` or
  `auto_decline_after_days = 0` to skip the scan entirely.

### Where to configure

Admin UI → *Settings* → *E-Mail* tab → *Approval Reminders* →
**Auto-decline (opt-in)** sub-card:

| Field | Default | Notes |
|---|---|---|
| Status | Disabled | Master switch — `Enabled` activates the Beat task |
| Decline after (days) | 0 | Counted from the approval row's `created_at`. 0 also disables |
| Decline reason | "Auto-declined: no decision recorded …" | Recorded on the approval + included in the rejection email |

### Stored config keys

| Key | Purpose |
|---|---|
| `approval.auto_decline_enabled` | `true`/`false` master switch |
| `approval.auto_decline_after_days` | Days a pending approval may sit before system-decline |
| `approval.auto_decline_message` | Decline reason text (operator-customisable) |

### Sensible cadences

A typical end-to-end staleness flow on a 14-day window:

```
Day 0 — Approval created, initial email + Teams card sent.
Day 1 — Reminder (1) — same channel mix.
Day 2 — Reminder (2).
Day 3 — Reminder (3) — cap reached, no further nudges.
Day 4 — Escalation email fires once to escalation contact.
Day 4 → 14 — Silent (operator handles via escalation).
Day 14 — Auto-decline fires; order → rejected; requester emailed.
```

Tune `reminder_after_hours`, `max_reminders`,
`escalation_email`, and `auto_decline_after_days` to match your
internal SLA policy.

---

## OpenTelemetry tracing (api + worker)

Auto-instrumented FastAPI requests, SQLAlchemy queries, and Celery
tasks flow through an OTLP HTTP exporter to any standard collector
(Jaeger, Tempo, SigNoz, Honeycomb). A request that dispatches a
runbook produces a single distributed trace spanning api + worker
when both sides point at the same collector.

### What gets traced

- **API** (`service.name = ipsolis-api`): every HTTP request,
  every SQLAlchemy statement.
- **Worker** (`service.name = ipsolis-worker`): every Celery task
  invocation, every SQLAlchemy statement.
- **Distributed trace context** is propagated across the Celery
  message boundary, so an http-dispatched runbook stitches into
  one trace.

### Where to configure

Admin UI → *Settings* → *Compliance* tab → **OpenTelemetry
Tracing** card:

| Field | Purpose |
|---|---|
| Status | Master switch (off by default) |
| Service name | Defaults to `ipsolis-api` (worker auto-suffixes `-worker`) |
| OTLP endpoint | Your collector's `/v1/traces` URL |
| Headers | JSON object — additional OTLP headers (vendor API key etc.) |
| Console exporter | Diagnostic only — emits spans to api/worker stdout |

### Operational notes

- Wires up at api/worker startup, so changes here require a
  restart (`docker compose restart api worker`).
- Console-exporter mode is for local verification — never enable
  it in production, span volume on stdout will eat your log
  pipeline.
- Pinned to OTel 1.29.0 / 0.50b0 for HTTP transport (avoids the
  grpcio compile dependency).

### Stored config keys

| Key | Purpose | Stored as |
|---|---|---|
| `otel.enabled` | `true`/`false` master switch | plain |
| `otel.service_name` | API service name (worker uses `<name>-worker`) | plain |
| `otel.endpoint` | OTLP HTTP traces endpoint | plain |
| `otel.headers` | JSON object of additional OTLP headers | secret |
| `otel.console_exporter` | Diagnostic-only stdout span dump | plain |

---

## Tamper-evident audit log + retention

Two layers, defense in depth:

1. **Tamper-evident triggers** (migration `0062`) — three
   BEFORE-statement triggers on `audit_log` (DELETE / UPDATE /
   TRUNCATE) raise an exception unless the transaction sets
   `ipsolis.allow_audit_mutation = 'true'` via `SET LOCAL`. Even
   an operator with full DB credentials can't quietly mutate the
   table; errors are loud and self-documenting.

2. **Retention pruning** — daily Beat task at 03:00 Europe/Berlin
   uses the documented bypass to delete rows past the configured
   window. Per-classification windows let PII / PHI / PCI rows
   keep a longer retention than routine config changes.

### Per-classification retention

Each audit row carries a `classification` column set at
write-time from the strictest class declared on the touched asset
type's attributes (`pci > phi > pii > internal`). The prune Beat
task iterates buckets:

| Window | Applies to | Default |
|---|---|---|
| `retention.audit_log_days` | `internal` + NULL rows | 0 (disabled) |
| `retention.pii_days` | rows classified `pii` | 0 (disabled) |
| `retention.phi_days` | rows classified `phi` | 0 (disabled) |
| `retention.pci_days` | rows classified `pci` | 0 (disabled) |

Each bucket runs in its own transaction with the bypass GUC, so
a single huge bucket can't starve the others.
**Per-class windows do not fall back to the global default** when
set to 0 — explicit opt-in to retention so PII / PHI / PCI rows
are never accidentally dropped under the catch-all.

### Status surface

`retention.last_run_at`, `retention.last_pruned`, and
`retention.last_pruned_by_class` (JSON breakdown) are kept in
`app_config` for ops visibility — Admin UI → *Settings* →
*Compliance* → *Audit Log Retention* card.

### Manual maintenance pattern

```sql
BEGIN;
SET LOCAL ipsolis.allow_audit_mutation = 'true';
DELETE FROM audit_log WHERE timestamp < NOW() - INTERVAL '7 years';
COMMIT;
```

The `SET LOCAL` is transaction-scoped; the next BEGIN reverts to
default-deny.

### Combined with SIEM streaming

If you also stream to a SIEM (see
[SIEM audit-log streaming](#siem-audit-log-streaming-splunk-hec--microsoft-sentinel--generic-webhook)),
the local row is hard to mutate quietly, and even if it were
mutated the SIEM has the original copy outside the app's blast
radius. Tamper-evidence + external streaming + tight retention
windows is a defensible compliance posture for ISO 27001 / SOX /
PCI audits.

---

## Admin RBAC (roles, ACL grants, SoD, password policy)

Five-tier role ladder backed by per-user accounts in `admin_users`
(PBKDF2-SHA256 / 600 k iterations, stdlib-only, no bcrypt /
passlib build dependency). Replaces the single shared
`ADMIN_API_KEY` for everyone except a back-compat fallback path.

### Role ladder

```
superadmin > admin > approver > auditor > helpdesk
```

- **superadmin** — admin user CRUD, license upload, seed export,
  initial setup, API token issuance, all admin role privileges.
- **admin** — operational config (modules, runbooks, asset types,
  maintenance, approval delegations), approval delegation create.
- **approver** — decide on pending approvals.
- **auditor** — read-only access to audit log + cost report +
  maintenance read paths (backups list, retention status, queue
  depth) — granted in slice 4.
- **helpdesk** — placeholder for future minimal-permission
  troubleshooting paths.

`role_at_least(actual, required)` in `app.utils.rbac` is the
single source of truth; every other role check delegates to it.

### First-run setup

When `admin_users` is empty, the login page renders a "Create
first administrator" form instead of the sign-in form. Submitting
it creates the first superadmin and auto-logs them in. Idempotent
against races (re-checks the count on the setup POST).

### Legacy back-compat

`ADMIN_API_KEY` from `.env` continues to work as a virtual
superadmin — set `username` blank and `password = ADMIN_API_KEY`
on the login page. Audit attribution is `admin:legacy_key` so
auditors can spot when the fallback path was used. Existing
scripts and bookmarked admin sessions don't break on upgrade.

### Per-asset-type ACL grants

Scope individual `admin` users to a subset of asset types. Granting
an admin even one type flips them into "see only granted types"
mode:

- The admin UI list filters automatically.
- Out-of-scope `PUT/DELETE/clone` returns **404** (same shape as
  a missing id) so the existence of unrelated teams' types isn't
  leaked.
- Auto-grant on create — when a scoped admin creates a new asset
  type, the grant is added inside the same transaction so they
  don't lose visibility on their own creation.
- Zero grants = back-compat "see all" so single-team installs
  aren't surprised by the new feature.

`superadmin` / `approver` / `auditor` / `helpdesk` always bypass
scoping — their role-level read or no-asset-type concerns make
type-level fencing pointless.

### Separation of duties (SoD)

A user who configured an asset type cannot also approve their own
access requests against it. Detection walks the audit log for
matching `created` / `updated` / `cloned` rows attributed to the
approver (matched on email, local-part, or admin username).

- Fires on **approve only** — declines stay open since rejecting
  your own work is always allowed.
- Blocked approver gets HTTP 409 with the original config-time
  audit attribution quoted back; the approval row stays `pending`
  so a different approver can decide.
- Per-rule opt-out: `approval_rules` JSON entries accept
  `sod_exempt: true` for compliance-officer style "this approver
  is also an admin and that's OK" scenarios. Captured in the
  `order_approvals.sod_exempt` column at order-creation time so
  subsequent rule edits don't shift past orders' SoD logic.
- SoD *enforcement* is itself an Enterprise feature; community
  installs get the audit-trail breadcrumb (warning log) but the
  decision is allowed to proceed.

### Bearer-token role binding

API tokens may be issued with a specific role on top of their
scopes. Role-gated routes consult the token's role too:

| Token role | Effect |
|---|---|
| `NULL` (default) | Pre-slice-3 scope-only authz (back-compat) |
| `superadmin` / `admin` / etc. | Standard role-ladder check via `role_at_least` |

**Mint guard**: a creator can only issue tokens at or below their
own role. A non-superadmin attempting to mint a superadmin token
gets HTTP 403 with a descriptive message; no privilege escalation
via token issuance.

### Self-service password change

Admin UI → *My Account* (`/ui/my-account`). Requires the current
password as a liveness check; new password must differ from the
current and be ≥12 chars. Legacy `ADMIN_API_KEY` actors get a
clear HTTP 409 directing them to rotate via `.env`. Audit row is
`password_changed_self` with no value content (no plaintext leak).

### Password policy + lockout

| Config key | Default | Purpose |
|---|---|---|
| `rbac.password_rotation_days` | 0 (off) | Force rotation after N days since `password_set_at` |
| `rbac.lockout_threshold` | 0 (off) | Lock the account after N failed login attempts |
| `rbac.lockout_duration_minutes` | 0 (off) | Auto-unlock after this many minutes since `locked_at` |

All three default to off so existing installs are unchanged.
Settings UI section in the *Compliance* tab; values writable on
community but **enforcement gated on the `password_policy`
Enterprise feature key**.

Lockout responses use HTTP 423 with an "unlock at <UTC>" hint.
Auto-unlock fires on the next attempt past the duration window so
brief flurries clear themselves. Superadmin password reset =
unlock + clock reset.

### Audit attribution

`triggered_by` carries both *who* and *with what authority*:

| Caller | Audit attribution |
|---|---|
| Admin session | `admin:session:alice:superadmin` |
| Per-integration bearer token | `token:<name>` |
| Legacy `ADMIN_API_KEY` | `admin:legacy_key` |
| Webhook bearer | `webhook:token:<name>` |
| Webhook HMAC fallback | `webhook:hmac` |
| Portal user | `portal:user:<email>` |
| Anonymous portal | `portal:anonymous` |
| Signed approval token | `api:approval_token (approver:<email>)` |
| System (auto-decline, retention prune, SIEM streamer) | `system:auto_decline`, etc. |

Filter on the `triggered_by` column in the audit log viewer to
isolate one credential's activity.

---

## External secret management (HashiCorp Vault + CyberArk CCP/AIM)

Replace plaintext credentials in `app_config` with references to
your secret store. Any secret-typed config row whose value is
`vault://<path>[#<field>]` or `ccp://[<safe>/]<object>` is
resolved at read time via the configured backend. Plain string
values keep working unchanged so partial migrations are safe.

### Reference grammar

```
vault://ipsolis/ad/password               # KV v2, default field "value"
vault://ipsolis/ad/password#bind_dn       # KV v2, custom field "bind_dn"
ccp://OperationsSafe/sccm-svc             # CyberArk CCP with explicit Safe
ccp://vsphere-svc                         # CCP with default Safe from config
```

### Where it kicks in

| Credential | Location | Resolved by |
|---|---|---|
| AD bind password | `ad.password` | API |
| Entra ID client secret | `entra.client_secret` | API |
| SMTP password | `smtp.password` | API |
| vSphere admin password | `vsphere.password` | API + worker |
| XenServer admin password | `xenserver.password` | API + worker |

The worker mirror at `worker/tasks/modules/secrets.py` is sync-only
(same boundary as `audit_helper.py`) so the worker stays free of
api package imports.

### Process-local TTL cache

Default 60 s, configurable via `secret.cache_ttl_seconds`. Keyed
by `(backend, reference)`. Avoids hammering the secret store on
every config read.

### Vault setup

| Config key | Purpose |
|---|---|
| `secret.backend` | `vault` |
| `secret.vault.url` | e.g. `https://vault.example.com:8200` |
| `secret.vault.token` | Static token (slice 1) — AppRole/JWT in slice 2 |
| `secret.vault.kv_mount` | KV mount path, default `secret` |
| `secret.vault.namespace` | Optional Vault Enterprise namespace |

KV v2 envelope is unwrapped automatically (`data.data.<field>`).

### CyberArk CCP setup

| Config key | Purpose |
|---|---|
| `secret.backend` | `ccp` |
| `secret.ccp.url` | e.g. `https://aim.example.com` |
| `secret.ccp.app_id` | AppID configured in PVWA |
| `secret.ccp.default_safe` | Default Safe for `ccp://<object>` references |
| `secret.ccp.client_cert_pem` | Optional mTLS PEM (cert + key, materialised to a 0600 temp file just for the request duration) |
| `secret.ccp.verify_tls` | Verify endpoint TLS cert |

Authentication is AppID + IP allow-list (the standard CCP install)
or optional mTLS via the configured client cert.

### Test connection

`POST /admin/config/secret-backend/test` clears the process cache,
hits the right probe (`/v1/sys/health` for Vault, `/api/Verify`
for CCP), and stamps `secret.last_test_at` on success or
`secret.last_test_error` on failure. Visible inline in the
*Settings → Compliance → External Secret Backend* card.

### Failure semantics

Backend failures (network / auth / missing path) log at WARNING and
**return empty string** — fail-closed-quiet so a Vault outage
doesn't crash unrelated requests. The calling integration's own
auth-failure error is the user-visible signal.

### Masking exception

`GET /admin/config/<key>` masks secrets as `***` by default, **but**
reference-shaped values (`vault://…`, `ccp://…`) stay in clear so
admins can see *which* store entry each row points to. Knowing the
path doesn't grant access. Genuine secrets
(`secret.vault.token`, `secret.ccp.client_cert_pem`) are still
masked.

### Slice 2 (queued)

CyberArk Conjur, AWS Secrets Manager, Azure Key Vault adapters;
Vault AppRole / Kubernetes-JWT auth methods; CCP mTLS bootstrap UX;
one-shot migration tool that walks every `is_secret=true` row and
writes the value into the chosen backend. Track in *Deferred
Enterprise Backlog* (top of `TASKS.md`).

---

## PowerShell modules — Linux compatibility

The worker runs **PowerShell 7 on Linux** in the worker container,
but many PSGallery modules ship with `PSEdition_Desktop` (Windows
PowerShell 5.1) only and won't load. Operators declare each
module's compatibility when adding it; the modules table shows the
flag and lets admins click any badge to cycle the value.

### Compatibility states

| Badge | Value | Meaning |
|---|---|---|
| `Linux ✓` | `core` | Operator-marked Linux-compatible (PowerShell 7 / Core) |
| `Windows only ✕` | `desktop_only` | Windows PowerShell 5.1 only — will not load on the Linux worker |
| `Unverified ?` | `unknown` | Operator hasn't declared yet (default for back-compat rows) |

### Where to set it

Admin UI → *PS Modules* → **Add module** form has a *Linux
compatibility* dropdown (visible for both Gallery and Upload
sources). On the modules table, click any compatibility badge to
cycle through the three states.

### Why no PSGallery search / probe

We deliberately don't query PSGallery for tag-derived
auto-detection. Two reasons:

1. **Most ip·Solis installs are air-gapped** — no outbound
   internet from the api / worker containers. A search-driven
   feature would be useless in those environments.
2. **Cloud PSGallery's `Search()` endpoint times out on popular
   modules** (e.g. `VMware.PowerCLI` with `$top=20` exceeds a 12 s
   budget) and `IsLatestVersion` filters return zero results when
   combined with `searchTerm`. Manual operator declaration is
   faster, deterministic, and works everywhere.

### Stored fields

`ps_modules.compatibility` (added by migration `0077`).
Default `unknown` for back-compat with existing rows; the next
add or inline cycle populates it.

---

## HA Beat scheduler (multi-replica with celery-redbeat)

Drop-in `celery-redbeat` swap moves the Celery Beat schedule into
Redis with a Lua-script distributed lock. Run multiple Beat
replicas side-by-side; only the lock-holder dispatches.

### Why this matters

Single-Beat is a single point of failure for every Celery
periodic task — health probes, SIEM streamer, retention prune,
license expiry check, approval reminders + auto-decline, scheduled
order dispatcher, backup scheduler, update notifier. A crash
silently stops everything until someone notices.

### How to run multiple replicas

```bash
docker compose up -d --scale beat=2
```

Both replicas race for the Redis lock; the loser polls until it
can take over.

### Failover timing

| Setting | Value | Purpose |
|---|---|---|
| `redbeat_lock_timeout` | 30 s | How long a dead lock survives in Redis before another replica can claim it |
| `beat_max_loop_interval` | 30 s | Caps the non-leader poll cadence so failover happens within ~lock-TTL |

Verified: SIGKILL on the leader → other replica acquired the lock
in **13 seconds** with the tuned timings. Default RedBeat polls
only every 5 min, which yields ~5-min failover and isn't really HA.

### What survives a restart

The schedule lives in Redis (`ipsolis:redbeat:` key prefix) so
stop/start of all Beat replicas doesn't lose schedule state. The
static `app.conf.beat_schedule` dict in `worker/tasks/__init__.py`
is re-ingested by RedBeat on first start and re-synced on every
restart, so schedule edits ship via container rebuild as before.

### Idempotence

Existing Beat tasks audited for "at-most-once-but-rarely-twice"
semantics during the lock-handover window:

- SIEM streamer's cursor-advance is idempotent (cursor only moves
  on 2xx).
- Retention prune is deterministic on the time cutoff.
- License-expiry mailer dedupes via the `license.last_warning_*`
  cursors.
- Approval reminders / auto-decline guard via `last_reminded_at` /
  status-already-changed checks.
- `check_backup_schedule` dedupes per-minute via a `db_backups`
  row query.

Handover window is sub-second on clean restart and ≤30 s on hard
kill, so duplicate-dispatch risk is limited to the very narrow
lock-handover window.

### Multi-tenancy on a shared Redis

`redbeat_key_prefix="ipsolis:redbeat:"` namespaces the schedule
keys so multiple ip·Solis tenants on a shared Redis don't collide.
Override via `redbeat_key_prefix` in `worker/tasks/__init__.py` if
you run more than one tenant on the same Redis.

---

## Setup checklist + pool capacity warnings on the dashboard

Two opt-in dashboard widgets that surface operational problems
before they bite.

### Setup checklist

`GET /admin/setup/state` returns 9 checklist items (6 essential,
3 recommended), each with `done` / `label` / `hint` / `link` /
`tier`. The dashboard renders them as a card with a circular
progress ring and percent badge.

| Tier | Items |
|---|---|
| Essential | App branding, SMTP, AD, Entra ID, asset definitions exist, asset pool has assets |
| Recommended | Teams card delivery, SIEM streaming, per-integration API token issued |

State is auto-derived from the current DB — *not* a one-time
"setup wizard", so deleting the only asset definition flips the
relevant item back to ☐. Each pending row is a direct link to the
relevant settings tab anchor.

**"Hide until next setup change"** persists a signature of the
current done-state in `localStorage`. If the state later changes
(regression or new config), the signature mismatch re-shows the
card.

### Pool capacity warnings

Surfaces capacity pressure before users hit a 409 from per-pool
quota enforcement. Renders inside the existing
`fragments/pool_summary.html` so it participates in the dashboard
auto-refresh path.

| Severity | Threshold | Color |
|---|---|---|
| `warning` | ≥80% fill | amber |
| `critical` | ≥95% fill | red |

Per-pool fill is computed in two batched queries (no N+1 regardless
of catalog size):

- `assigned_personal` / `dedicated_shared` — anything not in `Free`
  status counts as a consuming slot (busy, reserved, maintenance,
  Failed, Reinstall).
- `capacity_pooled` — count active orders against `pool_capacity`
  using the same status set as quota enforcement.

Each warning row is a clickable link — `pooled` types link to the
asset-definition edit page (where capacity is configured);
`personal` / `shared` types link to the asset-pool list filtered
to that type.

Inactive asset definitions are excluded — they can't accept new
orders so flagging them as "full" is noise.

### No configuration

Both widgets are auto-rendered from current DB state. The
checklist hides itself when everything is done; the capacity band
hides itself when no pool is at ≥80%.
