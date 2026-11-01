import asyncio
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from ..core import Core
from ..journal import event_to_dict
from ..messages import M, UserError, set_reason
from ..models import (
    AlbumStatus,
    Artist,
    ArtistMode,
    ArtistReleaseGroup,
    Candidate,
    CandidateStatus,
    Event,
    LibraryFile,
    QualityProfile,
    Release,
    ReleaseGroup,
    SearchRun,
    Transfer,
    of_artist,
)
from ..musicbrainz import MusicBrainzError
from ..slsk.engine import EngineUnavailable
from ..services.transfers import transfer_to_dict
from .deps import get_core, require_user
from .errors import as_http, http_error
from .serializers import artist_dict, candidate_dict, iso, library_file_dict, release_dict, rg_dict

router = APIRouter(prefix="/api", tags=["catalog"], dependencies=[Depends(require_user)])


# --- Artists --------------------------------------------------------------------


@router.get("/musicbrainz/artists")
async def mb_search(q: str, core: Core = Depends(get_core)) -> list[dict]:
    try:
        results = await core.mb.search_artists(q)
    except MusicBrainzError as e:
        raise HTTPException(502, str(e)) from e
    async with core.db.session() as s:
        known = set((await s.execute(select(Artist.mbid))).scalars())
    return [r | {"in_library": r["mbid"] in known} for r in results]


class ArtistIn(BaseModel):
    mbid: str
    mode: ArtistMode = ArtistMode.VALIDATION
    quality_profile_id: int | None = None
    primary_types: list[str] = ["album", "ep"]
    secondary_types: list[str] = []
    monitor: Literal["none", "future", "all"] = "none"


class ArtistsIn(BaseModel):
    """Several artists followed at once, each with the same settings."""

    mbids: list[str] = Field(min_length=1, max_length=100)
    mode: ArtistMode = ArtistMode.VALIDATION
    quality_profile_id: int | None = None
    primary_types: list[str] = ["album", "ep"]
    secondary_types: list[str] = []
    monitor: Literal["none", "future", "all"] = "none"


class ArtistPatch(BaseModel):
    monitored: bool | None = None
    mode: ArtistMode | None = None
    quality_profile_id: int | None = None
    primary_types: list[str] | None = None
    secondary_types: list[str] | None = None


async def _artist_stats(core: Core, ids: list[int]) -> dict[int, dict]:
    async with core.db.session() as s:
        rows = (
            await s.execute(
                select(ArtistReleaseGroup.artist_id, ReleaseGroup.status, func.count())
                .join(ReleaseGroup, ReleaseGroup.id == ArtistReleaseGroup.release_group_id)
                .where(ArtistReleaseGroup.artist_id.in_(ids))
                .group_by(ArtistReleaseGroup.artist_id, ReleaseGroup.status)
            )
        ).all()
        covers = (
            await s.execute(
                select(
                    ArtistReleaseGroup.artist_id,
                    ReleaseGroup.mbid,
                    ReleaseGroup.first_release_date,
                    ReleaseGroup.status,
                    ReleaseGroup.monitored,
                    ReleaseGroup.secondary_types,
                )
                .join(ReleaseGroup, ReleaseGroup.id == ArtistReleaseGroup.release_group_id)
                .where(ArtistReleaseGroup.artist_id.in_(ids), ReleaseGroup.primary_type == "album")
            )
        ).all()
    out: dict[int, dict] = {i: {"total": 0, "monitored": 0, "imported": 0, "wanted": 0, "attention": 0} for i in ids}
    for aid, status, n in rows:
        st = out[aid]
        st["total"] += n
        if status == AlbumStatus.IMPORTED:
            st["imported"] += n
        elif status in (AlbumStatus.WANTED, AlbumStatus.SEARCHING, AlbumStatus.DOWNLOADING, AlbumStatus.IMPORTING):
            st["wanted"] += n
        elif status in (AlbumStatus.PENDING_APPROVAL, AlbumStatus.NEEDS_REVIEW):
            st["attention"] += n
        if status != AlbumStatus.IGNORED:
            st["monitored"] += n
    # Pochette représentative : le dernier album studio présent, sinon suivi, sinon le dernier album
    best: dict[int, tuple] = {}
    for aid, mbid, date, status, monitored, secondary in covers:
        key = (status == AlbumStatus.IMPORTED, bool(monitored), not secondary, date or "")
        if aid not in best or key > best[aid][0]:
            best[aid] = (key, mbid)
    for aid, (_, mbid) in best.items():
        out[aid]["cover_mbid"] = mbid
    return out


@router.get("/artists")
async def list_artists(core: Core = Depends(get_core)) -> list[dict]:
    async with core.db.session() as s:
        artists = list((await s.execute(select(Artist).order_by(Artist.sort_name))).scalars())
    stats = await _artist_stats(core, [a.id for a in artists])
    return [artist_dict(a, stats.get(a.id)) for a in artists]


@router.post("/artists")
async def add_artist(body: ArtistIn, core: Core = Depends(get_core)) -> dict:
    try:
        a = await core.catalog.add_artist(
            body.mbid,
            mode=body.mode,
            quality_profile_id=body.quality_profile_id,
            primary_types=body.primary_types,
            secondary_types=body.secondary_types,
            monitor=body.monitor,
        )
    except MusicBrainzError as e:
        raise HTTPException(502, str(e)) from e
    stats = await _artist_stats(core, [a.id])
    return artist_dict(a, stats.get(a.id))


@router.post("/artists/bulk")
async def add_artists(body: ArtistsIn, core: Core = Depends(get_core)) -> dict:
    """Each artist takes MusicBrainz a few seconds (its whole discography): they are added
    one after the other in the background; each one shows up as it is added."""
    mbids = list(dict.fromkeys(body.mbids))
    asyncio.create_task(core.catalog.add_artists(
        mbids,
        mode=body.mode,
        quality_profile_id=body.quality_profile_id,
        primary_types=body.primary_types,
        secondary_types=body.secondary_types,
        monitor=body.monitor,
    ))
    return {"queued": len(mbids)}


@router.get("/artists/{aid}")
async def get_artist(aid: int, core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        a = await s.get(Artist, aid)
        if not a:
            raise HTTPException(404)
        rgs = list(
            (
                await s.execute(
                    select(ReleaseGroup)
                    .where(of_artist(aid))
                    .order_by(desc(ReleaseGroup.first_release_date))
                )
            ).scalars()
        )
    stats = await _artist_stats(core, [aid])
    return artist_dict(a, stats.get(aid)) | {"release_groups": [rg_dict(rg) for rg in rgs]}


@router.patch("/artists/{aid}")
async def patch_artist(aid: int, body: ArtistPatch, core: Core = Depends(get_core)) -> dict:
    changes = body.model_dump(exclude_unset=True)
    async with core.db.session() as s:
        a = await s.get(Artist, aid)
        if not a:
            raise HTTPException(404)
        for k, v in changes.items():
            setattr(a, k, v)
        await s.commit()
        await s.refresh(a)
    profile_name = None
    if changes.get("quality_profile_id"):
        async with core.db.session() as s:
            p = await s.get(QualityProfile, changes["quality_profile_id"])
            profile_name = p.name if p else f"#{changes['quality_profile_id']}"
    labels = {
        "monitored": lambda v: M("artist.change.monitored_on" if v else "artist.change.monitored_off"),
        "mode": lambda v: M("artist.change.mode", value=M("word.automatic" if v == "auto" else "word.ask_me")),
        "quality_profile_id": lambda v: M("artist.change.profile", value=profile_name),
        "primary_types": lambda v: M("artist.change.primary_types", value=", ".join(v)),
        "secondary_types": lambda v: M("artist.change.secondary_types", value=", ".join(v) or M("word.none")),
    }
    parts: list = []
    for k, v in changes.items():
        if k in labels:
            parts += [", ", labels[k](v)] if parts else [labels[k](v)]
    await core.journal.emit("artist.updated", M("artist.updated", name=a.name, changes=parts), artist_id=aid, data=changes)
    if "primary_types" in changes or "secondary_types" in changes:
        await core.catalog.apply_type_filter(aid)
    stats = await _artist_stats(core, [aid])
    return artist_dict(a, stats.get(aid))


class MonitorIn(BaseModel):
    scope: Literal["all", "none", "future"]
    section: str | None = None


@router.post("/artists/{aid}/monitor")
async def bulk_monitor(aid: int, body: MonitorIn, core: Core = Depends(get_core)) -> dict:
    return {"changed": await core.catalog.bulk_monitor(aid, body.scope, body.section)}


@router.delete("/artists/{aid}")
async def delete_artist(aid: int, core: Core = Depends(get_core)) -> dict:
    await core.catalog.delete_artist(aid)
    return {"ok": True}


@router.post("/artists/{aid}/sync")
async def sync_artist(aid: int, core: Core = Depends(get_core)) -> dict:
    asyncio.create_task(core.catalog.sync_artist(aid))
    return {"ok": True}


@router.post("/artists/{aid}/hunt")
async def hunt_artist(aid: int, core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        ids = list(
            (
                await s.execute(
                    select(ReleaseGroup.id).where(
                        of_artist(aid), ReleaseGroup.status == AlbumStatus.WANTED
                    )
                )
            ).scalars()
        )

    async def run():
        for i in ids:
            await core.hunter.hunt(i, trigger="manual")

    asyncio.create_task(run())
    return {"queued": len(ids)}


# --- Library ----------------------------------------------------------------------


@router.get("/library/albums")
async def library_albums(core: Core = Depends(get_core)) -> list[dict]:
    """Albums shelved in /music, newest first, with where they came from."""
    async with core.db.session() as s:
        rows = (
            await s.execute(
                select(ReleaseGroup, Artist)
                .join(Artist, Artist.id == ReleaseGroup.artist_id)
                .where(ReleaseGroup.imported_path.is_not(None))
                .order_by(desc(ReleaseGroup.updated_at))
            )
        ).all()
        ids = [rg.id for rg, _ in rows]
        sources: dict[int, list[str]] = {}
        if ids:
            for rid, user in (
                await s.execute(
                    select(Candidate.release_group_id, Candidate.username)
                    .where(Candidate.release_group_id.in_(ids), Candidate.status == CandidateStatus.IMPORTED)
                )
            ).all():
                sources.setdefault(rid, []).append(user)
            counts = dict(
                (
                    await s.execute(
                        select(LibraryFile.release_group_mbid, func.count())
                        .where(LibraryFile.release_group_mbid.in_([rg.mbid for rg, _ in rows]))
                        .group_by(LibraryFile.release_group_mbid)
                    )
                ).all()
            )
        else:
            counts = {}
    return [
        rg_dict(rg, a) | {"sources": sources.get(rg.id, []), "file_count": counts.get(rg.mbid, 0)}
        for rg, a in rows
    ]


@router.get("/library/manual")
async def library_manual(core: Core = Depends(get_core)) -> list[dict]:
    """Finished manual downloads (outside the library), grouped by download folder."""
    async with core.db.session() as s:
        ts = list(
            (
                await s.execute(
                    select(Transfer)
                    .where(Transfer.direction == "download", Transfer.candidate_id.is_(None), Transfer.state == "complete")
                    .order_by(desc(Transfer.completed_at))
                    .limit(2000)
                )
            ).scalars()
        )
    folders: dict[str, dict] = {}
    for t in ts:
        d = os.path.dirname(t.local_path or "")
        f = folders.setdefault(d, {
            "dir": d, "username": t.username, "files": 0, "size": 0, "completed_at": iso(t.completed_at),
            "present": 0, "shelved": 0, "albums": [],
        })
        f["files"] += 1
        f["size"] += t.size or 0
        if t.shelved_path:
            f["shelved"] += 1
            if t.release_group_id and t.release_group_id not in f["albums"]:
                f["albums"].append(t.release_group_id)
        elif t.local_path and os.path.isfile(t.local_path):
            f["present"] += 1
    return list(folders.values())


@router.get("/library/manual/folder")
async def library_manual_folder(dir: str, core: Core = Depends(get_core)) -> dict:
    """A manual download folder ready to be identified and shelved (same flow as existing music)."""
    try:
        return await core.existing.describe(dir)
    except ValueError as e:
        raise as_http(e) from e


# --- Albums ---------------------------------------------------------------------


@router.get("/albums")
async def list_albums(
    status: str | None = None, core: Core = Depends(get_core), limit: int = 500
) -> list[dict]:
    stmt = select(ReleaseGroup, Artist).join(Artist, Artist.id == ReleaseGroup.artist_id)
    if status:
        stmt = stmt.where(ReleaseGroup.status.in_(status.split(",")))
    stmt = stmt.order_by(desc(ReleaseGroup.updated_at)).limit(limit)
    async with core.db.session() as s:
        rows = (await s.execute(stmt)).all()
    return [rg_dict(rg, a) for rg, a in rows]


@router.get("/albums/{rid}")
async def get_album(rid: int, core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        rg = await s.get(ReleaseGroup, rid)
        if not rg:
            raise HTTPException(404)
        artist = await s.get(Artist, rg.artist_id)
    try:
        releases = await core.catalog.releases_with_tracks(rid)
    except MusicBrainzError:
        releases = []
    async with core.db.session() as s:
        rg = await s.get(ReleaseGroup, rid)
        assert rg
        cands = list(
            (
                await s.execute(
                    select(Candidate)
                    .where(Candidate.release_group_id == rid)
                    .order_by(Candidate.status == CandidateStatus.REJECTED, desc(Candidate.score))
                )
            ).scalars()
        )
        runs = list(
            (
                await s.execute(
                    select(SearchRun).where(SearchRun.release_group_id == rid).order_by(desc(SearchRun.id)).limit(20)
                )
            ).scalars()
        )
        rel_mbids = [r.mbid for r in releases]
        files = list(
            (
                await s.execute(
                    select(LibraryFile)
                    .where(
                        (LibraryFile.release_group_mbid == rg.mbid)
                        | (LibraryFile.release_mbid.in_(rel_mbids) if rel_mbids else False)
                    )
                    .order_by(LibraryFile.path)
                )
            ).scalars()
        )
        transfers = list(
            (
                await s.execute(
                    select(Transfer).where(Transfer.release_group_id == rid).order_by(desc(Transfer.id)).limit(200)
                )
            ).scalars()
        )
        events = list(
            (
                await s.execute(
                    select(Event).where(Event.release_group_id == rid).order_by(desc(Event.id)).limit(200)
                )
            ).scalars()
        )
    return rg_dict(rg, artist) | {
        "releases": [release_dict(r) for r in releases],
        "candidates": [candidate_dict(c) for c in cands],
        "search_runs": [
            {
                "id": r.id,
                "query": r.query,
                "kind": r.kind,
                "started_at": iso(r.started_at),
                "result_count": r.result_count,
                "peer_count": r.peer_count,
            }
            for r in runs
        ],
        "files": [library_file_dict(f) for f in files],
        "transfers": [transfer_to_dict(t) | (core.transfers.live(t.id) or {}) for t in transfers],
        "events": [event_to_dict(e) for e in events],
    }


class AlbumPatch(BaseModel):
    monitored: bool | None = None
    selected_release_id: int | None = None


@router.patch("/albums/{rid}")
async def patch_album(rid: int, body: AlbumPatch, core: Core = Depends(get_core)) -> dict:
    if body.monitored is not None:
        await core.catalog.set_album_monitored(rid, body.monitored)
    if body.selected_release_id is not None:
        async with core.db.session() as s:
            rg = await s.get(ReleaseGroup, rid)
            rel = await s.get(Release, body.selected_release_id)
            if not rg or not rel or rel.release_group_id != rid:
                raise http_error(400, "error.invalid_edition")
            rg.selected_release_id = rel.id
            await s.commit()
        await core.journal.emit(
            "album.release_selected",
            M(
                "album.release_selected", title=rg.title, release=rel.title, date=rel.date or "?",
                country=rel.country or "?", formats=rel.formats, n=rel.track_count,
            ),
            artist_id=rg.artist_id, release_group_id=rid,
        )
        # Tracks present against the new edition; a longer one leaves tracks to find
        await core.library.refresh_artist(rg.artist_id)
        async with core.db.session() as s:
            rg = await s.get(ReleaseGroup, rid)
            missing = rg.monitored and rg.status == AlbumStatus.WANTED
        if missing:
            asyncio.create_task(core.hunter.hunt(rid, trigger="manual"))
    async with core.db.session() as s:
        rg = await s.get(ReleaseGroup, rid)
        return rg_dict(rg)


@router.post("/albums/{rid}/hunt")
async def hunt_album(rid: int, core: Core = Depends(get_core)) -> dict:
    asyncio.create_task(core.hunter.hunt(rid, trigger="manual"))
    return {"ok": True}


@router.post("/albums/{rid}/lyrics")
async def fetch_album_lyrics(rid: int, core: Core = Depends(get_core)) -> dict:
    """Lyrics from LRCLIB for the album's tracks without, now (even those tried recently)."""
    async with core.db.session() as s:
        rg = await s.get(ReleaseGroup, rid)
        if not rg:
            raise HTTPException(404)
    return await core.lyrics.fetch_album(rg.mbid, by_user=True)


@router.post("/albums/{rid}/redownload")
async def redownload_album(rid: int, core: Core = Depends(get_core)) -> dict:
    """Download this album again; the shelved files are replaced once it's imported."""
    try:
        n = await core.hunter.redownload(rid)
    except ValueError as e:
        raise as_http(e, 404) from e
    return {"ok": True, "dropped": n}


@router.post("/candidates/{cid}/approve")
async def approve(cid: int, core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        c = await s.get(Candidate, cid)
        if not c:
            raise HTTPException(404)
        if c.status not in (CandidateStatus.PROPOSED, CandidateStatus.REJECTED, CandidateStatus.FAILED):
            raise http_error(409, "error.candidate_busy")
        started = (await s.execute(select(Transfer.id).where(Transfer.candidate_id == cid).limit(1))).first()
    if started:
        # Already started: resume without downloading again what arrived
        missing = await core.hunter.resume_candidate(cid)
        return {"ok": True, "resumed": True, "missing": missing}
    await core.hunter.start_download(cid, by="user")
    return {"ok": True, "resumed": False}


@router.post("/albums/{rid}/import")
async def import_now(rid: int, core: Core = Depends(get_core)) -> dict:
    """Import what was received for this album now (all sources together, tracks already shelved count)."""
    await core.guard.check()
    try:
        core.guard.require()
    except UserError as e:
        raise as_http(e, 409) from e
    async with core.db.session() as s:
        rows = (await s.execute(
            select(Transfer.candidate_id, func.count())
            .where(Transfer.release_group_id == rid, Transfer.candidate_id.is_not(None), Transfer.state == "complete")
            .group_by(Transfer.candidate_id)
            .order_by(func.count().desc())
        )).all()
    if not rows:
        raise http_error(400, "error.nothing_received")
    asyncio.create_task(core.importer.import_candidate(rows[0][0]))
    return {"ok": True}


@router.post("/candidates/{cid}/abandon")
async def abandon_candidate(cid: int, core: Core = Depends(get_core)) -> dict:
    """Drop a source that doesn't deliver: its unfinished files are cancelled."""
    return {"ok": True, "cancelled": await core.hunter.abandon(cid)}


@router.get("/candidates/{cid}/alternatives")
async def candidate_alternatives(cid: int, core: Core = Depends(get_core)) -> dict:
    """Other peers that have the tracks this source has not delivered yet."""
    try:
        return await core.coverage.alternatives(cid)
    except MusicBrainzError as e:
        raise HTTPException(502, str(e)) from e


class SwitchIn(BaseModel):
    to: int | None = None
    search: bool = False


@router.post("/candidates/{cid}/switch")
async def switch_peer(cid: int, body: SwitchIn, core: Core = Depends(get_core)) -> dict:
    """Replace a source that is stuck: what arrived is kept, the rest comes from another peer."""
    if body.to is None and not body.search:
        raise HTTPException(422, "to or search required")
    try:
        n = await core.hunter.switch_peer(cid, to=body.to, search=body.search)
    except (ValueError, EngineUnavailable) as e:
        raise as_http(e) from e
    return {"ok": True, "files": n}


@router.post("/albums/{rid}/cancel")
async def cancel_album(rid: int, core: Core = Depends(get_core)) -> dict:
    """Stop downloading this album: every active source is dropped (received files are kept)."""
    async with core.db.session() as s:
        ids = list((await s.execute(select(Candidate.id).where(
            Candidate.release_group_id == rid, Candidate.status == CandidateStatus.CHOSEN,
        ))).scalars())
    n = 0
    for cid in ids:
        n += await core.hunter.abandon(cid)
    return {"ok": True, "cancelled": n}


@router.post("/albums/{rid}/dismiss")
async def dismiss(rid: int, core: Core = Depends(get_core)) -> dict:
    """Rejects every proposal: the album goes back to wanted."""
    async with core.db.session() as s:
        rg = await s.get(ReleaseGroup, rid)
        if not rg:
            raise HTTPException(404)
        rg.status = AlbumStatus.WANTED if rg.monitored else AlbumStatus.IGNORED
        set_reason(rg, "status_reason", M("reason.dismissed"))
        await s.commit()
    await core.journal.emit(
        "hunt.dismissed", M("hunt.dismissed", title=rg.title),
        artist_id=rg.artist_id, release_group_id=rid,
    )
    return {"ok": True}


@router.get("/albums/{rid}/coverage")
async def coverage(rid: int, core: Core = Depends(get_core)) -> dict:
    try:
        return await core.coverage.album(rid)
    except MusicBrainzError as e:
        raise HTTPException(502, str(e)) from e


class CompleteIn(BaseModel):
    candidate_id: int
    track_ids: list[int]


@router.post("/albums/{rid}/complete")
async def complete(rid: int, body: CompleteIn, core: Core = Depends(get_core)) -> dict:
    """Mixing: take only the chosen missing tracks from another peer."""
    async with core.db.session() as s:
        c = await s.get(Candidate, body.candidate_id)
        if not c or c.release_group_id != rid:
            raise HTTPException(404)
    try:
        n = await core.hunter.complete_from(body.candidate_id, body.track_ids)
    except ValueError as e:
        raise as_http(e) from e
    except EngineUnavailable as e:
        raise as_http(e) from e
    return {"ok": True, "files": n}


class TakeIn(BaseModel):
    candidate_id: int


@router.post("/albums/{rid}/tracks/{tid}/take")
async def take_track(rid: int, tid: int, body: TakeIn, core: Core = Depends(get_core)) -> dict:
    """« Prendre chez… »: this track from this peer, now, even mid-download."""
    async with core.db.session() as s:
        c = await s.get(Candidate, body.candidate_id)
        if not c or c.release_group_id != rid:
            raise HTTPException(404)
    try:
        await core.hunter.take_track(rid, tid, body.candidate_id)
    except (ValueError, EngineUnavailable) as e:
        raise as_http(e) from e
    return {"ok": True}


@router.get("/candidates/{cid}/plan")
async def get_plan(cid: int, core: Core = Depends(get_core)) -> dict:
    built = await core.importer.build_plan(cid)
    if built is None:
        raise HTTPException(404)
    plan, info = built
    return {"plan": plan, "info": info}


class PlanIn(BaseModel):
    release_id: int
    plan: list[dict]


@router.post("/candidates/{cid}/import")
async def apply_plan(cid: int, body: PlanIn, core: Core = Depends(get_core)) -> dict:
    try:
        await core.importer.apply_plan(cid, body.plan, body.release_id, by_user=True)
    except ValueError as e:
        raise as_http(e) from e
    return {"ok": True}
