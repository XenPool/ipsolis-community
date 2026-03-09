FROM python:3.12-slim-bookworm

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# PowerShell Core – direct .deb from GitHub releases
# Using Debian 12 (bookworm) package – matches base image
RUN PWSH_VERSION=7.4.6 && \
    curl -fsSL "https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell_${PWSH_VERSION}-1.deb_amd64.deb" \
    -o /tmp/powershell.deb && \
    apt-get update && \
    apt-get install -y --no-install-recommends /tmp/powershell.deb && \
    rm /tmp/powershell.deb && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy worker code
COPY tasks/ ./tasks/

EXPOSE 5555

# Default: celery worker (overridden in docker-compose for beat/flower)
CMD ["celery", "-A", "tasks", "worker", "--loglevel=info", "--concurrency=4"]
