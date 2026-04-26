"""Seed OpenTelemetry tracing config keys.

Tracing is opt-in: ``otel.enabled = false`` until the operator
configures an exporter. Two exporter modes:

* ``otel.endpoint`` — OTLP HTTP collector URL (Jaeger, Tempo, SigNoz,
  Honeycomb, …). Production target.
* ``otel.console_exporter = true`` — print spans to stdout for local
  verification without a collector.

Either, both, or neither can be enabled.

Revision ID: 0060
Revises: 0059
Create Date: 2026-04-26
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0060"
down_revision: Union[str, None] = "0059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO app_config (key, value, description, is_secret, updated_at)
        VALUES
        ('otel.enabled', 'false',
         'Master switch for OpenTelemetry tracing. Restart the API after toggling.',
         false, NOW()),
        ('otel.service_name', 'ipsolis-api',
         'Service name written into every span''s resource attributes.',
         false, NOW()),
        ('otel.endpoint', '',
         'OTLP HTTP collector endpoint, e.g. https://otel-collector.example.com:4318/v1/traces. Empty disables OTLP export.',
         false, NOW()),
        ('otel.headers', '',
         'Optional headers for the OTLP exporter (one key=value per line). Used for vendor API keys.',
         true, NOW()),
        ('otel.console_exporter', 'false',
         'Print spans to stdout for local verification. Useful for first-run debugging without a collector.',
         false, NOW())
        ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM app_config WHERE key IN (
          'otel.enabled', 'otel.service_name', 'otel.endpoint',
          'otel.headers', 'otel.console_exporter'
        )
    """)
