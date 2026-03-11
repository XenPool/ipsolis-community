from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.asset import AssetPool, AssetStatus, AssetType
from app.schemas.asset import AssetPoolRead, AssetTypeRead

router = APIRouter(prefix="/assets", tags=["assets"])


# ── Asset Types ───────────────────────────────────────────────────────────────

@router.get("/types", response_model=list[AssetTypeRead])
async def list_asset_types(db: AsyncSession = Depends(get_db)) -> list[AssetType]:
    """Returns all available asset types."""
    result = await db.execute(select(AssetType).order_by(AssetType.name))
    return list(result.scalars().all())


@router.get("/types/{type_id}", response_model=AssetTypeRead)
async def get_asset_type(
    type_id: int, db: AsyncSession = Depends(get_db)
) -> AssetType:
    result = await db.execute(select(AssetType).where(AssetType.id == type_id))
    asset_type = result.scalar_one_or_none()
    if not asset_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AssetType not found")
    return asset_type


# ── Asset Pool ────────────────────────────────────────────────────────────────

@router.get("/pool", response_model=list[AssetPoolRead])
async def list_pool(
    asset_type_id: int | None = None,
    status_filter: AssetStatus | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[AssetPool]:
    """Returns all assets in the pool (optionally filtered by type/status)."""
    query = select(AssetPool)
    if asset_type_id:
        query = query.where(AssetPool.asset_type_id == asset_type_id)
    if status_filter:
        query = query.where(AssetPool.status == status_filter)
    query = query.order_by(AssetPool.name)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/pool/{asset_id}", response_model=AssetPoolRead)
async def get_asset(
    asset_id: int, db: AsyncSession = Depends(get_db)
) -> AssetPool:
    result = await db.execute(select(AssetPool).where(AssetPool.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return asset


@router.get("/pool/stats/summary")
async def pool_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """Zusammenfassung: Wie viele Assets pro Status."""
    result = await db.execute(select(AssetPool))
    assets = list(result.scalars().all())

    stats: dict[str, int] = {}
    for asset in assets:
        key = asset.status.value
        stats[key] = stats.get(key, 0) + 1

    return {
        "total": len(assets),
        "by_status": stats,
    }
