# XenPool IT Selfservice -- Production Deployment Guide

This guide walks you through setting up the IT Selfservice platform on a fresh on-premises server. No prior knowledge of the codebase is required.

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
git clone <repository-url> it-selfservice
cd it-selfservice
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
# Set to production -- this disables all mocks
ENVIRONMENT=production

# Secure database credentials
POSTGRES_PASSWORD=<generate-a-strong-password>

# Secure API secrets -- use random strings of 32+ characters
API_SECRET_KEY=<random-string-min-32-chars>
WEBHOOK_SECRET_TOKEN=<random-string>
ADMIN_API_KEY=<random-string-min-32-chars>

# CORS -- set to your production domain
CORS_ORIGINS=https://selfservice.yourcompany.com

# Flower monitoring password (optional, dev-only service)
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

  beat:
    volumes:
      - beat_schedule:/app/beat_schedule

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

> **Key differences from development**:
> - Hot-reload volumes are removed (code is baked into the Docker image)
> - API runs with 4 Uvicorn workers instead of `--reload`
> - Flower monitoring UI is not started (it's in the `dev` profile)

---

## 6. Start the Stack

```bash
cd /opt/it-selfservice

# Build and start all services
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d

# Run database migrations
docker compose exec -T api alembic upgrade head

# Verify all containers are running
docker compose ps
```

Expected output -- all services should show `Up (healthy)`:

```
NAME          STATUS
xp_postgres   Up (healthy)
xp_redis      Up (healthy)
xp_api        Up (healthy)
xp_worker     Up (healthy)
xp_beat       Up
xp_nginx      Up
```

Verify the application:

```bash
# Direct API health check
curl -f http://localhost:8000/health

# Through nginx (HTTPS)
curl -fsk https://selfservice.yourcompany.com/health
```

---

## 7. Initial Admin Setup

### Access the Admin UI

Open **https://selfservice.yourcompany.com/ui/** in your browser.

The Admin UI is protected by the `ADMIN_API_KEY` you set in `.env`. When
prompted, enter the key in the `X-Admin-Key` header field.

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
| `smtp.from_name` | Sender display name | `IT Selfservice` |

#### Email Templates

Navigate to **Admin > Email Templates** to customize notification emails.
Default templates are created during migration. You can edit the subject line
and body using `{{variable}}` placeholders.

#### Portal Settings

| Setting | Description | Default |
|---------|-------------|---------|
| `portal.max_advance_days` | How far ahead users can schedule orders | `0` (unlimited) |
| `portal.app_title` | Application title shown in the portal | `IT Selfservice` |

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
2. Name: `IT Selfservice`
3. Redirect URI: `https://selfservice.yourcompany.com/auth/callback` (Web)
4. Note down the **Application (client) ID** and **Directory (tenant) ID**
5. Under **Certificates & secrets**, create a new client secret

### Configure in Admin UI

Navigate to **Admin > Settings** and set:

| Setting | Description |
|---------|-------------|
| `entra.mode` | `enabled` |
| `entra.client_id` | Application (client) ID |
| `entra.client_secret` | Client secret value *(marked as secret)* |
| `entra.tenant_id` | Directory (tenant) ID |
| `entra.redirect_uri` | `https://selfservice.yourcompany.com/auth/callback` |
| `entra.allowed_domains` | Comma-separated list of allowed email domains, e.g. `yourcompany.com` |

Use the **Test Entra Connection** button to verify the configuration.

> When `entra.mode` is set to `disabled`, the portal uses a mock user for
> development purposes. **Never use this in production.**

---

## 9. Verify the Deployment

Run through this checklist to confirm everything works:

- [ ] **HTTPS**: `https://selfservice.yourcompany.com` loads with a valid certificate
- [ ] **Admin UI**: `https://selfservice.yourcompany.com/ui/` is accessible
- [ ] **Portal login**: Users can sign in via Entra ID SSO
- [ ] **AD lookup**: On the order form, user validation (deputy, RDP, admin fields) resolves names
- [ ] **Email**: Submit a test order and confirm notification email arrives
- [ ] **Health check**: `curl -fsk https://selfservice.yourcompany.com/health` returns `{"status": "ok"}`

---

## 10. Backup & Maintenance

### Database Backup

The PostgreSQL data is stored in a Docker volume (`postgres_data`). Back it up regularly:

```bash
# Dump the database
docker compose exec -T postgres pg_dump -U xpuser itselfservice > backup_$(date +%Y%m%d).sql

# Restore from backup
cat backup_20260414.sql | docker compose exec -T postgres psql -U xpuser itselfservice
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
cd /opt/it-selfservice

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
> already been applied and skips them.

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
e = create_engine('postgresql://xpuser:<password>@postgres:5432/itselfservice')
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
