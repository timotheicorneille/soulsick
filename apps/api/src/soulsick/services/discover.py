"""Discover: artists to follow, from the similar artists of those followed (ListenBrainz).

ListenBrainz (MetaBrainz, like MusicBrainz) answers from MusicBrainz ids, which every followed
artist already has: no name matching. Its similar artists are kept in the database and asked
again once a week; recommendations are computed from them, never from the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import delete, or_, select

from ..models import Artist, DismissedArtist, SimilarArtist
from ..musicbrainz import USER_AGENT

if TYPE_CHECKING:
    from ..core import Core

log = logging.getLogger(__name__)

URL = "https://labs.api.listenbrainz.org/similar-artists/json"
# Listening sessions over ~20 years: the broadest of the published algorithms
ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
KEEP = 30  # similar artists kept per followed artist
REFRESH = timedelta(days=7)
# Artist photos: MusicBrainz has none, Deezer has nearly every artist (no API key)
DEEZER_SEARCH = "https://api.deezer.com/search/artist"
NO_PHOTO_RETRY = 86400 * 7


def _same_name(a: str, b: str) -> bool:
    """Deezer is searched by name: accents, case and punctuation aside, the names must be equal."""
    def norm(v: str) -> str:
        v = unicodedata.normalize("NFKD", v.casefold())
        return re.sub(r"[\W_]+", "", "".join(c for c in v if not unicodedata.combining(c)))
    return norm(a) == norm(b)


class DiscoverService:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30)
        self._photos = asyncio.Semaphore(8)  # Deezer allows 50 requests every 5 s
        self._prefetch: set[asyncio.Task] = set()
        self._photo_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._wake = asyncio.Event()

    async def fetch(self, artist_id: int) -> bool:
        """Asks ListenBrainz for the similar artists of `artist_id`; False if it failed
        (the artist is then asked again on the next pass)."""
        async with self.core.db.session() as s:
            artist = await s.get(Artist, artist_id)
            if not artist:
                return False
            mbid = artist.mbid
        try:
            r = await self.client.get(URL, params={"artist_mbids": mbid, "algorithm": ALGORITHM})
            r.raise_for_status()
            rows = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.info("ListenBrainz, similar artists of %s: %s", mbid, e)
            return False
        rows = [x for x in rows if isinstance(x, dict) and x.get("artist_mbid") not in (None, "", mbid)]
        rows.sort(key=lambda x: -(x.get("score") or 0))
        # The same artist sometimes comes twice: its best score only
        seen: set[str] = set()
        rows = [x for x in rows if not (x["artist_mbid"] in seen or seen.add(x["artist_mbid"]))]
        async with self.core.db.session() as s:
            await s.execute(delete(SimilarArtist).where(SimilarArtist.artist_id == artist_id))
            for x in rows[:KEEP]:
                s.add(SimilarArtist(
                    artist_id=artist_id, mbid=x["artist_mbid"], name=x.get("name") or "?",
                    comment=x.get("comment") or "", type=x.get("type"), score=int(x.get("score") or 0),
                ))
            artist = await s.get(Artist, artist_id)
            if artist:
                artist.similar_fetched_at = datetime.now(UTC)
            await s.commit()
        # Their photos, before the page asks for them (a page is ~60 cards)
        task = asyncio.create_task(self._prefetch_photos([x["artist_mbid"] for x in rows[:KEEP]]))
        self._prefetch.add(task)
        task.add_done_callback(self._prefetch.discard)
        return True

    async def _prefetch_photos(self, mbids: list[str]) -> None:
        await asyncio.gather(*(self.photo(m) for m in mbids), return_exceptions=True)

    async def refresh_stale(self, limit: int = 10) -> int:
        """Fetches the artists never asked, or asked more than a week ago; returns how many."""
        cutoff = datetime.now(UTC) - REFRESH
        async with self.core.db.session() as s:
            ids = list((await s.execute(
                select(Artist.id)
                .where(or_(Artist.similar_fetched_at.is_(None), Artist.similar_fetched_at < cutoff))
                .order_by(Artist.similar_fetched_at.is_not(None), Artist.similar_fetched_at)
                .limit(limit)
            )).scalars())
        done = 0
        for i, aid in enumerate(ids):
            if i:
                await asyncio.sleep(1)  # politeness towards a free service
            done += await self.fetch(aid)
        return done

    async def refresh_all(self) -> int:
        async with self.core.db.session() as s:
            ids = list((await s.execute(select(Artist.id))).scalars())
        return sum([await self.fetch(aid) for aid in ids])

    def soon(self) -> None:
        """An artist was just followed: its similar artists are asked now, not at the next pass."""
        self._wake.set()

    async def _pause(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)
        self._wake.clear()

    async def loop(self) -> None:
        await self._pause(120)
        while True:
            try:
                while await self.refresh_stale():  # 10 at a time: a bulk add can bring more
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("discover")
            await self._pause(1800)

    async def dismiss(self, mbid: str) -> None:
        async with self.core.db.session() as s:
            if not await s.get(DismissedArtist, mbid):
                s.add(DismissedArtist(mbid=mbid))
                await s.commit()

    async def recommendations(self, limit: int = 60) -> dict:
        """Artists similar to several followed ones come first: each followed artist gives its
        similar ones a weight from 0 to 1 (relative to its best match), summed over all of them."""
        async with self.core.db.session() as s:
            artists = {a.id: a for a in (await s.execute(select(Artist))).scalars()}
            similar = list((await s.execute(select(SimilarArtist))).scalars())
            dismissed = set((await s.execute(select(DismissedArtist.mbid))).scalars())
        followed = {a.mbid for a in artists.values()}
        best: dict[int, int] = defaultdict(int)
        for x in similar:
            best[x.artist_id] = max(best[x.artist_id], x.score)
        weight: dict[str, float] = defaultdict(float)
        because: dict[str, list[tuple[float, int]]] = defaultdict(list)
        info: dict[str, SimilarArtist] = {}
        for x in similar:
            if x.mbid in followed or x.mbid in dismissed or not best[x.artist_id]:
                continue
            w = x.score / best[x.artist_id]
            weight[x.mbid] += w
            because[x.mbid].append((w, x.artist_id))
            info.setdefault(x.mbid, x)
        ranked = sorted(weight, key=lambda m: (-weight[m], info[m].name.lower()))[:limit]
        top = weight[ranked[0]] if ranked else 1
        return {
            "recommendations": [
                {
                    "mbid": m,
                    "name": info[m].name,
                    "comment": info[m].comment,
                    "type": info[m].type,
                    "score": max(1, round(100 * weight[m] / top)),
                    "because": [
                        {"id": aid, "name": artists[aid].name}
                        for _, aid in sorted(because[m], key=lambda b: -b[0])
                    ],
                }
                for m in ranked
            ],
            "pending": sum(1 for a in artists.values() if a.similar_fetched_at is None),
            "followed": len(artists),
        }

    async def similar(self, artist_id: int) -> list[dict]:
        async with self.core.db.session() as s:
            rows = list((await s.execute(
                select(SimilarArtist)
                .where(SimilarArtist.artist_id == artist_id)
                .order_by(SimilarArtist.score.desc())
            )).scalars())
            followed = {mbid: aid for mbid, aid in await s.execute(select(Artist.mbid, Artist.id))}
        return [
            {
                "mbid": x.mbid, "name": x.name, "comment": x.comment, "type": x.type,
                "followed_id": followed.get(x.mbid),
            }
            for x in rows
        ]

    async def photo(self, mbid: str) -> Path | None:
        """Photo of an artist (followed or recommended), from Deezer, kept on disk.

        Deezer is searched by name: among the artists with exactly that name, the one with
        the most fans (homonyms with a handful of fans are common). None if there is no photo.
        """
        async with self._photo_locks[mbid]:  # the page and the prefetch can ask at the same time
            return await self._photo(mbid)

    async def _photo(self, mbid: str) -> Path | None:
        covers = self.core.mb.covers_dir
        path, missing = covers / f"artist-{mbid}.jpg", covers / f"artist-{mbid}.missing"
        if path.exists():
            return path
        if missing.exists() and time.time() - missing.stat().st_mtime < NO_PHOTO_RETRY:
            return None
        async with self.core.db.session() as s:
            name = (await s.execute(select(Artist.name).where(Artist.mbid == mbid))).scalar() or (
                await s.execute(select(SimilarArtist.name).where(SimilarArtist.mbid == mbid).limit(1))
            ).scalar()
        if not name:
            return None
        async with self._photos:
            try:
                r = await self.client.get(DEEZER_SEARCH, params={"q": name, "limit": 25})
                r.raise_for_status()
                found = [
                    a for a in r.json().get("data", [])
                    if _same_name(a.get("name", ""), name)
                    and a.get("picture_big") and "/artist//" not in a["picture_big"]  # Deezer's placeholder
                ]
                if not found:
                    missing.touch()
                    return None
                best = max(found, key=lambda a: a.get("nb_fan", 0))
                img = await self.client.get(best["picture_big"])
                img.raise_for_status()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("artist photo %s: %s", name, e)
                return None  # network trouble: asked again next time
        covers.mkdir(parents=True, exist_ok=True)
        path.write_bytes(img.content)
        missing.unlink(missing_ok=True)
        return path
