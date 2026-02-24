FROM python:3.12-slim-bookworm

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Alembic (migrations)
COPY alembic/ ./alembic/
COPY alembic.ini .

EXPOSE 8000

# Uvicorn with hot-reload (dev) – in production remove --reload
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
