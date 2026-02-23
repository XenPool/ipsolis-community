# XenPool IT Selfservice

Eigenständiger Ersatz für **Ivanti Automation** zur Orchestrierung von IT-Asset-Lifecycle-Prozessen.

## Stack

- **FastAPI** – Dispatcher + REST API + Self-Service-Portal Backend
- **Celery + Redis** – Workflow Engine (Runbooks)
- **PostgreSQL** – Datenbank (ersetzt MS SQL)
- **PowerShell + PowerCLI** – vSphere-Operationen
- **pypsrp / WinRM** – Active Roles Anbindung

## Quickstart

```bash
cp .env.example .env
# .env anpassen
docker compose --profile dev up --build
```

- API: http://localhost:8000
- Docs: http://localhost:8000/docs
- Flower: http://localhost:5555

## Dokumentation

Siehe [CLAUDE.md](CLAUDE.md) für vollständigen Projektkontext.
