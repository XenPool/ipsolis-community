"""Cost / chargeback report.

Aggregates active orders against asset definitions that have
``monthly_cost`` set, grouped by cost center, asset type, and currency.
Two output formats: JSON (consumed by the admin UI table) and CSV
(downloadable for spreadsheet handoff).

"Active" matches the same set used by capacity / quota enforcement —
``pending``, ``pending_approval``, ``scheduled``, ``processing``,
``provisioning``, ``provisioned``, ``delivered``. Cancelled / rejected /
revoked orders never count.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.utils.auth import require_admin_key

router = APIRouter(
    prefix="/admin/cost-report",
    tags=["admin-cost-report"],
    dependencies=[Depends(require_admin_key)],
)

_ACTIVE_ORDER_STATUSES = (
    "pending", "pending_approval", "scheduled",
    "processing", "provisioning", "provisioned", "delivered",
)


async def _query_rows(db: AsyncSession) -> list[dict[str, Any]]:
    """Return one row per (asset_type, cost_center) with active-order
    counts and projected monthly spend.

    Asset definitions without ``monthly_cost`` are excluded — cost-tracking
    is opt-in. Cost center is normalised to ``"(unassigned)"`` for the
    grouping when blank, so they still appear in the report instead of
    silently dropping out.
    """
    sql = """
        SELECT
            COALESCE(NULLIF(at.cost_center, ''), '(unassigned)') AS cost_center,
            at.id      AS asset_type_id,
            at.name    AS asset_type_name,
            at.currency,
            at.monthly_cost,
            COUNT(o.id) AS active_orders,
            COUNT(DISTINCT LOWER(o.user_email)) AS unique_users
        FROM asset_types at
        LEFT JOIN orders o
          ON o.asset_type_id = at.id
         AND o.status::text = ANY(:active_statuses)
        WHERE at.monthly_cost IS NOT NULL
        GROUP BY at.id, at.cost_center, at.name, at.currency, at.monthly_cost
        ORDER BY cost_center ASC, at.name ASC
    """
    rows = await db.execute(text(sql), {"active_statuses": list(_ACTIVE_ORDER_STATUSES)})
    out: list[dict[str, Any]] = []
    for r in rows.mappings().all():
        unit = float(r["monthly_cost"]) if r["monthly_cost"] is not None else 0.0
        active = r["active_orders"] or 0
        out.append({
            "cost_center": r["cost_center"],
            "asset_type_id": r["asset_type_id"],
            "asset_type_name": r["asset_type_name"],
            "currency": r["currency"] or "",
            "unit_monthly_cost": unit,
            "active_orders": active,
            "unique_users": r["unique_users"] or 0,
            "projected_monthly_total": round(unit * active, 2),
        })
    return out


@router.get("", response_model=None)
async def cost_report(
    fmt: str = Query(default="json", regex="^(json|csv)$"),
    db: AsyncSession = Depends(get_db),
) -> Response | dict:
    rows = await _query_rows(db)

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "Cost center", "Asset type", "Currency",
            "Unit monthly cost", "Active orders", "Unique users",
            "Projected monthly total",
        ])
        for r in rows:
            writer.writerow([
                r["cost_center"], r["asset_type_name"], r["currency"],
                f"{r['unit_monthly_cost']:.2f}",
                r["active_orders"], r["unique_users"],
                f"{r['projected_monthly_total']:.2f}",
            ])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="ipsolis-cost-report.csv"'},
        )

    # JSON: also compute per-cost-center totals (per currency) so the UI
    # can render summary rows without re-aggregating client-side.
    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"projected_monthly_total": 0.0, "active_orders": 0, "asset_types": 0}
    )
    for r in rows:
        key = (r["cost_center"], r["currency"])
        t = totals[key]
        t["projected_monthly_total"] += r["projected_monthly_total"]
        t["active_orders"] += r["active_orders"]
        t["asset_types"] += 1

    return {
        "rows": rows,
        "totals": [
            {
                "cost_center": cc,
                "currency": cur,
                "projected_monthly_total": round(v["projected_monthly_total"], 2),
                "active_orders": v["active_orders"],
                "asset_types": v["asset_types"],
            }
            for (cc, cur), v in sorted(totals.items())
        ],
    }
