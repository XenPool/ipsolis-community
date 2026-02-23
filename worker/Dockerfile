FROM python:3.12-slim

WORKDIR /app

# System dependencies + PowerShell Core
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    apt-transport-https \
    && wget -q "https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb" \
    && dpkg -i packages-microsoft-prod.deb \
    && rm packages-microsoft-prod.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends powershell \
    && rm -rf /var/lib/apt/lists/*

# Install PowerCLI (requires PowerShell Core)
RUN pwsh -Command "\
    Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; \
    Install-Module -Name VMware.PowerCLI -Scope AllUsers -Force -AllowClobber; \
    Set-PowerCLIConfiguration -InvalidCertificateAction Ignore -Confirm:\$false; \
    Set-PowerCLIConfiguration -ParticipateInCEIP \$false -Confirm:\$false"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy worker code
COPY tasks/ ./tasks/

EXPOSE 5555

# Default: celery worker (overridden in docker-compose for beat/flower)
CMD ["celery", "-A", "tasks", "worker", "--loglevel=info", "--concurrency=4"]
