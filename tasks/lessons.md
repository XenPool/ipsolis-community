# Lessons Learned – ip·Solis

Dieses File wird nach Korrekturen durch den User aktualisiert.
Ziel: Gleiche Fehler nicht wiederholen.

---

## Alembic Migrationen & Docker Volumes

**Problem:** Neue Migration-Dateien werden von Alembic im laufenden Container nicht gefunden.

**Ursache:** `api/alembic/versions/` ist NICHT im Hot-Reload Volume-Mount (`./api/app:/app/app`).
Neue `.py`-Dateien sind erst nach Rebuild im Container.

**Fix:** `docker cp <datei> xp_api:/app/alembic/versions/<datei>` für sofortige Verfügbarkeit.
**Dauerlösung:** `./api/alembic:/app/alembic` als zusätzliches Volume in `docker-compose.yml` (bereits umgesetzt).

---

## PowerShell Scripts – Zwei Kategorien

**Klarstellung durch User:**
- Neue Scripts für vSphere, Active Roles etc. → selbst neu erstellen in `scripts/vsphere/`, `scripts/active_roles/`


---

## HTMX 2.x – Kein Body-Swap für Auto-Refresh

**Problem:** `hx-target="body" hx-swap="outerHTML" hx-select="body"` führt in HTMX 2.x zu einer visuell leeren Seite nach dem Swap — Tailwind-Styles und Event-Handler werden nicht korrekt neu angebunden.

**Fix:** Für einfaches Page-Polling `setTimeout` + `window.location.reload()` verwenden:
```html
{% if order.status.value in ["pending", "processing"] %}
<script>setTimeout(function(){ window.location.reload(); }, 4000);</script>
{% endif %}
```
Zuverlässiger, browser-nativ, kein DOM-Problem.

---

## JSON-Konvertierung in Alembic Migrations

**Problem:** `str(dict).replace("'", '"')` für JSON in SQL ist fragil — bricht bei `None`, `True`, `False`.

**Fix:** Immer `import json; json.dumps(config)` verwenden.
