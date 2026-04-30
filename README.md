# ip·Solis

Open-source platform for IT asset lifecycle automation. Built for on-premises datacenters, replacing expensive commercial tools.

Give your users a self-service portal to request, extend, and return IT assets (VDIs, application access, infrastructure resources) while your IT team keeps full control through configurable approval workflows, runbooks, and audit trails.

## Why This Exists

Enterprise IT automation shouldn't require a 6-month implementation project and a six-figure license. This platform was born from 30 years of datacenter operations experience and is designed to be deployed in an afternoon.

## Key Features

### Self-Service Portal
- Users request assets through a clean web interface
- Entra ID (Azure AD) single sign-on
- Order tracking with real-time status updates
- "My IT" dashboard showing active assets with extend/modify/cancel options
- Deputy support (order on behalf of another user)
- Multi-language UI (English, German, Spanish, French, Italian)
- **Catalog search and category filter** in the request page (auto-shown for catalogs with more than ~6 definitions)
- **Long-form help text per asset definition** (admin-authored markdown, sanitized) shown when the requester picks a type

### Approval Workflows
- Configurable per asset type: manager approval, application owner approval, or both
- Manager looked up automatically from Active Directory
- Re-approval on asset modification (optional per asset type)
- Email notifications to approvers with one-click approve/decline
- **Microsoft Teams approval cards** — Adaptive Card delivered via Workflows webhook with a tokenized link that lets approvers decide without logging into the portal
- **Approval reminders** — a Beat task re-sends email + Teams notification after a configurable interval (default 24 h, capped at 3 nudges) when an approver hasn't decided
- **Approval delegation (OOO mode)** — admins configure deputy windows ("Stefan is on vacation Aug 1–15, route his approvals to Jupp"); new orders during the window automatically address the deputy, original assignee captured in the audit trail
- **Self-service delegation** in the portal — managers configure their own OOO without going through an admin (`/portal/delegations`); identity is enforced server-side so a user can never re-route someone else's approvals
- **Approval escalation** — once an approval has burned through all its reminders without a decision, a single notification fires to the configured escalation contact(s) so an operator can intervene; each row escalates at most once
- **N-of-M approvals** — set `min_approvals_required` per asset definition so any N of M configured approvers can satisfy the order; remaining pending rows transition to `superseded` once the threshold is met. Per-rule N-of-M lets each conditional rule carry its own quorum; rule-driven approvers form an isolated group with that quorum, while manager / owner / no-quorum-rule approvers fold into the asset-type-level group. Decline still vetoes regardless of N.
- **Conditional approval rules** — JSONB rule list per asset definition adds extra approvers when a condition matches the order. Conditions can be a leaf `{field, op, value}` or a compound `{op: "and"|"or"|"not", clauses: [...]}` — nested up to 8 levels deep. Built-in fields (`duration_days`, `monthly_cost`, `has_pii/phi/pci`, `requester_department`) plus any `attr.<key>` from the asset type's user-supplied attributes. Malformed rules are logged and skipped, never blocking order creation.
- **Auto-decline on extended inactivity** — opt-in policy that system-declines pending approvals past a configurable age (e.g. 14 days) so stale requests don't pile up forever. Daily Beat task picks at most one stale approval per order, propagates the existing veto-on-decline semantics (order → rejected, requester emailed, audit row attributed to `system:auto_decline`). Off by default; configure under *Settings → E-Mail → Approval Reminders → Auto-decline (opt-in)*.

### Dynamic Runbook Engine
- Visual runbook builder in the Admin UI
- Three automation strategies: Group Access (AD/Entra groups), Runbook (PowerShell scripts), or Composite (both)
- PowerShell module management (install from Gallery or upload custom `.zip`) with a per-module **Linux compatibility flag** (`Linux ✓` / `Windows only ✕` / `Unverified ?`) — the worker runs PowerShell 7 on Linux, and modules tagged `PSEdition_Desktop` only won't load. Operators declare compatibility when adding a module and can flip the badge inline by clicking it in the modules table
- In-app PowerShell script editor with parameter introspection
- Step-by-step execution tracking with structured JSON logs

### Standalone Runbooks
- Ad-hoc or cron-scheduled runbooks that are not tied to an asset type
- Per-run tracking (history, logs, notes)
- Useful for housekeeping jobs, one-off operations, and scheduled reports

### Asset Lifecycle Management
- Three assignment models: capacity-pooled (quotas), dedicated-shared (jump hosts), assigned-personal (1:1)
- Automatic expiry checks and reminder emails (Celery Beat)
- Configurable deprovision policies: access removal, return-to-pool, return-to-pool-with-reinstall, deallocation, deletion, or custom runbook
- Extended asset statuses — `Free`, `In use`, `Reinstall`, `Reinstalling`, `Failed`, `Maintenance`
- Scheduled orders (future-dated provisioning with asset reservation)

### Maintenance & Operations
- Scheduled PostgreSQL backups (cron-driven) with retention policy
- Manual backup/restore/download via Admin UI
- Health probes (DB, Redis, external system reachability) with email alerts on state transitions
- Celery queue inspection and targeted purge
- **Audit Log viewer** at `/ui/audit-log` — filterable by entity type, entity id, triggered-by substring, and time range; coloured actor badges show at a glance whether each row was driven by a bearer token, an admin session, the legacy `X-Admin-Key`, or a webhook; expand any row to see the JSON before/after diff

### Access Control
- Restrict asset types to specific AD groups (eligible requestors)
- Per-asset-type configuration for RDP and admin user management
- Capacity enforcement with pool availability checks
- **Per-user quota** (`max_per_user`) for personal and pooled assignment models so one user can't exhaust the pool
- **Active / inactive flag** on asset definitions — deprecate without losing history; inactive types disappear from the portal catalog but stay visible in the admin list

### Observability
- **Prometheus `/metrics` endpoint** — request count + latency histogram per route, plus business gauges (orders by status, pending approvals, pool free/busy by asset type, Celery queue depth per worker queue)
- Cardinality-bounded route labels (path templates, not actual paths)
- Toggleable via the `metrics.enabled` config flag
- **OpenTelemetry tracing** — auto-instrumented FastAPI requests, SQLAlchemy queries, and Celery tasks flow through an OTLP HTTP exporter to any standard collector (Jaeger, Tempo, SigNoz, Honeycomb); a request that dispatches a runbook produces a single trace spanning api + worker; a console exporter mode is available for local verification without a collector

### Compliance & Audit
- **SIEM audit-log streaming (Splunk HEC + Microsoft Sentinel + generic HMAC-signed webhook)** — every `audit_log` row is forwarded once a minute to one of three back-ends: Splunk HTTP Event Collector, Sentinel Log Analytics workspace (Azure Monitor Data Collector API), or a generic JSON webhook with HMAC-SHA256 body signing in a GitHub-compatible `X-Hub-Signature-256: sha256=<hex>` header (configurable header name + extra headers for receivers like Datadog, Sumo, Loki, Elastic). Persistent cursor, automatic retry on transient failure, and a "Send Test Event" button verifies connectivity before enabling
- **Tamper-evident audit log** — BEFORE-statement triggers on the `audit_log` table block DELETE / UPDATE / TRUNCATE by default; a documented `SET LOCAL ipsolis.allow_audit_mutation = 'true'` escape hatch exists for legitimate retention maintenance
- **Audit retention pruning** — daily Beat task auto-deletes audit rows older than the configured window using the bypass GUC. Per-classification windows (`retention.pii_days`, `retention.phi_days`, `retention.pci_days`) sit on top of the global `retention.audit_log_days` so PII/PHI/PCI rows can be kept 7+ years while routine config changes drop after 90 days. Each row's classification is set at write time from the strictest class declared on the touched asset type's attributes; `last_run_at`, `last_pruned`, and a per-class breakdown (`last_pruned_by_class`) are tracked in `app_config` for ops visibility
- **Per-integration API tokens** — replaces the single shared `X-Admin-Key` with named, expiring, revocable bearer tokens stored as SHA-256 hashes (raw token shown once on creation); legacy `X-Admin-Key` still accepted as a fallback so existing integrations don't break on upgrade
- **Admin RBAC** — five-tier role ladder (`superadmin > admin > approver > auditor > helpdesk`) backed by per-user accounts in `admin_users` (PBKDF2-SHA256 / 600k iterations, stdlib-only, no bcrypt/passlib build dependency). First-run setup auto-prompts for the first superadmin when the table is empty. Self-protection guards prevent superadmin self-demotion / self-deactivation / self-delete and block losing the last active superadmin. Legacy `ADMIN_API_KEY` continues to authenticate as a virtual superadmin so existing scripts don't break. Audit attribution carries the role (`admin:session:alice:superadmin`) so auditors can filter by both *who* and *with what authority*. Comprehensive role gating: operational routers (modules, runbooks, maintenance, approval-delegations, asset-type CRUD, config writes) require `admin`+; infrastructure routers (license, seed export, initial setup, API tokens, admin user mgmt) require `superadmin`; cost-report is `auditor`+
- **Per-asset-type ACL grants** — scope individual `admin` users to a subset of asset types. Granting an admin even one type flips them into "see only granted types" mode (the admin UI list filters automatically and out-of-scope `PUT/DELETE/clone` returns 404 — same shape as a missing id, so the existence of unrelated teams' types isn't leaked). Zero grants = back-compat "see all" so single-team installs aren't surprised by the new feature. `superadmin` / `approver` / `auditor` / `helpdesk` always bypass scoping. Auto-grant on create: when a scoped admin creates a new asset type, the grant is added automatically so they don't lose visibility on their own creation
- **Separation of duties (SoD)** — a user who configured an asset type cannot also approve their own access requests against it. Detection walks the audit log for matching `created` / `updated` / `cloned` rows attributed to the approver (matched on email, local-part, or admin username). Fires on approve only — declines stay open since rejecting your own work is always allowed. The blocked approver gets HTTP 409 with the original config-time audit attribution quoted back, and the approval row stays `pending` so a different approver can decide
- **Bearer-token role binding** — API tokens may be issued with a specific role (`superadmin` / `admin` / `approver` / `auditor` / `helpdesk`) on top of their scopes. Role-gated routes consult the token's role too, so a token with `admin:*` scope but `auditor` role gets blocked from operational writes. Tokens issued without a role keep pre-slice-3 scope-only behaviour (back-compat). Mint guard: a creator can only issue tokens at or below their own role — no privilege escalation via token issuance
- **Self-service password change** — every admin user can rotate their own password via *My Account* (`/ui/my-account`). Requires the current password as a liveness check; new password must differ from the current and be ≥12 chars. Legacy `ADMIN_API_KEY` actors get a clear 409 directing them to rotate via `.env`. Audit row is `password_changed_self` with no value content
- **External secret management (HashiCorp Vault + CyberArk CCP/AIM)** — replace plaintext credentials in `app_config` with references to your secret store. Any secret-typed config row whose value is `vault://<path>[#<field>]` or `ccp://[<safe>/]<object>` is resolved at read time via the configured backend. Plain string values keep working unchanged so partial migrations are safe. Process-local TTL cache (default 60s) avoids hammering the backend on every config read. Credential consumers (AD bind, Entra client secret, SMTP password, vSphere/XenServer/SCCM passwords on the worker side) all go through the resolver. UI shows reference-shaped values in clear (the path itself isn't sensitive) so admins can see *which* store entry each row points to. Vault auth: static token in slice 1, AppRole/JWT queued for slice 2. CCP auth: AppID + IP allow-list or optional mTLS via configured client cert
- **HA Celery Beat scheduler** — drop-in `celery-redbeat` swap moves the schedule into Redis with a Lua-script distributed lock. Run `docker compose up -d --scale beat=N` and only the lock-holder dispatches; failover from a hard kill of the leader takes ~13 seconds with the tuned 30-second lock TTL + 30-second non-leader poll. Schedule survives restart since it lives in Redis. No application changes — the existing `app.conf.beat_schedule` dict is ingested by RedBeat on first boot
- **Full audit attribution** — every audit row records *who* drove the change, not just *what* happened. Admin endpoints record the credential (`token:<name>` / `admin:session:<user>:<role>` / `admin:legacy_key`); portal mutations record the Entra-resolved user (`portal:user:<email>`) or `portal:anonymous` when SSO is disabled; signed-token approvals record `api:approval_token (approver:<email>)`. The same attribution flows into the order, asset, asset-type, approval, and delegation audit rows so any change can be traced back to the specific credential that triggered it
- **Field-level data classification** — tag each asset attribute as `internal`, `pii`, `phi`, or `pci`; the portal renders matching warning badges next to sensitive fields when requesters fill them in, and the classification flows into the audit-log snapshot for downstream retention queries

### Finance & Chargeback
- **Cost / chargeback per asset definition** — set `monthly_cost`, `currency`, and `cost_center` on each definition; the Cost Report page aggregates active orders into projected monthly spend per cost center with CSV export
- **AD-driven consumer breakdown** — at order creation we snapshot the requester's AD attributes (`department`, `cost_center`, `company`, `employeeID`, `title`; attribute names configurable in Settings) onto each order, so the Cost Report can also slice spend by consuming team / department, and the per-order CSV carries every requester's full HR identity for spreadsheet pivots. Snapshot runs on every creation path — portal, public `POST /orders/`, and the ServiceNow webhook — so externally-driven orders feed the same consumer-side rows
- **Per-order cost projection** — the portal's order detail page renders the projected total (`monthly_cost × months_requested`) in the Access & Duration card whenever the asset definition is priced; users see what their request will cost before / after approval

### Admin UI
- Dashboard with live pool status tiles (auto-refreshing HTMX fragments)
- **Setup checklist** on the dashboard — auto-derived from current DB state, separates "essential" from "recommended", each pending item links to the relevant config page; collapses once everything is done and stays out of the way until something changes
- **Pool capacity warnings** on the dashboard — pools at ≥80% fill (warning, amber) or ≥95% (critical, red) surface in a banner above the status tiles, listed by severity with one-click links to the affected asset definition or pool view
- Full asset type configuration (categories, attributes, automation strategy, approvals)
- Runbook management with drag-and-drop step ordering
- Email template editor with variable placeholders (per-action templates)
- Central settings for AD, SMTP, vSphere, XenServer, SCCM, Entra ID
- Configurable app branding (title, logo, logo position/size)
- Global variables available to runbooks and scripts
- Audit log viewer and order change log
- Asset pool management with bulk import
- Session-based login (plus `X-Admin-Key` header for API access)

### Integrations
- **Active Directory / LDAP** -- user validation, manager lookup, group membership
- **Microsoft Entra ID** -- SSO authentication for the portal
- **vSphere / XenServer / XCP-ng** -- VM lifecycle operations via PowerShell
- **SCCM** -- task sequence triggers for OS deployment
- **SMTP** -- transactional email notifications
- **ServiceNow** -- inbound webhook for order dispatch

## Architecture

```
                    +------------------+
                    |   Nginx (SSL)    |
                    +--------+---------+
                             |
                    +--------+---------+
                    |   FastAPI (API)  |
                    |   Admin UI       |
                    |   Portal         |
                    +----+--------+----+
                         |        |
                +--------+--+  +--+--------+
                | PostgreSQL |  |   Redis   |
                +------------+  +-----+-----+
                                      |
                         +------------+------------+
                         |            |            |
                    +----+----+ +----+----+ +-----+-----+
                    |  Worker | |  Beat   | |  Flower   |
                    | (Celery)| |(Sched.) | | (Monitor) |
                    +----+----+ +---------+ +-----------+
                         |
              +----------+----------+
              |          |          |
          +---+---+ +---+---+ +---+---+
          |vSphere| | SCCM  | |  AD   |
          +-------+ +-------+ +-------+
```

## Stack

| Layer | Technology |
|---|---|
| API / Dispatcher | FastAPI (Python 3.12) |
| Workflow Engine | Celery + Redis |
| Scheduling | Celery Beat |
| Database | PostgreSQL 16 (SQLAlchemy + Alembic) |
| Frontend | HTMX + Jinja2 + Tailwind CSS |
| Authentication | Entra ID SSO (MSAL) |
| VM Operations | PowerShell / PowerCLI |
| Directory Services | Active Directory (msldap) |
| Deployment | Docker Compose |

## Quick Start (Development)

```bash
git clone https://github.com/XenPool/ip·Solis.git
cd ip·Solis

cp .env.example .env
# Edit .env -- set database credentials and API secrets

docker compose up --build
```

| Service | URL |
|---|---|
| Self-Service Portal | http://localhost:8000/portal |
| Admin UI | http://localhost:8000/ui/ |
| API Docs (Swagger) | http://localhost:8000/docs |
| Celery Flower | http://localhost:5555 |

After startup, configure external systems (Active Directory, SMTP, vSphere, Entra ID SSO) through the Admin UI at `/ui/settings`.

### Two run modes

The repository ships two compose files. The main one is the full stack (postgres / redis / api / worker / beat / flower); the second is a small overlay (just nginx) that adds TLS termination in front of the api.

| Mode | Command | When |
|---|---|---|
| **Direct** | `docker compose up -d` | Dev. API on `http://localhost:8000/`, no proxy. |
| **TLS-fronted** | `docker compose -f docker-compose.yml -f docker-compose.nginx.yml up -d` | Pre-live / prod. Nginx handles TLS using `certs/cert.pem` + `certs/key.pem`; reach the app at `https://<your-host>/`. |

The overlay is purely additive — same database, same migrations, same image tags. Switch between modes by stopping (`down`) and starting with the alternate command.

## Production Deployment

See the full deployment guide: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

For per-feature setup walkthroughs (Admin RBAC, external secret management, SIEM streaming, Teams approval cards, OpenTelemetry tracing, HA Beat, conditional approval rules, auto-decline, cost / chargeback, …) see **[docs/ENTERPRISE_FEATURES.md](docs/ENTERPRISE_FEATURES.md)**. Grafana dashboard + Prometheus alerts in **[docs/grafana/](docs/grafana/)**; ops runbooks in **[docs/runbooks/](docs/runbooks/)**.

Summary:
1. Provision a Linux server with Docker
2. Configure `.env` with secure credentials
3. Set up SSL certificates (internal CA, mkcert, or Let's Encrypt)
4. Start with `docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d`
5. Run migrations: `docker compose exec -T api alembic upgrade head`
6. Configure AD, SMTP, and Entra ID through the Admin UI

## Project Structure

```
api/
  app/
    models/         SQLAlchemy ORM models
    routes/         FastAPI routers (admin, admin_auth, admin_modules,
                    admin_runbooks, admin_standalone_runbooks,
                    admin_maintenance, admin_users, admin_self,
                    admin_api_tokens, admin_license, admin_setup,
                    admin_seed_export, admin_cost_report,
                    admin_approval_delegations, approvals_external,
                    portal, portal_delegations, auth, orders, webhook,
                    metrics, ui)
    schemas/        Pydantic request/response schemas
    templates/      Jinja2 templates (Admin UI + Portal + admin login)
    static/         Static JS/CSS assets served by FastAPI
    utils/          AD (msldap), capacity, MSAL, admin auth, PS param parser,
                    RBAC, password (PBKDF2), api_tokens, secrets resolver
                    (Vault / CCP), approval token signer + decision helper,
                    SoD detection, classification, metrics, tracing, audit
  alembic/
    versions/       Database migrations (0001 ... 0078, head bumps with each
                    feature slice — see `docker compose exec api alembic
                    history` for the full chain)
worker/
  tasks/
    modules/        Atomic workflow modules (pool_manager, vsphere, sccm,
                    active_directory, notifications, target_executor,
                    teams_notify, maintenance, config_reader, secrets,
                    siem_export, audit_helper, step_helper, registry)
    workflows/      Orchestration (dynamic_runner, standalone_runner,
                    ps_module_installer, sccm_probe, license_check,
                    siem_streamer, audit_retention, approval_reminders,
                    approval_auto_decline, update_checker)
    tracing.py      OpenTelemetry setup for the worker side
scripts/
  modules/          Per-category PowerShell script modules synced from the DB
                    (ad / sccm / sql / test / vmware / xenserver) — seed
                    material only, runtime reads from the DB
  runbooks/         Standalone runbook JSON snapshots (seed material)
tools/
  license/          Ed25519 keypair generator + license signer for
                    Enterprise .lic files
  validate_locales.py  Portal i18n JSON key-tree validator
locales/            Portal i18n (de/en/es/fr/it JSON)
nginx/              Reverse-proxy + TLS config (production overlay)
backups/            Persisted DB dumps (bind-mounted into api + worker)
licenses/           Signed `.lic` Enterprise license file (read-write)
docs/
  DEPLOYMENT.md             Production deployment guide
  ENTERPRISE_FEATURES.md    Per-feature setup walkthroughs
  grafana/                  Ready-to-import dashboard + Prometheus alerts
  runbooks/                 Operational runbooks (e.g. compose project rename)
```

## Data Privacy

This software is designed for on-premises deployment. All data stays within your infrastructure.

### What personal data is stored

| Data | Where | Purpose |
|---|---|---|
| Email address, display name | `orders`, `order_approvals` | Order processing, notifications |
| sAMAccountName | `orders` (RDP/admin user lists) | Access provisioning |
| Manager relationship | Queried live from AD, not stored | Approval workflow |
| Action history | `audit_log` | Accountability and compliance |
| Session token | Browser cookie (`xp_session`) | Authentication |

### Built-in safeguards

- No data leaves your network (zero telemetry, no cloud dependencies)
- Tamper-evident audit log (BEFORE-statement triggers block DELETE / UPDATE / TRUNCATE outside a documented bypass)
- Per-classification audit retention windows (PII / PHI / PCI configurable independently of the global window)
- Five-tier admin RBAC with per-asset-type ACL grants and separation-of-duties enforcement
- Entra ID SSO for the portal (no password storage on the portal side)
- Admin password storage uses PBKDF2-SHA256 (600k iterations, OWASP-2023)
- External secret management (HashiCorp Vault / CyberArk CCP) replaces plaintext credentials in `app_config`
- All traffic encrypted via HTTPS (nginx TLS termination)
- Field-level data classification badges so requesters know which fields are sensitive before submitting

### Planned enhancements

- Order-history retention with automatic cleanup
- User data export (data portability)
- User data anonymization/deletion
- Configurable session cookie hardening (production defaults)
- Privacy notice integration for the portal

Operators are responsible for maintaining their own records of processing activities and providing appropriate privacy notices to their users as required by applicable regulations.

## Screenshots

*Coming soon*

## Roadmap

**Shipped**

- [x] Multi-language support (i18n) — portal available in DE / EN / ES / FR / IT
- [x] Dashboard with live pool status tiles, setup checklist, pool capacity warnings
- [x] Maintenance UI (backups, health probes, queue inspection)
- [x] PowerShell module management (Gallery + manual upload, with Linux compatibility flag)
- [x] Standalone runbooks (ad-hoc + cron-scheduled)
- [x] Per-classification audit-log retention with tamper-evident triggers
- [x] Role-based admin access — five-tier ladder with per-asset-type ACL grants and SoD enforcement
- [x] API token management for external integrations (per-token scopes + role binding)
- [x] Audit log viewer + SIEM streaming (Splunk HEC / Microsoft Sentinel / generic webhook)
- [x] OpenTelemetry tracing (api + Celery worker) + Prometheus metrics + Grafana dashboard
- [x] External secret management (HashiCorp Vault + CyberArk CCP/AIM)
- [x] HA Beat scheduler (celery-redbeat with Redis-backed distributed lock)
- [x] Cost / chargeback per asset definition with provider + consumer (AD-driven) views
- [x] Approval reminders, escalation, delegation (admin + portal self-service), and auto-decline

**Open**

- [ ] Access certification campaigns (quarterly manager re-confirmation workflow)
- [ ] HR feed + SCIM 2.0 (Workday/SAP leaver events, Okta / Ping / SailPoint integration)
- [ ] Cost report: threshold alerting, historical view, FX conversion
- [ ] Data retention and automatic cleanup (order history)
- [ ] User data export and deletion (GDPR-style data portability)
- [ ] Optional Sentry integration for error tracking
- [ ] Terraform provider for asset definition configuration

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

You are free to use, modify, and distribute this software. If you run a modified version as a network service, you must make the source code available to your users.

For commercial licensing options (e.g., proprietary use without AGPL obligations), contact: **info@xenpool.com**

See [LICENSE](LICENSE) for the full license text.

This project uses open-source components under MIT, BSD, and Apache-2.0 licenses.
See [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md) for the complete list.

## Contributing

Contributions are welcome! Please open an issue first to discuss what you'd like to change.

## Support

- **Community**: [GitHub Issues](https://github.com/XenPool/ip·Solis/issues) for bug reports and feature requests
- **Commercial support**: Contact info@xenpool.com for SLA-backed support contracts and consulting

---

Built by [XenPool GmbH](https://xenpool.com) -- born from 30 years of datacenter operations.
