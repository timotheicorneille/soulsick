"""Paroles depuis LRCLIB (lrclib.net) : libre, sans clé, beaucoup de paroles synchronisées.

Seulement pour les pistes qui n'en ont pas : un fichier de paroles ou des paroles intégrées
ne sont jamais remplacés. Le résultat va dans un fichier à côté de la piste (.lrc si
synchronisées, .txt sinon), que Navidrome lit ; les tags du fichier audio ne changent pas.

LRCLIB est bénévole et parfois saturé (503) : on va doucement, et une passe s'arrête dès
qu'il ne répond plus, pour reprendre plus tard là où elle en était.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import or_, select

from ..messages import M
from ..models import LibraryFile
from ..musicbrainz import USER_AGENT

if TYPE_CHECKING:
    from ..core import Core

log = logging.getLogger(__name__)

URL = "https://lrclib.net/api/get"
SEARCH_URL = "https://lrclib.net/api/search"
DURATION_SLACK = 3  # seconds: beyond, it is another version of the song
RETRY_AFTER = timedelta(days=30)  # LRCLIB grows: what wasn't there is asked again
PAUSE = 0.5  # between two requests
BACKOFF = (5, 20, 60)  # LRCLIB busy: waits before giving up the pass
QUICK = (2, 4)  # the same, when you are waiting for it


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(r"[\W_]+", "", "".join(c for c in text if not unicodedata.combining(c)))


def _bare_title(title: str) -> str:
    """The title without what LRCLIB often writes otherwise: (Remastered), [Live], feat. X,
    a leading « Θ. »."""
    title = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title)
    title = re.sub(r"\s(feat|ft)\.?\s.*$", " ", title, flags=re.I)
    title = re.sub(r"^\W*\w\.\s+", "", title.strip())
    return title.strip()


def pick(results: list[dict], f: LibraryFile) -> dict | None:
    """Among LRCLIB's search results, the same song: same artist, same title (bare), duration
    within DURATION_SLACK. Synced first, then plain lyrics, then instrumental; then the closest
    duration, then the same album. None rather than another version."""
    artist, title = _norm(f.tag_artist or ""), _norm(_bare_title(f.tag_title or ""))
    if not (artist and title and f.duration):
        return None
    ok = []
    for r in results:
        theirs = _norm(r.get("artistName") or "")
        if not theirs or (artist not in theirs and theirs not in artist):
            continue
        if _norm(_bare_title(r.get("trackName") or "")) != title:
            continue
        if not r.get("duration") or abs(r["duration"] - f.duration) > DURATION_SLACK:
            continue
        ok.append(r)
    ok.sort(key=lambda r: (
        0 if r.get("syncedLyrics") else 1 if r.get("plainLyrics") else 2,
        abs(r["duration"] - f.duration),
        _norm(r.get("albumName") or "") != _norm(f.tag_album or ""),
    ))
    return ok[0] if ok else None


class LrclibUnavailable(Exception):
    pass


class LyricsFetchService:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20)
        self.job = {"running": False, "done": 0, "total": 0, "found": 0}
        self._tasks: set[asyncio.Task] = set()

    # --- One track ---------------------------------------------------------------

    async def _call(self, url: str, params: dict, patient: bool):
        """One LRCLIB request: its JSON, or None if it doesn't know. Busy: waits and tries again
        (long in the background, briefly when you are waiting), then LrclibUnavailable."""
        for wait in (*(BACKOFF if patient else QUICK), None):
            try:
                r = await self.client.get(url, params=params)
            except httpx.HTTPError as e:
                err = str(e)
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (400, 404):
                    return None
                err = f"HTTP {r.status_code}"
            if wait is None:
                raise LrclibUnavailable(err)
            await asyncio.sleep(wait)
        return None

    async def _ask(self, f: LibraryFile, patient: bool = True) -> dict | None:
        """LRCLIB's answer for this file, None if it doesn't know the track.

        First the exact lookup (artist, title, album, duration within 2 s). It misses as soon as
        LRCLIB files the song under another album or spelling: then a search by artist and
        title, kept only if it is the same song (see `pick`)."""
        exact = await self._call(URL, {
            "artist_name": f.tag_artist or "",
            "track_name": f.tag_title or "",
            "album_name": f.tag_album or "",
            "duration": round(f.duration or 0),
        }, patient)
        if exact:
            return exact
        titles = list(dict.fromkeys([f.tag_title or "", _bare_title(f.tag_title or "")]))
        for title in titles:
            found = await self._call(SEARCH_URL, {"artist_name": f.tag_artist or "", "track_name": title}, patient)
            best = pick(found or [], f)
            if best:
                return best
        return None

    async def fetch_file(self, file_id: int, patient: bool = True) -> str | None:
        """Lyrics for one shelved track: « synced », « plain », or None (none written)."""
        async with self.core.db.session() as s:
            f = await s.get(LibraryFile, file_id)
            if not f or f.lyrics_file or f.lyrics_embedded or not (f.tag_title and f.tag_artist and f.duration):
                return None
        answer = await self._ask(f, patient)  # no session held meanwhile: LRCLIB may keep us waiting
        synced, plain = (answer or {}).get("syncedLyrics"), (answer or {}).get("plainLyrics")
        ext, missing = None, None
        if answer and answer.get("instrumental"):
            missing = "instrumental"
        elif synced or plain:
            ext = "lrc" if synced else "txt"
            target = os.path.splitext(f.path)[0] + "." + ext
            if not os.path.exists(target):  # appeared meanwhile (by hand): it stays
                await asyncio.to_thread(_write, target, synced or plain)
        else:
            missing = "not_found"
        async with self.core.db.session() as s:
            row = await s.get(LibraryFile, file_id)
            if row:
                row.lyrics_checked_at = datetime.now(UTC)
                row.lyrics_missing = missing
                if ext:
                    row.lyrics_file = ext
                await s.commit()
        return None if missing else ("synced" if synced else "plain")

    # --- Several -----------------------------------------------------------------

    async def _todo(self, *, release_group_mbid: str | None = None, retry: bool = False) -> list[int]:
        """Tracks without lyrics; those asked recently are left out unless `retry`."""
        q = select(LibraryFile.id).where(
            LibraryFile.lyrics_file.is_(None),
            or_(LibraryFile.lyrics_embedded.is_(None), LibraryFile.lyrics_embedded == ""),
        )
        if release_group_mbid:
            q = q.where(LibraryFile.release_group_mbid == release_group_mbid)
        if not retry:
            cutoff = datetime.now(UTC) - RETRY_AFTER
            q = q.where(or_(LibraryFile.lyrics_checked_at.is_(None), LibraryFile.lyrics_checked_at < cutoff))
        async with self.core.db.session() as s:
            return list((await s.execute(q.order_by(LibraryFile.path))).scalars())

    async def fetch_album(self, release_group_mbid: str, *, by_user: bool = False) -> dict:
        """The album's tracks without lyrics. `by_user`: even those tried recently, and no waiting
        on a busy LRCLIB."""
        ids = await self._todo(release_group_mbid=release_group_mbid, retry=by_user)
        found, error = 0, None
        for i, fid in enumerate(ids):
            if i:
                await asyncio.sleep(PAUSE)
            try:
                found += await self.fetch_file(fid, patient=not by_user) is not None
            except LrclibUnavailable as e:
                error = str(e)
                break
        return {"asked": len(ids), "found": found, "error": error}

    def after_import(self, release_group_mbid: str) -> None:
        """An album was just shelved: its missing lyrics, in the background."""
        async def run() -> None:
            if not (await self.core.settings.get()).library.fetch_lyrics:
                return
            r = await self.fetch_album(release_group_mbid)
            if r["found"]:
                await self.core.journal.emit("lyrics.fetched", M("lyrics.fetched", n=r["found"], total=r["asked"]))

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def start_library(self) -> bool:
        """The whole library, on demand, in the background; False if a pass is already running."""
        if self.job["running"]:
            return False
        self.job = {"running": True, "done": 0, "total": 0, "found": 0}
        task = asyncio.create_task(self._library())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _library(self) -> None:
        error = None
        try:
            ids = await self._todo()
            self.job["total"] = len(ids)
            self.core.hub.publish("lyrics", self.job)
            for fid in ids:
                try:
                    self.job["found"] += await self.fetch_file(fid) is not None
                except LrclibUnavailable as e:
                    error = str(e)
                    break
                self.job["done"] += 1
                if self.job["done"] % 10 == 0:
                    self.core.hub.publish("lyrics", self.job)
                await asyncio.sleep(PAUSE)
        finally:
            self.job["running"] = False
            self.core.hub.publish("lyrics", self.job)
        if error:
            await self.core.journal.emit(
                "lyrics.unavailable",
                M("lyrics.unavailable", done=self.job["done"], total=self.job["total"], error=error),
                level="warning",
            )
        await self.core.journal.emit(
            "lyrics.library_done",
            M("lyrics.library_done", n=self.job["found"], total=self.job["done"]),
        )


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
