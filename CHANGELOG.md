# Changelog

All notable changes to ip·Solis are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per release, entries are grouped under `Added` / `Changed` / `Fixed` /
`Security` / `Migration` headings. The `Migration` section calls out
any DB schema changes; ip·Solis runs Alembic migrations on container
start, so a `docker compose pull && docker compose up -d` is the only
operator step. See [`docs/UPGRADING.md`](docs/UPGRADING.md) (TODO) for
the full upgrade procedure including DB backup recommendations.

## [Unreleased]

## [0.4.7] — 2026-05-11

### Added

- **Community example scripts (migration 0096).** Three ready-to-use PowerShell
  script modules — `Example - Provision Asset`, `Example - Change Asset`, and
  `Example - Deprovision Asset` — are now seeded on every fresh install. They
  demonstrate the standard module pattern (param block, `$VARS` access, JSON
  output, try/catch) for use in asset-type runbooks.

### Changed

- **Standalone Runbooks are now PRO-only.** The sidebar nav item shows a locked
  PRO badge on Community installs; navigating to `/ui/standalone-runbooks`
  renders an upgrade teaser instead of the runbook list. The API route
  (`admin_standalone_runbooks.py`) and the Celery worker task
  (`standalone_runner.py`) are stripped from Community images at build time,
  and the Beat cron-schedule entry is omitted when the task is absent.
- **Script module seed data split by edition.** The full set of production
  script modules (AD, SCCM, XenServer/XCP-ng, VMware, SQL) is PRO-only seed
  material. The community mirror now ships only `scripts/modules/examples/`;
  the `scripts/runbooks/` directory (Virtual Machine Recycler etc.) is also
  excluded from Community builds.

## [0.4.6] — 2026-05-10

### Fixed

- **PRO feature gating in Community edition.** Community installs now
  show locked PRO badges (violet) on all PRO-only nav items and settings
  sections instead of either hiding them completely or granting full access.
  Affected surfaces: Certifications, Leaver Events (sidebar nav), SIEM
  (Settings → Compliance), SCCM (Settings → SCCM tab), and
  vSphere / XenServer (Settings → Hosting Infra tab).
- **Settings page layout broken for E-Mail, Compliance, SCCM, and
  Hosting Infra tabs.** A missing `<div id="edit-modal">` outer wrapper
  in the Script Variables tab caused a stray `</div>` to close the main
  content container early, pushing every subsequent tab panel outside
  the page layout.
- **Dashboard banner incorrectly showed "PRO Edition" on Community
  installs.** The edition check now requires `edition == 'pro'`; Community
  installs show no banner.
- **Certifications and Leaver Events appeared as active links in Community
  when running a non-stripped image** (e.g. dev). Nav logic now gates
  on `edition` rather than `has_certifications` / `has_leaver_events`.
- **SCCM health probe called a stripped worker task on Community.**
  `_probe_sccm` now short-circuits with `{"ok": null, "detail": "PRO feature"}`
  when `edition != "pro"`, matching the N/A display of unconfigured services.

### Changed

- `enterprise_teaser.html` unified to a single **PRO** tier (previously
  had separate ENT / BUS tiers with amber / blue distinction). All gated
  features now show a consistent violet PRO badge. The partial is no
  longer stripped from the Community Docker image so teasers render
  correctly without the PRO code present.
- Community mirror workflow updated to retain `enterprise_teaser.html`
  in the public source tree (required for teaser rendering in community
  builds from source).

## [0.4.5]

  Range:  v0.4.4..HEAD
  Date:   2026-05-09

### Added

- add installation guide and environment variable setup (`3dc57ff`)

### Documentation

- remove completed compose-rename migration runbook (`1ed6c96`)

### Other

- remove orphaned and obsolete files (`738964f`)

## [0.4.3] — 2026-04-28

### Added

- add option to create AD groups if missing during grant (`420429d`)

## [0.4.2] — 2026-04-28

### Added

- Implement update notifier and password policy features (`30afc66`)
- add PowerShell and bash scripts for release management (`ecd252b`)
- refresh license globals on config refresh (`6d600fe`)

### Changed

- update project name to 'ip·Solis' across documentation and code (`0fa6328`)

## [0.4.1] — 2026-04-27

### Added

- **RBAC slice 4 — password rotation, lockout, SoD per-rule opt-out,
  token mint guard relaxation.** Operators can now configure forced
  password rotation (`rbac.password_rotation_days`, 0 disables) and
  lockout-on-N-failed-attempts (`rbac.lockout_threshold`,
  `rbac.lockout_duration_minutes`). Failed-login attempts are tracked
  per admin user; lockouts auto-expire after the configured window.
  Approval rules accept `sod_exempt: true` so a static compliance
  officer who is also an admin can sign off on orders for asset types
  they configured. `/admin/api-tokens` router gate relaxed from
  `superadmin` to `admin`; the existing mint guard prevents privilege
  escalation. `/admin/maintenance/*` GET endpoints now reachable by
  `auditor` for compliance review; writes still require `admin`.
- **RBAC enterprise gating.** Per-asset-type ACL grants, role-bound
  API tokens, SoD enforcement, and the new password policy are now
  Enterprise-only features. Community installs ship the full role
  ladder, per-user accounts, and scope-based authz — anything an
  ops team needs to run safely. The Enterprise upgrades target
  auditor-grade compliance (scoped grants, role-bound tokens,
  enforced SoD, password policy).
- **Testlab compose stack** (`docker-compose.testlab.yml`) bundling
  Vault dev mode, rsyslog, and a mock SIEM/webhook receiver so SIEM
  / secret-backend / webhook integrations can be smoke-tested
  without paying the resource bill of full Splunk / Sentinel /
  CyberArk lab installs. Splunk Free is profile-gated (heavy image)
  and brought up via `--profile splunk` on demand.
- **Role-aware Admin UI navigation.** Each nav item is gated by the
  signed-in admin's role so a helpdesk user lands on a clean
  6-item nav instead of seeing every page and 403'ing on every
  click. Asset-type form shows a read-only banner with disabled
  Save button when the role can't write.
- **Runbook step editor — categorised module dropdown.** Modules
  are grouped server-side by their `"CATEGORY - Name"` prefix
  (`<optgroup>`); description, category badge, parameter list, and
  an "Edit module ↗" deeplink render in a card below the dropdown
  instead of being squeezed into the option text.
- Implement update notifier and password policy features (`30afc66`)

### Changed

- **License changes refresh template globals immediately.** Uploading
  or removing a license now refreshes the `is_enterprise` / `edition`
  / `license_info` Jinja env globals as part of the same request, so
  the Dashboard and feature-gated nav blocks reflect the new edition
  without an api restart.
- **Approval-delegations nav link gated to `admin`+** to match the
  API gate (the page is "admin manages delegations on behalf of
  users", not self-service).
- **Asset-type form result banner moved above the sticky save bar**
  so 4xx responses (validation, role mismatch) are visible without
  scrolling past the bar pinned to the viewport bottom.

### Fixed

- **Vault testlab healthcheck** — the alpine Vault image has no
  `wget`; switched the healthcheck to `vault status`.

### Migration

- `0073_rbac_slice4` adds three columns to `admin_users`
  (`password_set_at`, `failed_login_count`, `locked_at`), one column
  to `order_approvals` (`sod_exempt`), and seeds three policy keys
  (`rbac.password_rotation_days`, `rbac.lockout_threshold`,
  `rbac.lockout_duration_minutes`). All defaults disable enforcement
  so existing installs see no behaviour change.
- `0074_update_check_config` (this release) seeds the update-checker
  feature config keys (`updates.check_enabled`, `updates.repo_url`,
  plus four cursor keys the daily Beat task fills in). All disabled
  by default.

## [0.4.0] — 2026-04-26

First internal release snapshot. No public deployments yet — versioned
in the 0.x range until the API surface is committed (1.0 marks the
"safe to integrate against" boundary). Highlights:

- Self-service portal + admin UI + ServiceNow webhook
- Asset type, runbook, and standalone-runbook orchestration
- VDI and Server lifecycle workflows on XenServer/XCP-ng + vSphere
- SCCM task-sequence integration (NTLM today; Kerberos backlogged)
- Active Directory user/manager/group lookups via msldap
- Entra ID portal SSO via MSAL
- Conditional approval rules with N-of-M quorum support
- Approval delegations + reminder/escalation Beat tasks
- SIEM streaming (Splunk HEC, Microsoft Sentinel, generic webhook)
- External secret management (Vault KV v2, CyberArk CCP)
- OpenTelemetry tracing, Audit log retention by classification
- HA Beat (RedBeat distributed lock)
- RBAC slices 1-3: 5-tier role ladder, per-user accounts,
  per-asset-type ACL grants, role-bound API tokens, SoD enforcement
