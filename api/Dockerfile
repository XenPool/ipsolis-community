FROM python:3.12-slim-bookworm

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Enable OpenSSL legacy provider (required for NTLM/MD4 in ldap3 on Bookworm)
RUN printf 'openssl_conf = openssl_init\n\n[openssl_init]\nproviders = provider_sect\n\n[provider_sect]\ndefault = default_sect\nlegacy = legacy_sect\n\n[default_sect]\nactivate = 1\n\n[legacy_sect]\nactivate = 1\n' \
    > /etc/ssl/openssl_legacy.cnf
ENV OPENSSL_CONF=/etc/ssl/openssl_legacy.cnf

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Version (passed from docker-compose via build arg)
ARG APP_VERSION=0.0.0
ENV APP_VERSION=${APP_VERSION}

# Alembic (migrations)
COPY alembic/ ./alembic/
COPY alembic.ini .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
