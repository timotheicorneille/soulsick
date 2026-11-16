import asyncio
import os
import time
from typing import Literal

from aioslsk.exceptions import NetworkError, NoSuchUserError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, select

from ..core import Core
from ..messages import M
from ..models import Share, Transfer, TransferState
from ..scoring import group_results
from ..services.transfers import TERMINAL, transfer_to_dict
from ..slsk.engine import EngineUnavailable
from ..slsk.rules import forbidden_share_reason
from .deps import get_core, require_user
from .errors import as_http, http_error
from .serializers import share_dict

router = APIRouter(prefix="/api", tags=["network"], dependencies=[Depends(require_user)])



# --- Peers -------------------------------------------------------------------------


@router.get("/peers/{username}")
async def peer_history(username: str, core: Core = Depends(get_core)) -> dict:
    """What we exchanged with this peer: files received and sent, albums, messages."""
    return await core.peers.history(username)


@router.get("/peers/{username}/profile")
async def peer_profile(username: str, refresh: bool = False, core: Core = Depends(get_core)) -> dict:
    """What the network says about this peer (status, stats, description, picture, interests)."""
    try:
        return await core.peers.profile(username, refresh=refresh)
    except EngineUnavailable as e:
        raise as_http(e) from e


# --- Manual search ----------------------------------------------------------------


class SearchIn(BaseModel):
    query: str = Field(min_length=2)


@router.post("/search")
async def start_search(body: SearchIn, core: Core = Depends(get_core)) -> dict:
    try:
        live = await core.engine.start_search(body.query)
    except EngineUnavailable as e:
        raise as_http(e) from e
    await core.journal.emit("search.manual", M("search.manual", query=body.query), data={"ticket": live.ticket})
    return {"ticket": live.ticket, "query": live.query}


SEARCH_FOLDER_LIMIT = 300

# Folder order. Only the first 300 are sent, so the sort happens here and not in the browser
SEARCH_SORTS = {
    "queue": lambda f: (not f["has_free_slots"], f["queue_size"], -f["avg_speed"]),
    "speed": lambda f: (-f["avg_speed"], not f["has_free_slots"], f["queue_size"]),
    "size": lambda f: (-f["size"], not f["has_free_slots"], f["queue_size"]),
}


def _folder_matches(username: str, directory: str, terms: list[str]) -> bool:
    """Every word must appear in the peer or folder name, and none of the "-word" ones."""
    text = f"{username}\\{directory}".casefold()
    for term in terms:
        if term.startswith("-") and len(term) > 1:
            if term[1:] in text:
                return False
        elif term not in text:
            return False
    return True


@router.get("/search/{ticket}")
async def get_search(
    ticket: int, sort: Literal["queue", "speed", "size"] = "queue", filter: str = "",
    core: Core = Depends(get_core),
) -> dict:
    live = core.engine.searches.get(ticket)
    if not live:
        raise http_error(404, "error.search_expired")
    groups = group_results(live.results)
    terms = filter.casefold().split()
    folders = []
    for g in groups:
        if terms and not _folder_matches(g.username, g.directory, terms):
            continue
        files = [f.to_dict() for f in g.audio_files] + [f.to_dict() | {"extra": True} for f in g.extra_files]
        formats = sorted({f["format"] for f in files if f.get("format")})
        folders.append(
            {
                "username": g.username,
                "directory": g.directory,
                "files": files,
                "formats": formats,
                "audio_count": len(g.audio_files),
                "size": sum(f.size for f in g.audio_files),
                "has_free_slots": g.has_free_slots,
                "avg_speed": g.avg_speed,
                "queue_size": g.queue_size,
            }
        )
    folders.sort(key=SEARCH_SORTS[sort])
    return {
        "ticket": ticket,
        "query": live.query,
        "elapsed": round(time.time() - live.started, 1),
        "peer_count": len(live.results),
        "file_count": sum(len(r.files) for r in live.results),
        "folder_count": len(groups),
        "match_count": len(folders),
        "folders": folders[:SEARCH_FOLDER_LIMIT],
        "responses": list(reversed(live.responses[-300:])),
    }


class DownloadIn(BaseModel):
    username: str
    files: list[str] = Field(min_length=1)


@router.post("/downloads")
async def download(body: DownloadIn, core: Core = Depends(get_core)) -> dict:
    try:
        ids = await core.transfers.download_manual(body.username, body.files)
    except EngineUnavailable as e:
        raise as_http(e) from e
    except ValueError as e:
        raise as_http(e) from e
    return {"ids": ids}


# --- Transfers --------------------------------------------------------------------


@router.get("/transfers")
async def list_transfers(direction: str = "download", limit: int = 300, core: Core = Depends(get_core)) -> list[dict]:
    async with core.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(Transfer).where(Transfer.direction == direction).order_by(desc(Transfer.id)).limit(limit)
                )
            ).scalars()
        )
    return [transfer_to_dict(t) | (core.transfers.live(t.id) or {}) if t.state not in TERMINAL else transfer_to_dict(t) for t in rows]


async def _get_transfer(core: Core, tid: int) -> Transfer:
    async with core.db.session() as s:
        t = await s.get(Transfer, tid)
    if not t:
        raise http_error(404, "error.transfer_not_found")
    return t


@router.post("/transfers/{tid}/cancel")
async def cancel_transfer(tid: int, core: Core = Depends(get_core)) -> dict:
    t = await _get_transfer(core, tid)
    if not await core.transfers.cancel(tid):
        return {"ok": False}
    await core.journal.emit(
        "transfer.cancelled", M("transfer.cancelled", path=t.remote_path, peer=t.username), transfer_id=tid,
        release_group_id=t.release_group_id,
    )
    return {"ok": True}


@router.post("/transfers/{tid}/pause")
async def pause_transfer(tid: int, core: Core = Depends(get_core)) -> dict:
    t = await _get_transfer(core, tid)
    try:
        changed = await core.transfers.pause(tid)
    except EngineUnavailable as e:
        raise as_http(e) from e
    if changed:
        await core.journal.emit(
            "transfer.paused", M("transfer.paused", name=t.remote_path.replace(chr(92), "/").rsplit("/", 1)[-1], peer=t.username),
            transfer_id=tid, release_group_id=t.release_group_id, candidate_id=t.candidate_id,
        )
    return {"ok": changed}


@router.post("/transfers/{tid}/resume")
async def resume_transfer(tid: int, core: Core = Depends(get_core)) -> dict:
    t = await _get_transfer(core, tid)
    try:
        changed = await core.transfers.resume(tid)
    except EngineUnavailable as e:
        raise as_http(e) from e
    if changed:
        await core.journal.emit(
            "transfer.resumed", M("transfer.resumed", name=t.remote_path.replace(chr(92), "/").rsplit("/", 1)[-1], peer=t.username),
            transfer_id=tid, release_group_id=t.release_group_id, candidate_id=t.candidate_id,
        )
    return {"ok": changed}


@router.post("/transfers/{tid}/retry")
async def retry_transfer(tid: int, core: Core = Depends(get_core)) -> dict:
    t = await _get_transfer(core, tid)
    if t.direction != "download":
        raise HTTPException(400)
    try:
        await core.transfers.requeue(tid, reset_attempts=True)
    except EngineUnavailable as e:
        raise as_http(e) from e
    if t.candidate_id:
        # The candidate becomes active again: once the file arrives, the album gets imported
        await core.hunter.reactivate(t.candidate_id, M("reason.retry_by_you"))
    name = t.remote_path.replace("\\", "/").rsplit("/", 1)[-1]
    await core.journal.emit(
        "transfer.retried", M("transfer.retried", name=name, peer=t.username),
        transfer_id=tid, release_group_id=t.release_group_id, candidate_id=t.candidate_id,
    )
    return {"ok": True}


class BatchIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    action: Literal["cancel", "pause", "resume", "retry"]


@router.post("/transfers/batch")
async def batch(body: BatchIn, core: Core = Depends(get_core)) -> dict:
    """One action on many transfers. Cancelling every unfinished file of an album source drops
    that source cleanly (what arrived is kept, nothing restarts elsewhere on its own)."""
    async with core.db.session() as s:
        rows = list((await s.execute(select(Transfer).where(Transfer.id.in_(body.ids)))).scalars())
    done = 0
    if body.action == "cancel":
        open_rows = [t for t in rows if t.state not in TERMINAL]
        chosen = {t.id for t in open_rows}
        by_cand: dict[int, list[Transfer]] = {}
        for t in open_rows:
            if t.candidate_id:
                by_cand.setdefault(t.candidate_id, []).append(t)
        handled: set[int] = set()
        for cid in by_cand:
            async with core.db.session() as s:
                unfinished = set((await s.execute(select(Transfer.id).where(
                    Transfer.candidate_id == cid, Transfer.state.not_in(list(TERMINAL)),
                ))).scalars())
            if unfinished and unfinished <= chosen:
                done += await core.hunter.abandon(cid)
                handled |= unfinished
        for t in open_rows:
            if t.id not in handled and await core.transfers.cancel(t.id):
                done += 1
        if done:
            await core.journal.emit("transfer.cancelled_many", M("transfer.cancelled_many", n=done))
        return {"ok": True, "done": done}

    for t in rows:
        try:
            if body.action == "pause":
                done += await core.transfers.pause(t.id)
            elif body.action == "resume":
                done += await core.transfers.resume(t.id)
            elif t.direction == "download" and t.state in (TransferState.FAILED, TransferState.ABORTED):
                await core.transfers.requeue(t.id, reset_attempts=True)
                if t.candidate_id:
                    await core.hunter.reactivate(t.candidate_id, M("reason.retry_by_you"))
                done += 1
        except EngineUnavailable as e:
            raise as_http(e) from e
    if done:
        await core.journal.emit(f"transfer.{body.action}_many", M(f"transfer.{body.action}_many", n=done))
    return {"ok": True, "done": done}


@router.post("/transfers/clear")
async def clear_transfers(direction: str = "download", core: Core = Depends(get_core)) -> dict:
    """Removes finished transfers from the list (history stays in the journal)."""
    async with core.db.session() as s:
        res = await s.execute(
            delete(Transfer).where(Transfer.direction == direction, Transfer.state.in_(list(TERMINAL)))
        )
        await s.commit()
    return {"removed": res.rowcount}


# --- Browse a peer ----------------------------------------------------------------


@router.get("/users/{username}/browse")
async def browse(username: str, core: Core = Depends(get_core)) -> dict:
    try:
        data = await core.engine.browse(username)
    except EngineUnavailable as e:
        raise as_http(e) from e
    except TimeoutError as e:
        raise http_error(504, "error.peer_no_answer", peer=username) from e
    except NoSuchUserError as e:
        raise http_error(404, "error.peer_unknown", peer=username) from e
    except NetworkError as e:
        # No connection either way: tell an offline peer from one that can't be reached
        status = await core.engine.user_status(username)
        key = "error.peer_offline" if status == "offline" else "error.peer_unreachable"
        raise http_error(502, key, peer=username) from e
    await core.journal.emit(
        "browse", M("browse", peer=username, n=len(data["directories"])),
        data={"username": username},
    )
    return data


# --- Shares -----------------------------------------------------------------------


class ShareIn(BaseModel):
    path: str
    enabled: bool = True
    allowed_extensions: list[str] = []
    exclude_patterns: list[str] = []


@router.get("/shares")
async def list_shares(core: Core = Depends(get_core)) -> dict:
    settings = await core.settings.get()
    async with core.db.session() as s:
        shares = [share_dict(x) for x in (await s.execute(select(Share).order_by(Share.path))).scalars()]
    for sh in shares:
        sh["exists"] = os.path.isdir(sh["path"])
    return {
        "shares": shares,
        "library": {"path": str(core.env.music_dir.resolve()), "shared": settings.soulseek.share_library},
        "engine": {
            "shared_folders": core.engine.status()["shared_folders"],
            "shared_files": core.engine.status()["shared_files"],
        },
    }


@router.post("/shares")
async def create_share(body: ShareIn, core: Core = Depends(get_core)) -> dict:
    path = os.path.abspath(body.path)
    if not os.path.isdir(path):
        raise http_error(400, "error.folder_missing", path=path)
    reason = forbidden_share_reason(path, [str(core.env.config_dir), str(core.env.downloads_dir)])
    if reason:
        raise http_error(400, reason)
    async with core.db.session() as s:
        if (await s.execute(select(Share).where(Share.path == path))).scalar_one_or_none():
            raise http_error(409, "error.already_shared")
        sh = Share(**(body.model_dump() | {"path": path}))
        s.add(sh)
        await s.commit()
        await s.refresh(sh)
    await core.journal.emit("share.added", M("share.added", path=path), data=body.model_dump())
    asyncio.create_task(core.shares.rescan())
    return share_dict(sh)


@router.put("/shares/{sid}")
async def update_share(sid: int, body: ShareIn, core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        sh = await s.get(Share, sid)
        if not sh:
            raise HTTPException(404)
        sh.enabled = body.enabled
        sh.allowed_extensions = body.allowed_extensions
        sh.exclude_patterns = body.exclude_patterns
        await s.commit()
    await core.journal.emit(
        "share.updated",
        M(
            "share.updated", path=sh.path, state=M("word.enabled" if body.enabled else "word.disabled"),
            extensions=", ".join(body.allowed_extensions) or M("word.default"),
            excludes=", ".join(body.exclude_patterns) or M("word.none"),
        ),
        data=body.model_dump(),
    )
    asyncio.create_task(core.shares.rescan())
    return share_dict(sh)


@router.delete("/shares/{sid}")
async def delete_share(sid: int, core: Core = Depends(get_core)) -> dict:
    async with core.db.session() as s:
        sh = await s.get(Share, sid)
        if sh:
            await s.delete(sh)
            await s.commit()
            await core.journal.emit("share.removed", M("share.removed", path=sh.path))
    asyncio.create_task(core.shares.rescan())
    return {"ok": True}


@router.get("/shares/{sid}/preview")
async def preview_share(sid: int, core: Core = Depends(get_core)) -> dict:
    try:
        return await core.shares.preview(sid)
    except KeyError as e:
        raise HTTPException(404) from e


@router.post("/shares/rescan")
async def rescan(core: Core = Depends(get_core)) -> dict:
    asyncio.create_task(core.shares.rescan())
    return {"ok": True}


@router.get("/fs")
async def list_dirs(path: str = "/", core: Core = Depends(get_core)) -> dict:
    """Folder picker to add a share (folders only)."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise HTTPException(404)
    try:
        entries = sorted(
            e.name for e in os.scandir(path) if e.is_dir(follow_symlinks=True) and not e.name.startswith(".")
        )
    except PermissionError as e:
        raise http_error(403, "error.access_denied") from e
    return {"path": path, "parent": os.path.dirname(path) if path != "/" else None, "dirs": entries[:500]}
