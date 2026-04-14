# XenPool IT Selfservice

Open-source platform for IT asset lifecycle automation. Built for on-premises datacenters, replacing expensive commercial tools like Ivanti Automation or ServiceNow orchestration.

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

### Approval Workflows
- Configurable per asset type: manager approval, application owner approval, or both
- Manager looked up automatically from Active Directory
- Re-approval on asset modification (optional per asset type)
- Email notifications to approvers with one-click approve/decline

### Dynamic Runbook Engine
- Visual runbook builder in the Admin UI
- Three automation strategies: Group Access (AD/Entra groups), Runbook (PowerShell scripts), or Composite (both)
- PowerShell module management (install from Gallery or upload custom)
- Step-by-step execution tracking with logs

### Asset Lifecycle Management
- Pool-based or dedicated asset assignment
- Automatic expiry checks and reminder emails (Celery Beat)
- Configurable deprovision policies (access removal, deallocation, deletion)
- Scheduled orders (future-dated provisioning with asset reservation)

### Access Control
- Restrict asset types to specific AD groups (eligible requestors)
- Per-asset-type configuration for RDP and admin user management
- Capacity enforcement with pool availability checks

### Admin UI
- Full asset type configuration (categories, attributes, automation, approvals)
- Runbook management with drag-and-drop step ordering
- Email template editor with variable placeholders
- Central settings for AD, SMTP, vSphere, SCCM, Entra ID
- Audit log viewer
- Asset pool management with bulk import

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
git clone https://github.com/XenPool/IT-SelfService.git
cd IT-SelfService

cp .env.example .env
# Edit .env -- defaults work for development (mock mode)

docker compose up --build
```

| Service | URL |
|---|---|
| Self-Service Portal | http://localhost:8000/portal |
| Admin UI | http://localhost:8000/ui/ |
| API Docs (Swagger) | http://localhost:8000/docs |
| Celery Flower | http://localhost:5555 |

In development mode (`ENVIRONMENT=development`), all external calls (vSphere, SCCM, AD, SMTP) are mocked with realistic delays and logging. No external infrastructure required.

## Production Deployment

See the full deployment guide: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

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
    routes/         FastAPI routers (admin, portal, auth, webhook)
    schemas/        Pydantic request/response schemas
    templates/      Jinja2 templates (Admin UI + Portal)
    utils/          AD lookup, capacity checks, auth helpers
  alembic/          Database migrations
worker/
  tasks/
    modules/        Atomic workflow modules (pool, vsphere, sccm, AD groups, notifications)
    workflows/      Celery workflow orchestration (dynamic_runner)
scripts/
  xenserver/        XCP-ng / XenServer PowerShell scripts
  vsphere/          vSphere PowerShell scripts
  sccm/             SCCM task sequence scripts
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

- [ ] Data retention and automatic cleanup
- [ ] User data export and deletion
- [ ] Sentry integration (optional error tracking)
- [ ] Multi-language support (i18n)
- [ ] Dashboard with usage analytics
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

- **Community**: [GitHub Issues](https://github.com/XenPool/IT-SelfService/issues) for bug reports and feature requests
- **Commercial support**: Contact info@xenpool.com for SLA-backed support contracts and consulting

---

Built by [XenPool GmbH](https://xenpool.com) -- born from 30 years of datacenter operations.
