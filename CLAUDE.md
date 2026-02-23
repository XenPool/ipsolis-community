# XenPool IT Selfservice – Projektkontext für Claude Code

## Projektziel

Eigenständiger, produktreifer Ersatz für **Ivanti Automation** zur Orchestrierung
von IT-Asset-Lifecycle-Prozessen (VDIs heute, beliebige Assets morgen).
Bringt eigenes Self-Service-Portal mit, kann aber auch ServiceNow-Webhooks empfangen.

## Stack

| Schicht | Technologie |
|---|---|
| API / Dispatcher | FastAPI (Python 3.12) |
| Workflow Engine | Celery + Redis |
| Scheduling | Celery Beat |
| Datenbank | PostgreSQL (via SQLAlchemy + Alembic) |
| Externe Systeme | vSphere (PowerCLI), Active Roles (WinRM/pypsrp), SCCM, SMTP |
| Container | Docker / Docker Compose |
| Frontend | React oder HTMX (später) |

## Branch-Strategie

- `main` – stabiler Stand / Produktion
- `dev` – aktive Entwicklung (alle PRs hierhin)
- Feature-Branches nach Bedarf: `feature/<name>`
- Merges nach `main` nur bei stabilem, getesteten Stand

## Lokal starten

```bash
cp .env.example .env
# .env anpassen (Passwörter, Secrets etc.)
docker compose up --build
```

API läuft dann auf http://localhost:8000
Celery Flower (Monitoring) auf http://localhost:5555

## Entwicklungshinweise

### Mock-Modus
Alle externen Aufrufe (vSphere, Active Roles, SCCM, SMTP) sind gemockt wenn
`ENVIRONMENT=development` in der `.env` gesetzt ist. Mocks simulieren realistisches
Verhalten inkl. Laufzeiten und Logging.

### PowerShell Scripts
**Die Scripts in `scripts/` werden NICHT verändert.** Sie sind atomar, fertig und
geben strukturiertes JSON auf stdout zurück (exit 0 = OK, exit 1 = Fehler).
Python ist der Dirigent, PowerShell die Ausführenden.

### Datenbankmigrationen
```bash
# Neue Migration erstellen
docker compose exec api alembic revision --autogenerate -m "beschreibung"

# Migrationen anwenden
docker compose exec api alembic upgrade head
```

### Wichtige Dateipfade
- `api/app/main.py` – FastAPI-Einstiegspunkt
- `api/app/config.py` – Pydantic Settings (Env-Variablen)
- `api/app/database.py` – SQLAlchemy Engine + Session
- `api/app/models/` – ORM-Models
- `api/app/routes/` – API-Routen
- `worker/tasks/__init__.py` – Celery App-Instanz
- `worker/tasks/workflows/` – Runbooks (Modul-Ketten)
- `worker/tasks/modules/` – Atomare Module

## Konzeptionelle Entsprechungen Ivanti → XenPool

| Ivanti | XenPool IT Selfservice |
|---|---|
| Modul | `worker/tasks/modules/*.py` |
| Runbook | `worker/tasks/workflows/*.py` (Celery Task-Chain) |
| Variablenverwaltung | `app_config`-Tabelle + `.env` |
| Dispatcher | FastAPI `/webhook` oder `/orders` |
| Audit-Log | `audit_log`-Tabelle (unveränderlich) |

## Externe Systemanbindungen

- **vSphere**: PowerCLI-Scripts via `subprocess` (pwsh in Worker-Container)
- **Active Roles**: pypsrp / WinRM → Windows-Host mit Active Roles Console
- **SCCM**: WinRM-Aufruf für Unattended Reinstall-Tasksequenz
- **SMTP**: Python `smtplib` oder `fastmail` für Benachrichtigungen

## Datenbankschema (Überblick)

- `asset_types` – Typdefinitionen (Test VDI, Business VDI, etc.)
- `asset_pool` – Alle verwalteten VMs/Assets
- `orders` – Bestellungen und Änderungsaufträge
- `order_steps` – Einzelne Modul-Schritte je Bestellung
- `audit_log` – Unveränderliches Protokoll
- `app_config` – Zentrale Konfigurationsvariablen
