# Runbook — Rename Compose Project and Deployment Path

**Goal:** rename the on-disk deployment from `/opt/it-selfservice/` to `/opt/ipsolis/`, and the Docker Compose project name from `it-selfservice` to `ipsolis`, **without losing any persistent data** (Postgres, Redis, Celery Beat schedule, PS module store).

**Scope:** LinPre1 (prelive) first. Same recipe applies to prod later.

**Estimated downtime:** 10–15 minutes.

**Executor:** `deployer` on LinPre1 + someone with admin on GitHub repo settings for the `PRELIVE_PATH` secret.

---

## 0. Why this is not just a `mv`

Docker Compose derives three things from the project name (which defaults to the directory name):

| Artifact | Default naming | If we just `mv` |
|---|---|---|
| Volumes | `<project>_<volume>` (e.g. `it-selfservice_postgres_data`) | New empty volumes created, old data orphaned |
| Networks | `<project>_default` | New empty network, no harm (containers re-wire) |
| Nginx container | `<project>-nginx-1` (no explicit `container_name`) | New container, briefly confusing in `docker ps` |
| Explicit-named containers (`xp_*`) | As set | No change |

The volume issue is the dangerous one. We neutralise it by **pinning the volume names** in `docker-compose.yml` before the rename, so the volumes stay attached regardless of project name.

---

## 1. Prep commit (do this **before** the maintenance window)

On a dev workstation, on the `prelive` branch, make the following edits and push. The deploy-to-prelive workflow will run with no functional change — the volume-name pinning is a no-op against existing volumes that already carry those names by default.

### 1.1 — Add `name:` at the top of `docker-compose.yml`

```yaml
# docker-compose.yml (top of file)
name: it-selfservice          # pin current project name explicitly
services:
  postgres:
    # ...
```

This locks the project name to `it-selfservice` even if the directory is renamed. We flip this to `ipsolis` later.

### 1.2 — Pin volume names in the `volumes:` block

```yaml
# docker-compose.yml (bottom of file)
volumes:
  postgres_data:
    name: it-selfservice_postgres_data
  redis_data:
    name: it-selfservice_redis_data
  beat_schedule:
    name: it-selfservice_beat_schedule
  ps_user_modules:
    name: it-selfservice_ps_user_modules
```

Each volume now has an explicit name — independent of the project prefix.

### 1.3 — Commit + push

```bash
git add docker-compose.yml
git commit -m "compose: pin project name and volume names (rename prep)

Decouples volume identity from the compose project name so a future
directory/project rename doesn't orphan persistent data."
git push origin prelive
```

CI deploys the change on LinPre1. `docker compose up -d` is a no-op for volumes
(names already match what they're called on disk), containers are recreated
to pick up the pinned-name metadata. **Verify services are green after this
deploy** before moving on — if something is off here, we don't want to rename
on top of it.

---

## 2. Maintenance window — announce downtime

Tell anyone who might hit the URL that prelive will be down for ~15 min.

Snapshot the current state for rollback reference:

```bash
cd /opt/it-selfservice
docker compose ps > /tmp/pre-rename-ps.txt
docker volume ls --filter name=it-selfservice > /tmp/pre-rename-volumes.txt
cat /tmp/pre-rename-ps.txt
cat /tmp/pre-rename-volumes.txt
```

---

## 3. Fresh backup

```bash
cd /opt/it-selfservice
BACKUP_FILE="/opt/it-selfservice/backups/preRenamePath_$(date +%Y%m%d_%H%M%S).sql.gz"
docker compose exec -T postgres pg_dump -U xpuser -d ipsolis | gzip > "$BACKUP_FILE"
ls -la "$BACKUP_FILE"
```

---

## 4. Stop everything and do the rename

```bash
cd /opt/it-selfservice

# 4a. Stop the whole stack
docker compose -f docker-compose.yml -f docker-compose.nginx.yml down

# 4b. Confirm no containers from this project remain
docker ps -a --filter label=com.docker.compose.project=it-selfservice

# 4c. Rename the directory on disk
sudo mv /opt/it-selfservice /opt/ipsolis
cd /opt/ipsolis
pwd

# 4d. Edit docker-compose.yml — flip project name from it-selfservice -> ipsolis
#     (leave volume name: entries pinned to it-selfservice_*)
sed -i 's/^name: it-selfservice$/name: ipsolis/' docker-compose.yml
grep "^name:" docker-compose.yml

# 4e. Bring it back up with the new project name
docker compose -f docker-compose.yml -f docker-compose.nginx.yml up -d
```

---

## 5. Update the GitHub `PRELIVE_PATH` secret

The deploy-to-prelive workflow uses `${{ secrets.PRELIVE_PATH }}` in `cd` commands. Update it **immediately** after step 4 so the next CI run lands in the right place.

1. GitHub UI: **Settings → Secrets and variables → Actions → PRELIVE_PATH**
2. Change value from `/opt/it-selfservice` to `/opt/ipsolis`
3. Save

(Trying a CI deploy before this is updated will fail with `cd: no such directory`.)

---

## 6. Verify

```bash
cd /opt/ipsolis

echo "=== project name and containers ==="
docker compose ps
docker compose ls

echo; echo "=== volumes are the OLD names, still attached ==="
docker volume ls --filter name=it-selfservice

echo; echo "=== api healthy? ==="
for i in $(seq 1 30); do
  STATUS=$(docker inspect --format='{{.State.Health.Status}}' xp_api 2>/dev/null)
  echo "  try $i: api=$STATUS"
  [ "$STATUS" = "healthy" ] && break
  sleep 2
done

echo; echo "=== external URL check ==="
curl -fsk https://ipsolis-pre.xenpool.local/health; echo

echo; echo "=== DB has data? ==="
docker compose exec -T postgres psql -U xpuser -d ipsolis -c \
  "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname;"
```

Green checklist:
- `docker compose ls` shows project **`ipsolis`** (not `it-selfservice`)
- All 7 containers healthy
- Volumes still carry `it-selfservice_*` names (intentional — data survives)
- External URL returns 200 OK
- Tables show the expected row counts (unchanged from before the rename)

Trigger a test CI deploy from GitHub (Actions → Deploy to prelive → Run workflow) and confirm it lands in `/opt/ipsolis/` and stays green.

---

## 7. Commit the project-name flip

Commit the `sed` change from step 4d so the repo reflects reality:

```bash
cd /opt/ipsolis
git add docker-compose.yml
git commit -m "compose: flip project name to ipsolis after on-disk rename

Volume names remain pinned to it-selfservice_* so persistent data
stays attached."
git push origin prelive
```

The CI run this triggers will be a no-op (container config unchanged).

---

## 8. Rollback (if needed)

The only non-reversible step is the `sudo mv` in 4c. To roll back:

```bash
cd /opt
docker compose -f ipsolis/docker-compose.yml -f ipsolis/docker-compose.nginx.yml down 2>/dev/null || true
sudo mv /opt/ipsolis /opt/it-selfservice
cd /opt/it-selfservice
# Revert docker-compose.yml project-name line if needed
sed -i 's/^name: ipsolis$/name: it-selfservice/' docker-compose.yml
docker compose -f docker-compose.yml -f docker-compose.nginx.yml up -d
# Revert the GitHub PRELIVE_PATH secret back to /opt/it-selfservice
```

Because the volumes are still named `it-selfservice_*` regardless of rollback, no data migration is involved either way. Postgres data is safe through both directions.

---

## 9. Optional — fully clean up volume names later

Once you're confident the rename is stable, you can migrate each volume to a new `ipsolis_*` name for cosmetic consistency. This is **not** required and adds downtime + risk; skip unless aesthetics matter.

For each volume (example: `postgres_data`):

```bash
cd /opt/ipsolis
docker compose stop api worker beat flower

# Create a new, empty volume with the target name
docker volume create ipsolis_postgres_data

# Copy data across using a throwaway container
docker run --rm \
  -v it-selfservice_postgres_data:/from:ro \
  -v ipsolis_postgres_data:/to \
  alpine sh -c "cp -a /from/. /to/"

# Update docker-compose.yml: change name: it-selfservice_postgres_data -> ipsolis_postgres_data
sed -i 's/name: it-selfservice_postgres_data/name: ipsolis_postgres_data/' docker-compose.yml

# Bring everything up on the new volume
docker compose -f docker-compose.yml -f docker-compose.nginx.yml up -d

# After verifying health, prune the old volume
docker volume rm it-selfservice_postgres_data
```

Repeat for `redis_data`, `beat_schedule`, `ps_user_modules`. Commit the compose changes.

---

## 10. Documentation follow-ups

After the rename sticks:

- Update `docs/DEPLOYMENT.md` references from `/opt/ipsolis` if they're still stale (most should already be correct after the initial rebrand commit).
- Update any internal wiki / runbooks that reference `/opt/it-selfservice`.
- The backup files in `backups/` keep their historical names — no action needed.
