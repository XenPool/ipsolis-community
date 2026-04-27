# ip·Solis – Edition Feature Matrix

This document defines the feature split between the **Community Edition** (open-source, AGPL-3.0) and the **Enterprise Edition** (commercial license). It serves as the canonical reference for edition gating throughout the codebase.

## Guiding Principles

- The Community Edition must be **fully functional** for small-to-mid-sized teams — not a crippled demo.
- Enterprise features target organizations with **complex governance, compliance, or scale requirements**.
- Edition gating is implemented via **runtime license checks and feature flags**, not separate codebases or branches.
- All features ship in a **single codebase**. Enterprise features are present but gated.

## Community Edition (AGPL-3.0)

### Self-Service Portal
- Asset request, status tracking, extend, return
- "My IT" dashboard (active assets overview)
- Multi-language UI (EN, DE, FR, ES, IT)
- Entra ID (Azure AD) single sign-on

### Approval Workflows
- Manager approval (auto-resolved from Active Directory)
- Email notifications with one-click approve / decline

### Runbook Engine
- Three automation strategies: Group Access, Runbook, Composite
- Runbook definition with ordered steps (list-based configuration)
- PowerShell script execution via Celery workers
- Step-by-step execution tracking with structured JSON logs
- In-app PowerShell script editor

### Asset Lifecycle Management
- All three assignment models: capacity-pooled, dedicated-shared, assigned-personal
- Asset statuses: Free, Reserved, Busy, Reinstall, Reinstalling, Failed, Maintenance
- Standard deprovision policies: access_only, return_to_pool, return_to_pool_reinstall, deallocate, delete
- Automatic expiry checks and reminder emails (Celery Beat)

### Admin UI
- Asset type configuration (categories, attributes, automation strategy)
- Asset pool management
- Order overview and management
- Settings for AD, SMTP, Entra ID
- Dashboard with live pool status tiles

### Integrations
- Active Directory / LDAP (user validation, manager lookup, group membership)
- SMTP (transactional email notifications)

### Infrastructure
- PostgreSQL database with Alembic migrations
- REST API with OpenAPI / Swagger documentation
- Docker Compose deployment
- Basic health probes (DB, Redis connectivity)
- Append-only audit log (data written in all editions)
- Mock mode for development (no external systems required)

---

## Enterprise Edition (Commercial License)

*Includes everything in Community Edition, plus:*

### Advanced Workflows
- Application owner approval (second approval tier)
- Re-approval on asset modification (configurable per asset type)
- Deputy support (order on behalf of another user)
- Scheduled orders (future-dated provisioning with asset reservation)
- Custom runbook deprovision policy

### Visual Runbook Builder
- Drag-and-drop step ordering
- Visual workflow composition

### Standalone Runbooks
- Ad-hoc runbooks (not tied to asset types)
- Cron-scheduled runbooks with per-run history, logs, and notes

### PowerShell Module Management
- Install modules from PowerShell Gallery
- Upload custom modules (.zip)
- Module registry with metadata

### Platform Integrations
- VMware vSphere (VM lifecycle operations via PowerCLI)
- XenServer / XCP-ng (VM lifecycle operations)
- SCCM (task sequence triggers, device import/delete, status polling)
- ServiceNow (inbound HMAC-signed webhook for order dispatch)

### Advanced Access Control
- Eligible requestors (restrict asset types to specific AD groups)
- Per-asset-type RDP and admin user management

### Maintenance & Operations
- Scheduled PostgreSQL backups with retention policy
- Manual backup / restore / download via Admin UI
- Celery queue inspection and targeted purge
- Email alerts on health state transitions

### Customization
- Email template editor with variable placeholders (per-action templates)
- App branding (title, logo, logo position and size)
- Global variables for runbooks and scripts

### Audit & Compliance
- Audit log viewer (UI)
- Order change log viewer (UI)

### Planned (Enterprise Roadmap)
- Role-based admin access (multiple admin roles)
- API token management for external integrations
- Usage analytics dashboard
- Data retention policies with automatic cleanup
- User data export and deletion (GDPR / DSGVO)
- Sentry integration (optional error tracking)
- Terraform provider for asset type configuration

---

## Edition Gating – Implementation Guide

### License Model

The application checks for a valid license at startup. Without a license (or with an expired license), all Enterprise features are hidden and disabled.

```
# License check pseudocode
EDITION = load_license()  # "community" | "enterprise"
```

### Gating Pattern

Enterprise features are gated at three levels:

1. **UI layer** — Menu items, buttons, and pages are conditionally rendered:
   ```jinja2
   {% if edition == "enterprise" %}
     <a href="/ui/standalone-runbooks">Standalone Runbooks</a>
   {% endif %}
   ```

2. **API layer** — Endpoints return `HTTP 403` with an upgrade message:
   ```python
   @router.post("/standalone-runbooks")
   async def create_standalone_runbook(...):
       if not license.is_enterprise:
           raise HTTPException(403, "Standalone Runbooks require an Enterprise license.")
   ```

3. **Worker layer** — Tasks check edition before execution:
   ```python
   if not is_enterprise():
       return {"status": "skipped", "reason": "enterprise_only"}
   ```

### Feature Flag Registry

A central `ENTERPRISE_FEATURES` registry maps feature keys to their gating status:

```python
ENTERPRISE_FEATURES = {
    "standalone_runbooks":      True,
    "visual_runbook_builder":   True,
    "ps_module_management":     True,
    "deputy_support":           True,
    "scheduled_orders":         True,
    "app_owner_approval":       True,
    "reapproval_on_modify":     True,
    "servicenow_webhook":       True,
    "vsphere_integration":      True,
    "xenserver_integration":    True,
    "sccm_integration":         True,
    "eligible_requestors":      True,
    "email_template_editor":    True,
    "app_branding":             True,
    "global_variables":         True,
    "audit_log_viewer":         True,
    "change_log_viewer":        True,
    "advanced_maintenance":     True,
    "custom_deprovision":       True,
}
```

---

## Versioning

This document follows the product version. Update it whenever features move between editions or new features are added.

| Version | Date | Change |
|---------|------|--------|
| 1.0 | 2026-04-23 | Initial edition split |
