# ip·Solis -- Production Deployment Guide

This guide walks you through setting up the ip·Solis platform on a fresh on-premises server. No prior knowledge of the codebase is required.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Get the Software](#2-get-the-software)
3. [Configure Environment Variables](#3-configure-environment-variables)
4. [SSL / TLS Certificate Setup](#4-ssl--tls-certificate-setup)
5. [Create the Production Compose Overlay](#5-create-the-production-compose-overlay)
6. [Start the Stack](#6-start-the-stack)
7. [Initial Admin Setup](#7-initial-admin-setup)
8. [Entra ID SSO (Portal Authentication)](#8-entra-id-sso-portal-authentication)
9. [Verify the Deployment](#9-verify-the-deployment)
10. [Backup & Maintenance](#10-backup--maintenance)
11. [Updating to a New Version](#11-updating-to-a-new-version)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisites

### Server Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Linux (Debian/Ubuntu recommended) | Ubuntu 22.04 LTS or newer |
| CPU | 2 cores | 4 cores |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB | 50 GB (depends on number of managed assets) |

### Software

Install the following before proceeding:

- **Docker Engine** >= 24.0 -- [Install Docker](https://docs.docker.com/engine/install/)
- **Docker Compose** >= 2.20 (included with Docker Engine)
- **Git** -- to clone the repository

Verify your installation:

```bash
docker --version        # Docker version 24.x or higher
docker compose version  # Docker Compose version v2.20 or higher
git --version
```

### Network Requirements

The server needs outbound access to:

| Destination | Purpose |
|-------------|---------|
| Your Active Directory / LDAP server (port 389 or 636) | User validation, manager lookup, group membership |
| Your SMTP relay | Email notifications |
| vSphere / XenServer (if applicable) | VM lifecycle automation |
| SCCM server (if applicable) | Task sequence triggers |

Inbound: ports **80** and **443** must be reachable from your users' browsers.

---

## 2. Get the Software

Clone the repository onto your server:

```bash
cd /opt
git clone <repository-url> ipsolis
cd ipsolis
```

---

## 3. Configure Environment Variables

Copy the example file and edit it:

```bash
cp .env.example .env
nano .env
```

### Required settings to change

```ini
# Secure database credentials
POSTGRES_PASSWORD=<generate-a-strong-password>

# Secure API secrets -- use random strings of 32+ characters
API_SECRET_KEY=<random-string-min-32-chars>
WEBHOOK_SECRET_TOKEN=<random-string>
ADMIN_API_KEY=<random-string-min-32-chars>

# CORS -- set to your production domain
CORS_ORIGINS=https://selfservice.yourcompany.com
FLOWER_PASSWORD=<strong-password>
```

> **Tip**: Generate secure passwords with:
> ```bash
> openssl rand -base64 32
> ```

## 4. SSL / TLS Certificate Setup

The platform runs behind an nginx reverse proxy that terminates SSL. You need a TLS certificate and private key.

### Option A: Internal / Self-Signed Certificate (Intranet)

If your server is only accessible within your corporate network, use [mkcert](https://github.com/FiloSottile/mkcert) to generate a trusted certificate:

```bash
# Install mkcert (one-time)
# Ubuntu/Debian:
apt install -y libnss3-tools
curl -JLO "https://dl.filippo.io/mkcert/latest?for=linux/amd64"
chmod +x mkcert-v*-linux-amd64
mv mkcert-v*-linux-amd64 /usr/local/bin/mkcert

# Install the local CA into your system trust store
mkcert -install

# Generate the certificate for your hostname
mkdir -p certs
mkcert -cert-file certs/cert.pem -key-file certs/key.pem selfservice.yourcompany.com
```

> **Important**: For browsers on other machines to trust this certificate, you must
> distribute the root CA (`mkcert -CAROOT` shows the path) to client machines via
> Group Policy or your enterprise CA trust store.

### Option B: Certificate from your Enterprise CA (Recommended for production)

If your organization runs an internal Certificate Authority (e.g., Active Directory Certificate Services):

1. Generate a CSR on the server:
   ```bash
   mkdir -p certs
   openssl req -new -newkey rsa:2048 -nodes \
     -keyout certs/key.pem \
     -out certs/server.csr \
     -subj "/CN=selfservice.yourcompany.com"
   ```
2. Submit `certs/server.csr` to your CA and obtain the signed certificate.
3. Save the signed certificate as `certs/cert.pem`.
4. If your CA provides an intermediate/chain certificate, append it to `cert.pem`:
   ```bash
   cat signed-cert.pem intermediate-ca.pem > certs/cert.pem
   ```

### Option C: Let's Encrypt (Public-facing servers)

If your server is publicly accessible, you can use free certificates from Let's Encrypt:

```bash
apt install -y certbot
certbot certonly --standalone -d selfservice.yourcompany.com

# Symlink into the certs directory
mkdir -p certs
ln -sf /etc/letsencrypt/live/selfservice.yourcompany.com/fullchain.pem certs/cert.pem
ln -sf /etc/letsencrypt/live/selfservice.yourcompany.com/privkey.pem certs/key.pem
```

Set up auto-renewal:

```bash
# Test renewal
certbot renew --dry-run

# Add a cron job to reload nginx after renewal
echo "0 3 * * * certbot renew --quiet --post-hook 'docker exec xp_nginx nginx -s reload'" | crontab -
```

### Configure nginx

Create the nginx config for your hostname:

```bash
mkdir -p nginx
cat > nginx/nginx.conf << 'EOF'
server {
    listen 80;
    server_name selfservice.yourcompany.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name selfservice.yourcompany.com;

    ssl_certificate     /etc/nginx/certs/cert.pem;
    ssl_certificate_key /etc/nginx/certs/key.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # WebSocket / HTMX support
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    location / {
        proxy_pass         http://api:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
EOF
```

> Replace `selfservice.yourcompany.com` with your actual hostname in both the
> nginx config and the certificate generation step.

---

## 5. Create the Production Compose Overlay

Create a production overlay that adds the nginx service. This file extends the
base `docker-compose.yml` without modifying it:

```bash
cat > docker-compose.prod.yml << 'EOF'
# Production overlay -- adds nginx reverse proxy with SSL.
# Usage: docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

services:
  api:
    # Remove dev hot-reload volumes and use built-in code from Docker image
    volumes: []
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

  worker:
    # Remove dev hot-reload volumes, keep persistent PowerShell modules
    volumes:
      - ps_user_modules:/root/.local/share/powershell/Modules
      - ./scripts:/app/scripts:ro

  # Beat schedule lives in Redis (celery-redbeat); no on-disk schedule
  # volume needed. See ENTERPRISE_FEATURES.md → "HA Beat scheduler" for
  # multi-replica scaling: `docker compose up -d --scale beat=2`.

  nginx:
    image: nginx:alpine
    container_name: xp_nginx
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./certs:/etc/nginx/certs:ro
    depends_on:
      api:
        condition: service_healthy
EOF
```

> **Note**: The production compose overlay adds nginx for SSL termination.
> All application code is baked into the Docker images at build time.

---

## 6. Start the Stack

```bash
cd /opt/ipsolis

# Build and start all services
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d

# Run database migrations
docker compose exec -T api alembic upgrade head

# Verify all containers are running
docker compose ps
```

Expected output -- all services should show `Up (healthy)`:

```
NAME             STATUS
xp_postgres      Up (healthy)
xp_redis         Up (healthy)
xp_api           Up (healthy)
xp_worker        Up (healthy)
ipsolis-beat-1   Up
xp_nginx         Up
```

The beat container has no fixed `container_name` so it can be scaled
for HA — see [ENTERPRISE_FEATURES.md → HA Beat scheduler](ENTERPRISE_FEATURES.md#ha-beat-scheduler-multi-replica-with-celery-redbeat).

Verify the application:

```bash
# Direct API health check
curl -f http://localhost:8000/health

# Through nginx (HTTPS)
curl -fsk https://selfservice.yourcompany.com/health
```

---

## 7. Initial Admin Setup

### First-run admin account (RBAC)

Open **https://selfservice.yourcompany.com/ui/** in your browser. On
the very first visit (when `admin_users` is empty), the login page
renders a **"Create first administrator"** form instead of the
normal sign-in form. Fill in:

| Field | Notes |
|---|---|
| Username | 3–128 chars, allowed: `[a-zA-Z0-9._@-]+`. Lower-cased at write time. |
| Password | ≥ 12 chars. PBKDF2-SHA256 / 600k iterations (OWASP-2023). |
| Confirm password | Must match. |

Submitting creates the first **superadmin** and auto-logs you in.
This is idempotent against races — if two operators hit the form at
the same time, only one wins; the other gets a "use the sign-in
form" message.

After the first superadmin exists, the form switches to the regular
username + password sign-in.

### Add additional admin users

Once signed in, navigate to **Admin Users** in the left nav
(superadmin-only). Create per-user accounts in the role appropriate
to each operator:

```
superadmin > admin > approver > auditor > helpdesk
```

See **[ENTERPRISE_FEATURES.md → Admin RBAC](ENTERPRISE_FEATURES.md#admin-rbac-roles-acl-grants-sod-password-policy)**
for the full role ladder, per-asset-type ACL grants, separation-of-duties
enforcement, and password-policy options.

### Legacy `ADMIN_API_KEY` fallback

The `ADMIN_API_KEY` from `.env` continues to authenticate as a
**virtual superadmin** even after first-run setup, so existing
scripts / `X-Admin-Key` headers don't break on upgrade. To use it
on the login page: leave **Username** blank, paste the key into
**Password**. Audit attribution shows up as `admin:legacy_key` so
auditors can tell when the fallback path was used.

For new integrations prefer **Per-integration API tokens** (Admin UI
→ *API Tokens*) — named, expiring, revocable bearer tokens with
optional role binding and scoped permissions. The legacy single
shared key is kept for back-compat only.

### Configuration Checklist

Navigate to **Admin > Settings** and configure the following:

#### Active Directory (Required)

| Setting | Description | Example |
|---------|-------------|---------|
| `ad.server` | AD domain controller hostname or IP | `dc01.yourcompany.com` |
| `ad.port` | LDAP port | `389` (or `636` for LDAPS) |
| `ad.base_dn` | Search base DN | `DC=yourcompany,DC=com` |
| `ad.domain` | NetBIOS domain name | `YOURCOMPANY` |
| `ad.username` | Service account (sAMAccountName) | `svc-selfservice` |
| `ad.password` | Service account password | *(marked as secret)* |
| `ad.use_ssl` | Use LDAPS | `true` or `false` |

> The service account needs **read-only** access to user objects (attributes:
> `mail`, `displayName`, `sAMAccountName`, `userPrincipalName`, `manager`, `memberOf`).

#### SMTP (Required for notifications)

| Setting | Description | Example |
|---------|-------------|---------|
| `smtp.host` | SMTP relay hostname | `smtp.yourcompany.com` |
| `smtp.port` | SMTP port | `587` |
| `smtp.user` | SMTP username (if auth required) | `selfservice@yourcompany.com` |
| `smtp.password` | SMTP password | *(marked as secret)* |
| `smtp.tls` | Use STARTTLS | `true` |
| `smtp.from` | Sender email address | `noreply@yourcompany.com` |
| `smtp.from_name` | Sender display name | `ip·Solis` |

#### Email Templates

Navigate to **Admin > Email Templates** to customize notification emails.
Default templates are created during migration. You can edit the subject line
and body using `{{variable}}` placeholders.

#### Portal Settings

| Setting | Description | Default |
|---------|-------------|---------|
| `portal.max_advance_days` | How far ahead users can schedule orders | `0` (unlimited) |
| `portal.app_title` | Application title shown in the portal | `ip·Solis` |

### Create your first Asset Type

1. Go to **Admin > Asset Types > New**
2. Fill in the name, description, and category
3. Configure the automation strategy (Group Access, Runbook, or Composite)
4. Set approval requirements if needed
5. Optionally restrict access with an Eligible Requestors group DN
6. Save

### Create Runbooks (if applicable)

If your asset types use runbook automation:

1. Go to **Admin > Runbooks > New**
2. Define the steps (PowerShell modules or built-in modules)
3. Link the runbook to an asset type

---

## 8. Entra ID SSO (Portal Authentication)

The self-service portal supports Microsoft Entra ID (Azure AD) for single sign-on.

### Register an App in Entra ID

1. Go to the [Azure Portal](https://portal.azure.com) > **App registrations** > **New registration**
2. Name: `ip·Solis`
3. Redirect URI: `https://selfservice.yourcompany.com/portal/auth/callback` (Web)
4. Note down the **Application (client) ID** and **Directory (tenant) ID**
5. Under **Certificates & secrets**, create a new client secret

### Configure in Admin UI

Navigate to **Admin > Settings** and set:

| Setting | Description |
|---------|-------------|
| `entra.mode` | `entra_only` (Entra ID login required) or `entra_with_onprem` (Entra ID + on-prem LDAP check) |
| `entra.client_id` | Application (client) ID |
| `entra.client_secret` | Client secret value *(marked as secret)* |
| `entra.tenant_id` | Directory (tenant) ID |
| `entra.redirect_uri` | `https://selfservice.yourcompany.com/portal/auth/callback` |
| `entra.allowed_domains` | Comma-separated list of allowed email domains, e.g. `yourcompany.com` |

Use the **Test Entra Connection** button to verify the configuration.

> When `entra.mode` is set to `disabled`, the portal is open to anyone
> on the network with a shared anonymous identity — every visitor sees
> and can act on the same set of orders. Only use this for demo /
> air-gapped lab deployments. For multi-user production, set
> `entra.mode = entra_only`.

---

## 9. Verify the Deployment

Run through this checklist to confirm everything works:

- [ ] **HTTPS**: `https://selfservice.yourcompany.com` loads with a valid certificate
- [ ] **Admin UI**: `https://selfservice.yourcompany.com/ui/` is accessible
- [ ] **First-run setup**: visiting the admin login renders the "Create first administrator" form (or, if already done, the regular sign-in form with no error)
- [ ] **Setup checklist**: the dashboard shows the in-app setup checklist; tick off Essential items as you configure them
- [ ] **Portal login**: Users can sign in via Entra ID SSO
- [ ] **AD lookup**: On the order form, user validation (deputy, RDP, admin fields) resolves names
- [ ] **Email**: Submit a test order and confirm notification email arrives
- [ ] **Health check**: `curl -fsk https://selfservice.yourcompany.com/health` returns `{"status": "ok"}`
- [ ] *(optional)* **API tokens**: issue a per-integration token for any automation that previously used `X-Admin-Key`
- [ ] *(optional)* **SIEM streaming**: configure under *Settings → Compliance* if you have Splunk / Sentinel / a generic webhook receiver
- [ ] *(optional)* **Prometheus**: scrape `/metrics` from your monitoring; the dashboard ships in [docs/grafana/](grafana/)

---

## 10. Backup & Maintenance

### Database Backup

The PostgreSQL data is stored in a Docker volume (`postgres_data`). Back it up regularly:

```bash
# Dump the database
docker compose exec -T postgres pg_dump -U xpuser ipsolis > backup_$(date +%Y%m%d).sql

# Restore from backup
cat backup_20260414.sql | docker compose exec -T postgres psql -U xpuser ipsolis
```

### Logs

View container logs:

```bash
# All services
docker compose logs --tail=50

# Specific service
docker compose logs api --tail=100 -f    # follow mode
docker compose logs worker --tail=100
```

### Disk Cleanup

Periodically remove old Docker images:

```bash
docker image prune -f
```

---

## 11. Updating to a New Version

```bash
cd /opt/ipsolis

# Pull the latest code
git pull origin main

# Rebuild and restart
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d

# Run any new database migrations
docker compose exec -T api alembic upgrade head

# Reload nginx to pick up new container IPs
docker compose exec -T nginx nginx -s reload

# Verify health
curl -fsk https://selfservice.yourcompany.com/health
```

> Migrations are safe to run multiple times -- Alembic tracks which have
> already been applied and skips them. Each feature slice typically
> ships its own migration; review `api/alembic/versions/` between
> upgrades for the changeset, and `docker compose exec api alembic
> history` to see the chain.

### Backing up before upgrade

Always snapshot the database first — `pg_dump` from the Postgres
container, or use the in-app **Maintenance → Backups** page (Admin UI)
which writes a timestamped SQL dump to the bind-mounted `./backups/`
directory. Configure a daily backup schedule in the same UI so the
snapshot is fresh when an unexpected regression appears.

### Beat HA failover during the restart

If you run multiple Beat replicas (`--scale beat=N`), `docker compose
up --build -d` rolls the containers one at a time and the leader lock
hands over to the surviving replica within ~13 s (see
[ENTERPRISE_FEATURES.md → HA Beat scheduler](ENTERPRISE_FEATURES.md#ha-beat-scheduler-multi-replica-with-celery-redbeat)).
For single-Beat installs there's a brief gap during the restart
where periodic tasks aren't running — usually invisible since cadences
are minutes / hours.

---

## 12. Troubleshooting

### Container won't start

```bash
# Check container status and exit codes
docker compose ps -a

# Check logs for the failing service
docker compose logs <service-name> --tail=50
```

### Health check fails through nginx but API is healthy

Nginx may have cached the old container IP. Reload it:

```bash
docker compose exec -T nginx nginx -s reload
```

### Database connection errors

```bash
# Check if postgres is running
docker compose exec postgres pg_isready -U xpuser

# Verify the connection from the API container
docker compose exec api python -c "
from sqlalchemy import create_engine, text
e = create_engine('postgresql://xpuser:<password>@postgres:5432/ipsolis')
with e.connect() as c: print(c.execute(text('SELECT 1')).scalar())
"
```

### AD / LDAP connection issues

1. Verify network connectivity from the container:
   ```bash
   docker compose exec api curl -v telnet://dc01.yourcompany.com:389
   ```
2. Check the AD settings in Admin > Settings
3. Review API logs for LDAP errors:
   ```bash
   docker compose logs api 2>&1 | grep -i "ldap\|ad_lookup"
   ```

### Emails not sending

1. Verify SMTP settings in Admin > Settings
2. Check worker logs for SMTP errors:
   ```bash
   docker compose logs worker 2>&1 | grep -i "smtp\|mail\|notification"
   ```
3. Ensure the server can reach the SMTP relay:
   ```bash
   docker compose exec api curl -v telnet://smtp.yourcompany.com:587
   ```

### Permission denied on certs directory

```bash
chmod 644 certs/cert.pem
chmod 600 certs/key.pem
```
