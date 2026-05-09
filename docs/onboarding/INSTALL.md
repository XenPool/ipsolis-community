# ip·Solis Business Edition — Installation Guide

Deploy a production instance in under 30 minutes.  
You do **not** need to clone the repository or install any build tools.

---

## Prerequisites

### Server

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Linux (Debian/Ubuntu) | Ubuntu 22.04 LTS or newer |
| CPU | 2 cores | 4 cores |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB | 50 GB |

### Software

```bash
# Docker Engine >= 24.0 + Docker Compose >= 2.20
curl -fsSL https://get.docker.com | sh

# Verify
docker --version
docker compose version
```

### Network

| Outbound | Purpose |
|----------|---------|
| `ghcr.io` (port 443) | Pull container images |
| Your AD / LDAP server | User lookups |
| Your SMTP relay | Email notifications |
| vSphere / XenServer (optional) | VM lifecycle automation |
| SCCM server (optional) | Task-sequence triggers |

Inbound: ports **80** and **443** must be reachable from your users' browsers.

---

## Step 1 — Authenticate with the Registry

You received a registry token from XenPool. Log in once on your server:

```bash
echo "YOUR_REGISTRY_TOKEN" | docker login ghcr.io \
  -u ipsolis-deploy \
  --password-stdin
```

Expected output: `Login Succeeded`

---

## Step 2 — Create the Deployment Directory

```bash
mkdir -p /opt/ipsolis/{backups,licenses,locales,nginx/ssl}
cd /opt/ipsolis
```

---

## Step 3 — Copy the Compose and Environment Files

From the files included in your onboarding package:

```bash
cp docker-compose.pro.yml /opt/ipsolis/docker-compose.yml
cp .env.example           /opt/ipsolis/.env
```

---

## Step 4 — Configure Environment Variables

```bash
nano /opt/ipsolis/.env
```

Fill in all `CHANGE_ME_*` values:

| Variable | What to set |
|----------|-------------|
| `POSTGRES_PASSWORD` | Strong random password (20+ chars) |
| `API_SECRET_KEY` | Random string, 32+ characters |
| `ADMIN_API_KEY` | Your admin password (32+ chars) |
| `FLOWER_PASSWORD` | Password for the Celery monitoring UI |
| `CORS_ORIGINS` | `https://ipsolis.yourcompany.com` |

Leave `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` as-is unless you use an external Redis.

---

## Step 5 — TLS Certificate

Place your certificate and key in `/opt/ipsolis/nginx/ssl/`:

```bash
cp your-cert.pem /opt/ipsolis/nginx/ssl/cert.pem
cp your-key.pem  /opt/ipsolis/nginx/ssl/key.pem
```

> **Self-signed for testing:**
> ```bash
> openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
>   -keyout /opt/ipsolis/nginx/ssl/key.pem \
>   -out    /opt/ipsolis/nginx/ssl/cert.pem \
>   -subj "/CN=ipsolis.yourcompany.com"
> ```

Then create the Nginx config at `/opt/ipsolis/nginx/nginx.conf`:

```nginx
server {
    listen 80;
    server_name ipsolis.yourcompany.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name ipsolis.yourcompany.com;

    ssl_certificate     /etc/nginx/ssl/cert.pem;
    ssl_certificate_key /etc/nginx/ssl/key.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    client_max_body_size 2g;

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
```

---

## Step 6 — Start the Stack

```bash
cd /opt/ipsolis
docker compose pull          # download the latest images
docker compose up -d         # start all services
docker compose ps            # confirm everything is healthy
```

All services should reach `healthy` within about 60 seconds.  
Check logs if any service stays unhealthy:

```bash
docker compose logs api --tail=50
docker compose logs worker --tail=50
```

---

## Step 7 — First Admin Login

Open `https://ipsolis.yourcompany.com/ui/` in your browser.

Log in with:
- **Username:** `admin`
- **Password:** the value you set for `ADMIN_API_KEY` in `.env`

---

## Step 8 — Upload Your License

1. Go to **Admin → License** (or `/ui/license`).
2. Upload the `.lic` file you received from XenPool.
3. The page shows the license tier, seat count, and expiry date once accepted.

Without a valid license the platform runs in Community mode — Business features
(SCCM, ServiceNow webhook, SIEM export, certifications) are inactive.

---

## Step 9 — Configure Integrations

Go to **Admin → Settings** to connect your environment:

- **Active Directory** — server, bind DN, search base, credentials
- **Email (SMTP)** — relay host, credentials, sender address
- **Entra ID** (optional) — portal SSO for end users
- **vSphere / XenServer** (optional) — VM lifecycle automation
- **SCCM** (optional) — task-sequence integration

All settings are stored in the database and take effect immediately — no container restart needed.

---

## Updating to a New Version

```bash
cd /opt/ipsolis

# Option A: pull latest
docker compose pull
docker compose up -d

# Option B: pin a specific release (edit .env first)
#   IPSOLIS_VERSION=1.3.0
docker compose pull
docker compose up -d
```

Database migrations run automatically on api container startup.

---

## Backup & Restore

Backups are configured in **Admin → Maintenance → Backup**.  
Files land in `./backups/` on the host and can also be pushed to S3.

Manual backup at any time:

```bash
docker compose exec api python -c "
from app.tasks import backup_database; backup_database()
"
```

---

## Troubleshooting

**Container exits immediately**
```bash
docker compose logs <service> --tail=100
```
Most common cause: missing or incorrect value in `.env`.

**Registry pull fails with 401**
Your registry token may have expired.  
Contact XenPool support to issue a new token, then re-run `docker login`.

**License not accepted**
Ensure the `.lic` file is unmodified. The file contains a cryptographic
signature — any whitespace or encoding change invalidates it.

**Portal shows "Login disabled"**
Entra ID SSO is not configured yet.  
Go to **Admin → Settings → Authentication** and set `entra.mode = disabled`
to open the portal with a shared anonymous identity while you configure SSO.

**Database connection refused on first start**
PostgreSQL may still be initialising. Wait 30 seconds and retry:
```bash
docker compose restart api worker beat
```

---

## Support

- Documentation: `https://ipsolis.yourcompany.com/docs` (once running)
- Email: support@xenpool.de
- License issues: kontakt@xenpool.de
