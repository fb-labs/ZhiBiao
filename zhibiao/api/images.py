from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from zhibiao.images import guess_mime, load_image
from zhibiao.models import Account
from zhibiao.security import require_permission

router = APIRouter(prefix="/images", tags=["v2-images"])


@router.get("/{image_id}")
async def get_image(
    image_id: str,
    _: Account = Depends(require_permission("tender.read")),
):
    data = load_image(image_id)
    if not data:
        raise HTTPException(status_code=404, detail="图片不存在")
    return Response(
        content=data,
        media_type=guess_mime(data),
        headers={"Cache-Control": "public, max-age=604800"},
    )
