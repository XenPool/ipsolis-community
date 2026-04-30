# ip·Solis – Claude Code Context

## Task Backlog
Open and completed tasks: see [`TASKS.md`](TASKS.md)
Read at the start of each session and update when a task is completed.

## Project Goal

Production-ready platform for orchestrating IT asset lifecycle workflows — VDIs today, any
asset type tomorrow. Includes a self-service portal for end users, an admin UI for
operators, and a webhook receiver for ServiceNow integration.

## Stack

| Layer | Technology |
|---|---|
| API / Dispatcher | FastAPI (Python 3.12) |
| Workflow Engine | Celery + Redis |
| Scheduling | Celery Beat |
| Database | PostgreSQL 16 (SQLAlchemy + Alembic) |
| Portal Auth | Entra ID SSO (MSAL) |
| Admin Auth | Session login + `ADMIN_API_KEY` header |
| Active Directory | `msldap` (NTLM signing / Kerberos) |
| Virtualization | XenServer/XCP-ng + VMware vSphere (PowerShell / PowerCLI) |
| OS Deployment | SCCM (WinRM + AdminService REST) |
| Email | Python `smtplib` |
| Container | Docker / Docker Compose |
| Reverse Proxy | Nginx (TLS termination) |
| Frontend | HTMX + Jinja2 + Tailwind CSS (CDN JIT) |
| Portal i18n | Static JSON per locale (`locales/*.json`) |

## Branch Strategy

- `main` – stable / production
- `pre` – pre-live / testing
- `dev` – active development (all PRs target this branch)
- Feature branches as needed: `feature/<name>`
- Merges to `main` only when stable and tested

## Local Setup

```bash
cp .env.example .env
# Edit .env (passwords, API secret, admin key, webhook token)
docker compose up --build
```

- API + Admin UI: http://localhost:8000/ui/
- Swagger Docs: http://localhost:8000/docs
- Self-Service Portal: http://localhost:8000/portal
- Celery Flower: http://localhost:5555

All external system credentials (AD, SMTP, vSphere, XenServer, SCCM, Entra ID) are
configured at runtime via Admin UI → Settings (`app_config` table), not via `.env`.

## Development Notes

### Scripts + Runbooks — DB is the single source of truth
At runtime, script modules and standalone runbooks are read from the DB
(`script_modules` + `standalone_runbooks` + `standalone_runbook_steps`). The
`scripts/` folder is **seed material only** — disk files are used to (a) seed
fresh deployments via migration 0046, and (b) track changes in git for code
review. Disk files are NOT read at runtime.

**On-disk layout:**
- `scripts/modules/<category>/<Name>.<ext>` — one file per `script_modules` row.
  Category is derived from the DB name prefix (`"SCCM - Delete Device"` → `sccm/`).
  First comment lines carry round-trip metadata: `# NAME: <exact DB name>` and `# DESC: <...>`.
- `scripts/runbooks/<Name>.json` — one file per `standalone_runbooks` row, with steps
  referenced by **script name** (not id) so the seed works regardless of fresh-install ids.

**Export (DB → disk):** Admin UI → Modules → **Export to disk** button, or
`POST /admin/seed/export`. Overwrites the current `scripts/modules/` and
`scripts/runbooks/` contents with a snapshot of the DB. Commit the result to
git to ship it as updated seed data.

**Seeding (disk → DB):** migration `0046_seed_example_scripts_and_runbooks` runs
on every `alembic upgrade head`. Inserts rows only when the name is not already
present — never overwrites user edits.

**PowerShell script requirements:**
- Return JSON on stdout
- Use pure ASCII (no Unicode characters)
- Not rely on interactive prompts (SSL cert prompts auto-answered via stdin)

**Developer tools** (not runtime modules) live under `tools/`:
- `tools/license/` — Ed25519 keypair generator + license signer for Enterprise .lic files
- `tools/validate_locales.py` — portal i18n JSON key-tree validator

### Database Migrations

```bash
# Create a new migration
docker compose exec api alembic revision --autogenerate -m "description"

# Apply migrations
docker compose exec api alembic upgrade head
```

**Important:** Migration files are baked into the image at build time.
For a running container without rebuild: `docker cp <file> xp_api:/app/alembic/versions/`,
then run `alembic upgrade head` directly.
Enum types (e.g. `order_action`, `asset_status`) already exist in the DB — use
`op.execute(raw SQL)` instead of `op.create_table()` with `sa.Enum` to avoid
`DuplicateObject` errors.

Current head: `0093_seed_api_token_purge.py`.

### Template changes require image rebuild
`api/app/templates/` and `api/app/routes/` are baked into the `xp_api` image, not
bind-mounted. After editing any template or route file:
```bash
docker compose up -d --build api
```
Or hot-copy the file in: `docker cp <file> xp_api:/app/app/...` + `docker compose restart api`.

### Jinja2 + JavaScript Templates
JS template literals using `{{` / `}}` conflict with Jinja2 syntax.
Instead of `` `{{${p}}}` ``, always use `'{{' + p + '}}'` (string concatenation).

### Router Registration Order
`admin.router` is registered **before** `admin_runbooks.router` in `main.py`.
`POST /admin/asset-types` is handled by `admin.py` (ORM), not the runbooks router.
`admin_auth.router` is registered **before** `ui.router` so the admin login page is
reachable without a session.

### ORM Type Mapping
`lifecycle_renewable` must be declared as `Boolean` (not `Integer`) in the ORM model —
required for asyncpg compatibility.

### Tailwind via CDN (JIT)
The UI uses `cdn.tailwindcss.com` (see `_partials/theme_head.html`). All utility classes —
including dynamic colors like `bg-purple-50` and arbitrary grid widths — resolve at
runtime; no build step needed.

## Key File Paths

| Path | Description |
|------|-------------|
| `api/app/main.py` | FastAPI entry point, router registration, i18n mount, middleware |
| `api/app/config.py` | Pydantic Settings (env vars) |
| `api/app/database.py` | SQLAlchemy async engine + session |
| `api/app/templates_instance.py` | Shared Jinja2 env + live `app_config` globals (title, logo) |
| `api/app/models/` | ORM models (asset, order, approval, runbook, config, audit, standalone_runbook, ps_module, script_module, global_var, change_log, db_backup) |
| `api/app/routes/admin.py` | Admin CRUD (asset types, pool, orders, config) |
| `api/app/routes/admin_auth.py` | Admin login/logout (session cookie) |
| `api/app/routes/admin_modules.py` | PS module management (Gallery + upload) |
| `api/app/routes/admin_runbooks.py` | Asset-type-bound runbook editor |
| `api/app/routes/admin_standalone_runbooks.py` | Ad-hoc / cron-scheduled runbooks |
| `api/app/routes/admin_maintenance.py` | Backups, health, queue, retention, alerts |
| `api/app/routes/portal.py` | Self-service portal (Entra ID protected) |
| `api/app/routes/auth.py` | Entra ID login/callback/logout for portal |
| `api/app/routes/webhook.py` | ServiceNow inbound webhook |
| `api/app/routes/orders.py` | Order API (create/list/get/cancel) |
| `api/app/routes/ui.py` | Admin UI pages (dashboard, pool, orders, settings, …) |
| `api/app/utils/module_registry.py` | Module metadata mirror for Admin UI |
| `api/app/utils/capacity.py` | Pool capacity enforcement |
| `api/app/utils/entra.py` | MSAL helper (auth URL, token exchange, domain check) |
| `api/app/utils/ad_lookup.py` | msldap user/manager/group lookup (sync wrapper over async) |
| `api/app/utils/auth.py` | `require_admin_key` / session dependencies |
| `api/app/utils/ps_param_parser.py` | PowerShell `param()` block introspection |
| `api/app/utils/asset_type_constraints.py` | Referential-integrity guards on asset types |
| `worker/tasks/__init__.py` | Celery app instance + `include=[]` + Beat schedule |
| `worker/tasks/workflows/dynamic_runner.py` | Main runbook workflow + expiry/schedule Beat tasks |
| `worker/tasks/workflows/standalone_runner.py` | Ad-hoc + cron standalone runbook executor |
| `worker/tasks/workflows/ps_module_installer.py` | PS Gallery install / uploaded zip install |
| `worker/tasks/workflows/sccm_probe.py` | SCCM task-sequence polling workflow |
| `worker/tasks/modules/` | Atomic modules (pool_manager, vsphere, sccm, active_directory, notifications, target_executor, maintenance, config_reader) |
| `worker/tasks/modules/step_helper.py` | Shared step tracking |
| `worker/tasks/modules/registry.py` | Module metadata (names, params, param_schema) |
| `scripts/modules/<cat>/` | Seed copies of script_modules rows (ad, sccm, sql, test, vmware, xenserver) |
| `scripts/runbooks/` | Seed copies of standalone_runbooks as JSON |
| `tools/license/` | Dev tooling: Ed25519 keypair generator + license signer |
| `tools/validate_locales.py` | Portal i18n JSON validator |
| `locales/` | Portal i18n JSON (de/en/es/fr/it) |
| `nginx/nginx.conf` | Reverse-proxy + TLS config (production overlay) |
| `docs/DEPLOYMENT.md` | Production deployment guide |

## Architecture Concepts

| Concept | Implementation |
|---|---|
| Atomic Module | `worker/tasks/modules/*.py` — single-purpose Celery task (pool, vsphere, AD group, …) |
| Runbook (asset-bound) | `runbook_definitions` + `runbook_steps`, executed by `dynamic_runner` per order |
| Standalone Runbook | `standalone_runbooks` + `standalone_runbook_steps`, cron-scheduled or ad-hoc |
| Automation Strategy | `asset_types.automation_strategy` = `group_only` / `runbook_only` / `composite` |
| Group Targets | `asset_types.targets` JSONB: `[{type, identifier, principal_source}]`, executed by `target_executor` |
| Composite Order | `asset_types.composite_steps` JSONB: ordered list of `GROUP_TARGETS` / `RUNBOOK` steps |
| Deprovision Policy | `asset_types.deprovision_policy`: access_only / return_to_pool / return_to_pool_reinstall / deallocate / delete / custom_runbook |
| Assignment Model | `asset_types.assignment_model`: `capacity_pooled` (quota) / `dedicated_shared` (shared instance) / `assigned_personal` (1:1) |
| Configuration | `app_config` table (live-editable) + `.env` (infra only) |
| Inbound Dispatch | FastAPI `/webhook` (ServiceNow HMAC) or `/orders` (portal/API) |
| Audit Log | `audit_log` table (append-only) |
| Step Log | `order_steps` / `standalone_runbook_run_steps` (structured JSON per step) |
| Maintenance | DB backup scheduler, health probes, queue inspection, retention, email alerts |
| PS Module Store | `ps_modules` table + PS-Gallery installer / manual zip upload |
| Email Templates | `app_config` keys `email.tpl.*` (body + subject, variable placeholders) |

## Asset Status Lifecycle

`AssetStatus` enum (see `api/app/models/asset.py`):
- `Free` — available for assignment
- `reserved` — held by a scheduled order (not yet active)
- `busy` — actively assigned to a user
- `Reinstall` — awaiting reinstall runbook after `return_to_pool_reinstall`
- `Reinstalling` — reinstall runbook currently running
- `Failed` — reinstall failed, manual intervention required
- `maintenance` — taken offline by operator

Dashboard tiles (Admin UI `/ui/`) count Free / In use / Failed / Reinstall / Maintenance / Total.

## External System Integrations

- **XenServer/XCP-ng**: PowerShell scripts via `subprocess` (`pwsh` in worker container);
  SSL cert bypass injected globally (self-signed cert support), interactive prompts
  auto-answered via stdin
- **VMware vSphere**: same mechanism as XenServer (PowerCLI-based scripts stored in `script_modules` under the `vmware` category)
- **Active Directory**: `msldap` (NTLM signing / Kerberos) for user validation, manager
  lookup, group membership. Deeper AD integration (e.g. Quest Active Roles) via
  PS modules + runbooks
- **SCCM**: WinRM for task-sequence triggers; AdminService REST (Kerberos auth) for
  device import/delete; status polled by `sccm_probe` workflow
- **SMTP**: Python `smtplib` for all notifications (approvals, reminders, alerts)
- **Entra ID**: MSAL for portal SSO; `POST /admin/config/entra/test` verifies credentials
  via client-credentials token flow
- **ServiceNow**: HMAC-signed webhook at `/webhook`

## Database Schema Overview

| Table | Description |
|---------|-------------|
| `asset_types` | Type definitions — `category`, `assignment_model`, `automation_strategy`, `composite_steps`, `targets`, `deprovision_policy`, `pool_capacity`, lifecycle flags, approval flags |
| `asset_pool` | All managed assets/VMs (status in `AssetStatus` enum) |
| `orders` | Orders and change requests |
| `order_steps` | Individual module steps per order (structured JSON log) |
| `order_approvals` | Approval workflow records (manager / app-owner, per order) |
| `order_change_log` | Append-only diff of order mutations |
| `runbook_definitions` | One runbook per `(asset_type_id, action)` |
| `runbook_steps` | Ordered module calls per runbook |
| `standalone_runbooks` | Ad-hoc or cron-scheduled runbooks (not tied to asset types) |
| `standalone_runbook_steps` | Ordered module calls per standalone runbook |
| `standalone_runbook_runs` | Execution history for standalone runbooks |
| `standalone_runbook_run_steps` | Per-step JSON log for standalone runs |
| `audit_log` | Append-only audit trail |
| `app_config` | Central configuration key/value store (AD, SMTP, vSphere, Entra, email templates, app branding) |
| `ps_modules` | PowerShell modules (Gallery source or uploaded zip in `upload_data BYTEA`) |
| `script_modules` | In-app PowerShell script editor storage |
| `global_vars` | Shared variables available to runbooks and scripts |
| `db_backups` | Maintenance backup metadata (filename, size, created_at) |

## Conventions

- **Audit logging**: `aaudit()` (async, API) · `waudit()` (sync, Worker) — see `worker/tasks/modules/audit_helper.py`
- **Step tracking**: `worker/tasks/modules/step_helper.py`
- **Admin auth**: `require_admin_key` accepts either `X-Admin-Key: <ADMIN_API_KEY>` header or an authenticated admin session cookie
- **Portal auth**: controlled by `entra.mode` (Admin → Settings). `disabled` = portal open with shared anonymous identity; `entra_only` = Entra ID login required; `entra_with_onprem` = Entra ID + on-prem LDAP check
- **`dynamic_runner`, `standalone_runner`, `ps_module_installer`, `sccm_probe`, `maintenance`** must be listed in `include=[]` in `worker/tasks/__init__.py` or Beat tasks won't register
- **Worker queues**: `default` (maintenance), `provision` (orders + standalone + installs), `reclaim` (expiry checks), `notifications` (email)
- **Timezone**: Celery configured for `Europe/Berlin`; DB timestamps stored in UTC
- **No mock mode**: all external systems (AD, SMTP, vSphere, XenServer, SCCM, Entra ID) must point at real test environments — there is no built-in mocking
