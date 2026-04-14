# XenPool IT Selfservice – Claude Code Context

## Task Backlog
Open and completed tasks: see [`TASKS.md`](TASKS.md)
Read at the start of each session and update when a task is completed.

## Project Goal

Production-ready platform for orchestrating IT asset lifecycle workflows — VDIs today, any asset type tomorrow.
Includes a self-service portal for end users and a webhook receiver for ServiceNow integration.

## Stack

| Layer | Technology |
|---|---|
| API / Dispatcher | FastAPI (Python 3.12) |
| Workflow Engine | Celery + Redis |
| Scheduling | Celery Beat |
| Database | PostgreSQL (SQLAlchemy + Alembic) |
| Auth | Entra ID SSO (MSAL) |
| External Systems | XenServer/XCP-ng + vSphere (PowerCLI), Active Directory (LDAP), SCCM, SMTP |
| Container | Docker / Docker Compose |
| Frontend | HTMX + Jinja2 + Tailwind CSS |

## Branch Strategy

- `main` – stable / production
- `pre` – pre-live / testing
- `dev` – active development (all PRs target this branch)
- Feature branches as needed: `feature/<name>`
- Merges to `main` only when stable and tested

## Local Setup

```bash
cp .env.example .env
# Edit .env (passwords, secrets, etc.)
docker compose up --build
```

- API + Admin UI: http://localhost:8000
- Swagger Docs: http://localhost:8000/docs
- Self-Service Portal: http://localhost:8000/portal
- Celery Flower: http://localhost:5555

## Development Notes

### PowerShell Scripts
Scripts in `scripts/ivanti/` are **read-only reference material** and must not be modified.
New scripts belong in:
- `scripts/xenserver/` — XCP-ng / XenServer VM operations
- `scripts/vsphere/` — VMware vSphere operations
- `scripts/sccm/` — SCCM task sequence helpers

All scripts must return JSON on stdout and use pure ASCII (no Unicode characters).

### Database Migrations

```bash
# Create a new migration
docker compose exec api alembic revision --autogenerate -m "description"

# Apply migrations
docker compose exec api alembic upgrade head
```

**Important:** Migration files are embedded at image build time.
For a running container: `docker cp` the file in, then run `alembic upgrade head` directly.
Enum types (e.g. `order_action`) already exist in the DB — use `op.execute(raw SQL)` instead of
`op.create_table()` with `sa.Enum` to avoid `DuplicateObject` errors.

### Jinja2 + JavaScript Templates
JS template literals using `{{` / `}}` conflict with Jinja2 syntax.
Instead of `` `{{${p}}}` ``, always use `'{{' + p + '}}'` (string concatenation).

### Router Registration Order
`admin.router` is registered **before** `admin_runbooks.router` in `main.py`.
`POST /admin/asset-types` is handled by `admin.py` (ORM), not the runbooks router.

### ORM Type Mapping
`lifecycle_renewable` must be declared as `Boolean` (not `Integer`) in the ORM model — required for asyncpg compatibility.

## Key File Paths

| Path | Description |
|------|-------------|
| `api/app/main.py` | FastAPI entry point, router registration |
| `api/app/config.py` | Pydantic Settings (env vars) |
| `api/app/database.py` | SQLAlchemy engine + session |
| `api/app/models/` | ORM models |
| `api/app/routes/` | API routers (admin, portal, auth, webhook, …) |
| `api/app/schemas/` | Pydantic request/response schemas |
| `api/app/templates/` | Jinja2 templates (Admin UI + Portal) |
| `api/app/utils/module_registry.py` | Module metadata mirror for Admin UI |
| `api/app/utils/capacity.py` | Pool capacity enforcement |
| `api/app/utils/entra.py` | MSAL helper (auth URL, token exchange, domain check) |
| `worker/tasks/__init__.py` | Celery app instance + task includes |
| `worker/tasks/workflows/dynamic_runner.py` | Main runbook workflow + Beat scheduler |
| `worker/tasks/modules/` | Atomic modules (pool, vsphere, sccm, …) |
| `worker/tasks/modules/step_helper.py` | Shared step tracking |
| `scripts/xenserver/` | XCP-ng / XenServer PowerShell scripts |
| `scripts/vsphere/` | vSphere PowerShell scripts |
| `scripts/sccm/` | SCCM PowerShell scripts |
| `scripts/ivanti/` | Legacy reference scripts (read-only) |

## Architecture Concepts

| Concept | Implementation |
|---|---|
| Atomic Module | `worker/tasks/modules/*.py` |
| Runbook | `runbook_definitions` + `runbook_steps` tables, executed by `dynamic_runner` |
| Configuration | `app_config` table + `.env` |
| Inbound Dispatch | FastAPI `/webhook` (ServiceNow) or `/orders` (portal/API) |
| Audit Log | `audit_log` table (append-only) |
| Step Log | `order_steps` table with structured JSON per step |

## External System Integrations

- **XenServer/XCP-ng**: PowerCLI scripts via `subprocess` (pwsh in worker container); SSL cert bypass injected globally for self-signed certs
- **vSphere**: same mechanism as XenServer
- **Active Directory**: LDAP (ldap3) for user validation; deeper AD integration (e.g. Quest Active Roles) via PS modules + runbooks
- **SCCM**: WinRM call to trigger unattended reinstall task sequence
- **SMTP**: Python `smtplib` for notifications
- **Entra ID**: MSAL for portal SSO; `POST /admin/config/entra/test` verifies credentials via client-credentials token flow

## Database Schema Overview

| Table | Description |
|---------|-------------|
| `asset_types` | Type definitions incl. `asset_model` (named/pooled), `pool_capacity`, targets |
| `asset_pool` | All managed VMs/assets |
| `orders` | Orders and change requests |
| `order_steps` | Individual module steps per order (structured JSON log) |
| `runbook_definitions` | One runbook per asset type + action |
| `runbook_steps` | Ordered module calls per runbook |
| `audit_log` | Append-only audit trail |
| `app_config` | Central configuration key/value store |
| `ps_modules` | PowerShell modules (Gallery or manual upload) |

## Conventions

- **Audit logging**: `aaudit()` (async, API) · `waudit()` (sync, Worker)
- **Step tracking**: `worker/tasks/modules/step_helper.py`
- **Admin auth**: requires `ADMIN_API_KEY` in `.env`
- **Portal auth**: requires Entra ID config (`entra.mode = enabled`); portal returns HTTP 503 when Entra is not configured
- **`dynamic_runner`** must be listed in `include=[]` in `worker/tasks/__init__.py` or Beat tasks won't register
