# Ipsolis

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

### Dynamic Runbook Engine
- Visual runbook builder in the Admin UI
- Three automation strategies: Group Access (AD/Entra groups), Runbook (PowerShell scripts), or Composite (both)
- PowerShell module management (install from Gallery or upload custom `.zip`)
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
- **Prometheus `/metrics` endpoint** — request count + latency histogram per route, plus business gauges (orders by status, pending approvals, pool free/busy by asset type)
- Cardinality-bounded route labels (path templates, not actual paths)
- Toggleable via the `metrics.enabled` config flag
- **OpenTelemetry tracing** — auto-instrumented FastAPI requests and SQLAlchemy queries flow through an OTLP HTTP exporter to any standard collector (Jaeger, Tempo, SigNoz, Honeycomb); a console exporter mode is available for local verification without a collector

### Compliance & Audit
- **SIEM audit-log streaming (Splunk HEC)** — every `audit_log` row is forwarded once a minute to a configured Splunk HTTP Event Collector, with persistent cursor, automatic retry on transient failure, and a "Send Test Event" button to verify connectivity before enabling
- **Per-integration API tokens** — replaces the single shared `X-Admin-Key` with named, expiring, revocable bearer tokens stored as SHA-256 hashes (raw token shown once on creation); legacy `X-Admin-Key` still accepted as a fallback so existing integrations don't break on upgrade
- **Field-level data classification** — tag each asset attribute as `internal`, `pii`, `phi`, or `pci`; the portal renders matching warning badges next to sensitive fields when requesters fill them in, and the classification flows into the audit-log snapshot for downstream retention queries

### Finance & Chargeback
- **Cost / chargeback per asset definition** — set `monthly_cost`, `currency`, and `cost_center` on each definition; the Cost Report page aggregates active orders into projected monthly spend per cost center with CSV export
- **AD-driven consumer breakdown** — at order creation we snapshot the requester's AD attributes (`department`, `cost_center`, `company`, `employeeID`, `title`; attribute names configurable in Settings) onto each order, so the Cost Report can also slice spend by consuming team / department, and the per-order CSV carries every requester's full HR identity for spreadsheet pivots

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

## Production Deployment

See the full deployment guide: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

For per-feature setup (Teams approval cards, Prometheus metrics, per-user quotas, catalog help text, …) see **[docs/ENTERPRISE_FEATURES.md](docs/ENTERPRISE_FEATURES.md)**.

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
                    admin_maintenance, portal, auth, orders, webhook, ui)
    schemas/        Pydantic request/response schemas
    templates/      Jinja2 templates (Admin UI + Portal + admin login)
    static/         Static JS/CSS assets served by FastAPI
    utils/          AD (msldap), capacity, MSAL, admin auth, PS param parser
  alembic/
    versions/       Database migrations (0001 ... 0044+)
worker/
  tasks/
    modules/        Atomic workflow modules (pool_manager, vsphere, sccm,
                    active_directory, notifications, target_executor,
                    maintenance, config_reader)
    workflows/      Orchestration (dynamic_runner, standalone_runner,
                    ps_module_installer, sccm_probe)
    utils/          DB query helpers for worker-side code
scripts/
  ad/               Active Directory PowerShell scripts
  sccm/             SCCM task sequence scripts
  sql/              SQL helpers (legacy migration queries)
  test/             Sandbox / smoke-test scripts
  vmware/           VMware vSphere PowerShell scripts
  xenserver/        XCP-ng / XenServer PowerShell scripts
locales/            Portal i18n (de/en/es/fr/it JSON + validator)
nginx/              Reverse-proxy + TLS config (production overlay)
backups/            Persisted DB dumps (bind-mounted into api + worker)
docs/
  DEPLOYMENT.md     Production deployment guide
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
- Append-only audit log for accountability
- Role-based access (admin vs. portal user)
- Entra ID SSO (no password storage)
- All traffic encrypted via HTTPS (nginx TLS termination)

### Planned enhancements

- Data retention policies with automatic cleanup of old orders
- User data export (data portability)
- User data anonymization/deletion
- Configurable session cookie hardening (production defaults)
- Privacy notice integration for the portal

Operators are responsible for maintaining their own records of processing activities and providing appropriate privacy notices to their users as required by applicable regulations.

## Screenshots

*Coming soon*

## Roadmap

- [x] Multi-language support (i18n) -- portal available in DE / EN / ES / FR / IT
- [x] Dashboard with live pool status tiles
- [x] Maintenance UI (backups, health probes, queue inspection)
- [x] PowerShell module management (Gallery + manual upload)
- [x] Standalone runbooks (ad-hoc + cron-scheduled)
- [ ] Data retention and automatic cleanup (order history)
- [ ] User data export and deletion
- [ ] Sentry integration (optional error tracking)
- [ ] Usage analytics dashboard
- [ ] Role-based admin access (multiple admin roles)
- [ ] API token management for external integrations
- [ ] Terraform provider for asset type configuration

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
