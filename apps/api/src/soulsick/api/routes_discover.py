from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..core import Core
from .deps import get_core, require_user
from .routes_system import _UUID

router = APIRouter(prefix="/api", tags=["discover"], dependencies=[Depends(require_user)])


class DismissIn(BaseModel):
    mbid: str


@router.get("/discover")
async def recommendations(core: Core = Depends(get_core)) -> dict:
    return await core.discover.recommendations()


@router.post("/discover/refresh")
async def refresh(core: Core = Depends(get_core)) -> dict:
    """Asks ListenBrainz again for every followed artist, now."""
    return {"refreshed": await core.discover.refresh_all()}


@router.post("/discover/dismiss")
async def dismiss(body: DismissIn, core: Core = Depends(get_core)) -> dict:
    await core.discover.dismiss(body.mbid)
    return {"ok": True}


@router.get("/artists/{aid}/similar")
async def similar(aid: int, core: Core = Depends(get_core)) -> list[dict]:
    return await core.discover.similar(aid)


@router.get("/artist-photos/{mbid}")
async def artist_photo(mbid: str, core: Core = Depends(get_core)):
    if not _UUID.fullmatch(mbid):
        raise HTTPException(400)
    path = await core.discover.photo(mbid)
    if not path:
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=86400"})
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})
