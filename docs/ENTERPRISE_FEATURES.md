# Enterprise & operability features

This page covers the per-feature setup for capabilities that go beyond the
default install. Everything below is community-licensed unless an
explicit *Enterprise license* note appears.

- [Per-user quota (`max_per_user`)](#per-user-quota-max_per_user)
- [Active / inactive flag on asset definitions](#active--inactive-flag-on-asset-definitions)
- [Long-form help text per asset definition (markdown)](#long-form-help-text-per-asset-definition-markdown)
- [Catalog search and category filter](#catalog-search-and-category-filter)
- [Microsoft Teams approval cards](#microsoft-teams-approval-cards)
- [Approval reminders](#approval-reminders)
- [Prometheus `/metrics` endpoint](#prometheus-metrics-endpoint)
- [SIEM audit-log streaming (Splunk HEC)](#siem-audit-log-streaming-splunk-hec)
- [Per-integration API tokens](#per-integration-api-tokens)
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

In ipSolis Admin UI:

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
common in regulated environments; without automatic nudges, ipSolis
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

## SIEM audit-log streaming (Splunk HEC)

Every `audit_log` row (every order/asset/asset-type/approval mutation)
gets forwarded to a configured SIEM endpoint. Today's adapter is
**Splunk HEC**; the architecture is generic and additional adapters
(Microsoft Sentinel, Elastic, generic JSON webhook) can be added by
implementing a new `build_*_payload` and `post_*` pair in
`worker/tasks/modules/siem_export.py`.

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

### ipSolis setup

Admin UI → *Settings* → *Compliance* tab → *SIEM — Audit Log Streaming*:

1. Set *Mode* to `Enabled`.
2. Leave *Format* at `Splunk HEC`.
3. Paste the *Endpoint URL* and *HEC Token*.
4. Adjust *Batch size* and *Verify TLS* as needed (defaults are sane:
   200 / verify on).
5. Click **Save Settings**, then **Send Test Event** — a single
   synthetic `siem_test` event is posted; success means Splunk
   accepts your payload format and authentication.
6. Enable the master switch and watch the live status panel:
   `Backlog: 41 rows pending` → `(caught up)` after the next
   minute's Beat tick.

### Stored config keys

| Key | Purpose | Stored as |
|---|---|---|
| `siem.enabled`         | `true`/`false` master switch | plain |
| `siem.format`          | `splunk_hec` (only adapter today) | plain |
| `siem.endpoint_url`    | Splunk HEC endpoint URL | plain |
| `siem.token`           | HEC token | secret |
| `siem.batch_size`      | Max events per minute (1–1000) | plain |
| `siem.verify_tls`      | Verify endpoint TLS cert | plain |
| `siem.last_id`         | Auto: last forwarded audit_log id | plain |
| `siem.last_error`      | Auto: most recent failure message | plain |
| `siem.last_success_at` | Auto: ISO timestamp of last success | plain |

### Operational notes

- The streamer **never raises** — failed batches are logged at WARNING,
  recorded in `siem.last_error`, and retried next tick. The Beat
  scheduler is unaffected by individual ticks.
- If the cursor (`siem.last_id`) needs manual repositioning (e.g. you
  want to backfill from the beginning into a fresh Splunk index),
  set it directly via `PUT /admin/config/siem.last_id` with
  `{"value": "0"}`.
- Tamper-evidence at the database layer (revoking `DELETE`/`UPDATE`
  on `audit_log` from the application role) is a separate forthcoming
  slice; today an admin with DB credentials could still mutate the
  underlying table. Streaming to an external SIEM mitigates that risk
  by giving you a copy outside the app's blast radius.

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

**Not yet:**

- Scope decorators per endpoint (everything still requires
  `admin:*` for now). The `scopes` column is already JSON-shaped so
  this lands without a migration.
- ServiceNow webhook secret migration to a bearer token. The webhook
  HMAC stays as-is; admins can pre-create a token named e.g.
  `servicenow-int` and send it as `Authorization: Bearer …` once the
  scoped admin endpoints land.

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
