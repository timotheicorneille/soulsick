import asyncio
import base64
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from ..core import Core
from ..journal import event_to_dict
from ..messages import M
from ..models import AlbumStatus, Artist, Event, LibraryFile, QualityProfile, ReleaseGroup, Transfer
from ..quality import ALL_FORMATS
from ..settings_store import AppSettings
from ..slsk.engine import PICTURE_MAX_BYTES, picture_mime
from .deps import get_core, require_user
from .errors import as_http, http_error
from .serializers import library_file_dict, profile_dict

router = APIRouter(prefix="/api", tags=["system"], dependencies=[Depends(require_user)])
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@router.get("/status")
async def status(core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        counts = dict(
            (await s.execute(select(ReleaseGroup.status, func.count()).group_by(ReleaseGroup.status))).all()
        )
        artists = (await s.execute(select(func.count()).select_from(Artist))).scalar_one()
        lib = (await s.execute(select(func.count(), func.coalesce(func.sum(LibraryFile.size), 0)).select_from(LibraryFile))).one()
        active = dict(
            (
                await s.execute(
                    select(Transfer.direction, func.count())
                    .where(Transfer.state.in_(["queued", "initializing", "transferring"]))
                    .group_by(Transfer.direction)
                )
            ).all()
        )
    return {
        "engine": core.engine.status(),
        "hunter": core.hunter.state(),
        "library": {"files": lib[0], "size": lib[1], "scanning": core.library.scanning},
        "artists": artists,
        "albums": {k: v for k, v in counts.items()},
        "attention": counts.get(AlbumStatus.PENDING_APPROVAL, 0) + counts.get(AlbumStatus.NEEDS_REVIEW, 0),
        "active_transfers": {"download": active.get("download", 0), "upload": active.get("upload", 0)},
        "stuck_transfers": core.transfers.stuck_count(),
        "chat_unread": await core.chat.unread_private(),
        "storage": core.storage.quick_level(),
        "library_guard": core.guard.state(),
        "paths": {
            "music": str(core.env.music_dir.resolve()),
            "downloads": str(core.env.downloads_dir.resolve()),
            "config": str(core.env.config_dir.resolve()),
        },
    }


# --- Settings -----------------------------------------------------------------


def _redact(s: AppSettings) -> dict:
    d = s.model_dump()
    d["soulseek"]["password"] = "••••••••" if s.soulseek.password else ""
    d["navidrome"]["password"] = "••••••••" if s.navidrome.password else ""
    return d


@router.get("/settings")
async def get_settings(core: Core = Depends(get_core)) -> dict:
    return _redact(await core.settings.get())


@router.put("/settings")
async def put_settings(body: AppSettings, core: Core = Depends(get_core)) -> dict:
    current = await core.settings.get()
    if body.soulseek.password == "••••••••":
        body.soulseek.password = current.soulseek.password
    if body.navidrome.password == "••••••••":
        body.navidrome.password = current.navidrome.password
    await core.settings.save(body)
    changed = [k for k in ("soulseek", "hunter", "library", "navidrome") if getattr(body, k) != getattr(current, k)]
    await core.journal.emit(
        "settings.updated",
        M("settings.updated", sections=", ".join(changed)) if changed else M("settings.unchanged"),
        data={"sections": changed},
    )
    if body.soulseek != current.soulseek:
        only_library = body.soulseek.model_copy(update={"share_library": current.soulseek.share_library}) == current.soulseek
        if only_library:
            # Toggling library sharing needs no reconnection
            asyncio.create_task(core.shares.rescan())
        else:
            await core.restart_engine()
    return _redact(body)


# --- Soulseek profile picture -----------------------------------------------------


@router.get("/profile/picture")
async def get_picture(core: Core = Depends(get_core)) -> Response:
    path = core.env.profile_picture_path
    data = path.read_bytes() if path.is_file() else None
    if not data:
        raise HTTPException(404)
    return Response(data, media_type=picture_mime(data) or "application/octet-stream", headers={"Cache-Control": "no-cache"})


class PictureIn(BaseModel):
    # base64, optionally as a data: URL
    data: str


@router.put("/profile/picture")
async def put_picture(body: PictureIn, core: Core = Depends(get_core)) -> dict:
    raw = body.data.split(",", 1)[1] if body.data.startswith("data:") else body.data
    if len(raw) > PICTURE_MAX_BYTES * 4 // 3 + 4:
        raise http_error(413, "error.picture_too_big")
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError:
        raise http_error(400, "error.picture_invalid") from None
    if len(data) > PICTURE_MAX_BYTES:
        raise http_error(413, "error.picture_too_big")
    if not picture_mime(data):
        raise http_error(400, "error.picture_invalid")
    path = core.env.profile_picture_path
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    core.engine.set_picture(data)
    await core.journal.emit("profile.picture_updated", M("profile.picture_updated"))
    return {"ok": True, "size": len(data)}


@router.delete("/profile/picture")
async def delete_picture(core: Core = Depends(get_core)) -> dict:
    core.env.profile_picture_path.unlink(missing_ok=True)
    core.engine.set_picture(None)
    await core.journal.emit("profile.picture_removed", M("profile.picture_removed"))
    return {"ok": True}


@router.post("/settings/navidrome/test")
async def test_navidrome(core: Core = Depends(get_core)) -> dict:
    return await core.navidrome.trigger_scan(force=True)


@router.post("/hunter/run")
async def run_hunter(core: Core = Depends(get_core)) -> dict:
    """Hunts wanted albums right now (without waiting for the interval)."""
    if not core.engine.logged_in:
        raise http_error(503, "error.engine_offline")
    limit = (await core.settings.get()).hunter.max_searches_per_run
    async with core.db.session() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(ReleaseGroup)
                .join(Artist, Artist.id == ReleaseGroup.artist_id)
                .where(
                    ReleaseGroup.status == AlbumStatus.WANTED,
                    ReleaseGroup.monitored.is_(True),
                    Artist.monitored.is_(True),
                )
            )
        ).scalar_one()
    asyncio.create_task(core.hunter.run_wanted(limit, force=True))
    return {"queued": min(n, limit)}


@router.post("/engine/reconnect")
async def reconnect(core: Core = Depends(get_core)) -> dict:
    await core.restart_engine()
    return {"ok": True}


# --- Profils qualité -----------------------------------------------------------


class ProfileIn(BaseModel):
    name: str = Field(min_length=1)
    formats: list[str]
    upgrade_until: str | None = None
    max_missing_tracks: int = Field(default=0, ge=0)


@router.get("/formats")
async def formats() -> list[dict]:
    return [{"key": k, "label": label} for k, label in ALL_FORMATS]


@router.get("/profiles")
async def list_profiles(core: Core = Depends(get_core)) -> list[dict]:
    async with core.db.session() as s:
        return [profile_dict(p) for p in (await s.execute(select(QualityProfile).order_by(QualityProfile.id))).scalars()]


@router.post("/profiles")
async def create_profile(body: ProfileIn, core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        p = QualityProfile(**body.model_dump())
        s.add(p)
        await s.commit()
        await s.refresh(p)
    await core.journal.emit("profile.created", M("profile.created", name=p.name, formats=" > ".join(p.formats)))
    return profile_dict(p)


@router.put("/profiles/{pid}")
async def update_profile(pid: int, body: ProfileIn, core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        p = await s.get(QualityProfile, pid)
        if not p:
            raise http_error(404, "error.profile_not_found")
        for k, v in body.model_dump().items():
            setattr(p, k, v)
        await s.commit()
    await core.journal.emit("profile.updated", M("profile.updated", name=p.name, formats=" > ".join(p.formats)))
    return profile_dict(p)


@router.delete("/profiles/{pid}")
async def delete_profile(pid: int, core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        used = (await s.execute(select(func.count()).where(Artist.quality_profile_id == pid))).scalar_one()
        if used:
            raise http_error(409, "error.profile_in_use", n=used)
        p = await s.get(QualityProfile, pid)
        if p:
            await s.delete(p)
            await s.commit()
    return {"ok": True}


# --- Journal -------------------------------------------------------------------


@router.get("/events")
async def events(
    core: Core = Depends(get_core),
    before: int | None = None,
    limit: int = 100,
    kind: str | None = None,
    level: str | None = None,
    artist_id: int | None = None,
    release_group_id: int | None = None,
    q: str | None = None,
) -> list[dict]:
    stmt = select(Event).order_by(desc(Event.id)).limit(min(limit, 500))
    if before:
        stmt = stmt.where(Event.id < before)
    if kind:
        stmt = stmt.where(Event.kind.startswith(kind))
    if level:
        stmt = stmt.where(Event.level == level)
    if artist_id:
        stmt = stmt.where(Event.artist_id == artist_id)
    if release_group_id:
        stmt = stmt.where(Event.release_group_id == release_group_id)
    if q:
        stmt = stmt.where(Event.message.ilike(f"%{q}%"))
    async with core.db.session() as s:
        return [event_to_dict(e) for e in (await s.execute(stmt)).scalars()]


# --- Custom themes -----------------------------------------------------------------

_THEME_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_FORBIDDEN_CSS = re.compile(r"[;{}<>\\]")


def _check_theme_value(v, path: str) -> None:
    if isinstance(v, dict):
        if len(v) > 40:
            raise http_error(400, "error.theme_too_many_keys", path=path)
        for k, x in v.items():
            if not isinstance(k, str) or len(k) > 40:
                raise http_error(400, "error.theme_bad_key", path=path)
            _check_theme_value(x, f"{path}.{k}")
    elif isinstance(v, str):
        # Themes become CSS: nothing that could break out of it
        if len(v) > 200 or _FORBIDDEN_CSS.search(v):
            raise http_error(400, "error.theme_bad_value", path=path)
    elif not isinstance(v, (int, float, bool)):
        raise http_error(400, "error.theme_bad_type", path=path)


@router.get("/themes")
async def get_themes(core: Core = Depends(get_core)) -> list[dict]:
    return (await core.settings.get_raw("themes") or {}).get("items", [])


@router.put("/themes")
async def put_themes(themes: list[dict], core: Core = Depends(get_core)) -> list[dict]:
    if len(themes) > 50:
        raise http_error(400, "error.theme_limit")
    ids = set()
    for t in themes:
        tid = t.get("id")
        if not isinstance(tid, str) or not _THEME_ID.match(tid) or tid in ids:
            raise http_error(400, "error.theme_bad_id", id=repr(tid))
        if not isinstance(t.get("name"), str) or not t["name"].strip():
            raise http_error(400, "error.theme_no_name", id=tid)
        _check_theme_value(t, tid)
        ids.add(tid)
    before = {t["id"] for t in await get_themes(core)}
    await core.settings.set_raw("themes", {"items": themes})
    added, removed = ids - before, before - ids
    await core.journal.emit(
        "settings.themes",
        M("settings.themes", changes=", ".join([f"+{i}" for i in sorted(added)] + [f"−{i}" for i in sorted(removed)]) or "~"),
        data={"count": len(themes)},
    )
    return themes


# --- Console (logs techniques en direct) -----------------------------------------


@router.get("/logs")
async def logs(after: int = 0, limit: int = 1000) -> dict:
    from ..logbuffer import BUFFER

    return {"logs": BUFFER.since(after, min(limit, 3000)), "capacity": BUFFER.records.maxlen}


@router.get("/activity")
async def activity(core: Core = Depends(get_core)) -> dict:
    """Ce sur quoi Soulsick travaille en ce moment."""
    now = time.time()
    searches = [
        {
            "ticket": ls.ticket,
            "query": ls.query,
            "age": round(now - ls.started, 1),
            "responses": len(ls.results),
            "files": sum(len(r.files) for r in ls.results),
        }
        for ls in sorted(core.engine.searches.values(), key=lambda x: -x.started)
        if now - ls.started < 60
    ]
    return {
        "hunter": core.hunter.state(),
        "searches": searches,
        "library_scanning": core.library.scanning,
        "retrying": len(core.hunter._retrying),
        "engine": core.engine.status(),
    }


@router.get("/storage")
async def storage(refresh: bool = False, core: Core = Depends(get_core)) -> dict:
    """Disks holding the library, downloads, configuration and shares: free space and what Soulsick uses."""
    return await core.storage.snapshot(force=refresh)


# --- Library -------------------------------------------------------------------


@router.post("/library/scan")
async def scan_library(core: Core = Depends(get_core)) -> dict:
    asyncio.create_task(core.library.scan())
    return {"ok": True}


@router.get("/library/lyrics")
async def lyrics_job(core: Core = Depends(get_core)) -> dict:
    return core.lyrics.job


@router.post("/library/lyrics")
async def fetch_library_lyrics(core: Core = Depends(get_core)) -> dict:
    """Lyrics from LRCLIB for every track without, in the background."""
    return {"started": core.lyrics.start_library(), **core.lyrics.job}


@router.post("/library/guard/check")
async def check_library_guard(core: Core = Depends(get_core)) -> dict:
    """Look at the library folder again now (after fixing a mount, plugging a disk…)."""
    await core.guard.check()
    return core.guard.state()


@router.post("/library/guard/confirm-empty")
async def confirm_library_empty(core: Core = Depends(get_core)) -> dict:
    """The library folder really is empty now: forget the files that were known."""
    await core.guard.check()
    if not core.guard.problem or core.guard.problem.key != "library.guard.emptied":
        raise http_error(400, "error.library_not_empty_case")
    stats = await core.library.scan(confirm_empty=True)
    return {**core.guard.state(), "removed": stats.get("removed", 0)}


class IdentifyIn(BaseModel):
    dir: str
    artist: str | None = None
    album: str | None = None


class ApplyExistingIn(BaseModel):
    dir: str
    release_group_mbid: str
    release_mbid: str
    follow: bool = False


@router.get("/library/existing")
async def existing_groups(core: Core = Depends(get_core)) -> dict:
    return {"groups": await core.existing.groups(), "job": core.existing.job}


@router.post("/library/existing/identify")
async def existing_identify(body: IdentifyIn, core: Core = Depends(get_core)) -> dict:
    try:
        return await core.existing.identify(body.dir, body.artist, body.album)
    except ValueError as e:
        raise as_http(e) from e


@router.post("/library/existing/identify-all")
async def existing_identify_all(core: Core = Depends(get_core)) -> dict:
    asyncio.create_task(core.existing.identify_all())
    return {"ok": True}


@router.post("/library/existing/apply")
async def existing_apply(body: ApplyExistingIn, core: Core = Depends(get_core)) -> dict:
    try:
        return await core.existing.apply(body.dir, body.release_group_mbid, body.release_mbid, body.follow)
    except ValueError as e:
        raise as_http(e) from e


@router.get("/library/unmatched")
async def unmatched(core: Core = Depends(get_core)) -> list[dict]:
    return [library_file_dict(f) for f in await core.library.unmatched_files()]


# --- Covers --------------------------------------------------------------------


@router.get("/covers/{kind}/{mbid}")
async def cover(kind: str, mbid: str, size: int = 500, core: Core = Depends(get_core)):
    if kind not in {"release-group", "release"} or size not in {250, 500, 1200} or not _UUID.fullmatch(mbid):
        raise HTTPException(400)
    path = await core.mb.cover(kind, mbid, size)
    if not path:
        # No cover on Cover Art Archive (common for small releases): the browser remembers it for a day
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=86400"})
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})
