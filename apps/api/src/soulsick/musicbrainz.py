"""Client MusicBrainz + Cover Art Archive.

Respecte la politique de l'API : 1 requête/seconde, User-Agent identifiable.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import __version__

log = logging.getLogger(__name__)

MB_URL = "https://musicbrainz.org/ws/2"
CAA_URL = "https://coverartarchive.org"
USER_AGENT = f"Soulsick/{__version__} ( https://github.com/soulsick/soulsick )"


class MusicBrainzError(Exception):
    pass


class MusicBrainz:
    def __init__(self, covers_dir: Path, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
            follow_redirects=True,
        )
        self.covers_dir = covers_dir
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: dict, ttl: float = 3600) -> Any:
        params = {**params, "fmt": "json"}
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]

        for attempt in range(4):
            async with self._lock:
                wait = 1.05 - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
                try:
                    r = await self.client.get(f"{MB_URL}/{path}", params=params)
                except httpx.HTTPError as e:
                    err: Exception = e
                else:
                    if r.status_code == 200:
                        data = r.json()
                        self._cache[key] = (time.monotonic(), data)
                        return data
                    if r.status_code == 404:
                        raise MusicBrainzError(f"Not found on MusicBrainz: {path}")
                    err = MusicBrainzError(f"MusicBrainz answered {r.status_code}")
            await asyncio.sleep(2 ** attempt)
        raise MusicBrainzError(f"MusicBrainz unavailable: {err}")

    # --- Artistes ----------------------------------------------------------

    async def search_artists(self, query: str, limit: int = 15) -> list[dict]:
        data = await self._get("artist", {"query": query, "limit": limit}, ttl=600)
        return [
            {
                "mbid": a["id"],
                "name": a["name"],
                "sort_name": a.get("sort-name", ""),
                "disambiguation": a.get("disambiguation", ""),
                "country": a.get("country"),
                "type": a.get("type"),
                "score": a.get("score", 0),
                "begin": (a.get("life-span") or {}).get("begin"),
                "tags": [t["name"] for t in sorted(a.get("tags", []), key=lambda t: -t.get("count", 0))[:4]],
            }
            for a in data.get("artists", [])
        ]

    async def get_artist(self, mbid: str) -> dict:
        a = await self._get(f"artist/{mbid}", {})
        return {
            "mbid": a["id"],
            "name": a["name"],
            "sort_name": a.get("sort-name", ""),
            "disambiguation": a.get("disambiguation", ""),
            "country": a.get("country"),
            "type": a.get("type"),
        }

    async def artist_release_groups(self, artist_mbid: str) -> list[dict]:
        """Release groups de l'artiste, sans ceux qui n'ont que des bootlegs ou des promos.

        « website-default » applique le même filtre que le site MusicBrainz. Parcourir les
        release groups plutôt que les releases demande 2 à 4 fois moins de pages (une seconde
        chacune) : une sortie a souvent plusieurs éditions.
        """
        groups: dict[str, dict] = {}
        offset = 0
        while True:
            data = await self._get(
                "release-group",
                {"artist": artist_mbid, "release-group-status": "website-default", "limit": 100, "offset": offset},
                ttl=600,
            )
            page = data.get("release-groups", [])
            for g in page:
                if not g.get("id") or g["id"] in groups:
                    continue
                groups[g["id"]] = {
                    "mbid": g["id"],
                    "title": g.get("title", ""),
                    "primary_type": (g.get("primary-type") or "").lower() or None,
                    "secondary_types": [t.lower() for t in g.get("secondary-types", [])],
                    "first_release_date": g.get("first-release-date") or None,
                }
            offset += len(page)
            if not page or offset >= data.get("release-group-count", 0):
                break
        return sorted(groups.values(), key=lambda g: g["first_release_date"] or "9999")

    async def search_release_groups(self, artist: str, album: str, limit: int = 5) -> list[dict]:
        """Release groups correspondant à « artiste + album » (pour reconnaître de la musique déjà présente)."""

        def q(v: str) -> str:
            return v.replace("\\", " ").replace('"', " ").strip()

        queries = []
        if artist and album:
            queries.append(f'releasegroup:"{q(album)}" AND artist:"{q(artist)}"')
        if album:
            queries.append(f'releasegroup:"{q(album)}"')
        for query in queries:
            data = await self._get("release-group", {"query": query, "limit": limit}, ttl=3600)
            out = []
            for g in data.get("release-groups", []):
                credit = (g.get("artist-credit") or [{}])[0]
                a = credit.get("artist") or {}
                if not a.get("id"):
                    continue
                out.append(
                    {
                        "mbid": g["id"],
                        "title": g["title"],
                        "primary_type": (g.get("primary-type") or "").lower() or None,
                        "secondary_types": [t.lower() for t in g.get("secondary-types", [])],
                        "first_release_date": g.get("first-release-date") or None,
                        "artist_mbid": a["id"],
                        "artist_name": a.get("name", ""),
                        "score": g.get("score", 0),
                    }
                )
            if out:
                return out
        return []

    async def get_release_group(self, mbid: str) -> dict:
        g = await self._get(f"release-group/{mbid}", {"inc": "artist-credits"})
        credit = (g.get("artist-credit") or [{}])[0].get("artist") or {}
        return {
            "mbid": g["id"],
            "title": g["title"],
            "primary_type": (g.get("primary-type") or "").lower() or None,
            "secondary_types": [t.lower() for t in g.get("secondary-types", [])],
            "first_release_date": g.get("first-release-date") or None,
            "artist_mbid": credit.get("id"),
            "artist_name": credit.get("name", ""),
        }

    # --- Éditions et pistes -------------------------------------------------

    async def release_group_releases(self, rg_mbid: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            data = await self._get(
                "release",
                {
                    "release-group": rg_mbid,
                    "inc": "recordings+media+labels+artist-credits",
                    "limit": 100,
                    "offset": offset,
                },
                ttl=600,
            )
            releases = data.get("releases", [])
            for r in releases:
                tracks = []
                media = r.get("media", [])
                for disc_idx, medium in enumerate(media, start=1):
                    for t in medium.get("tracks", []) or []:
                        rec = t.get("recording") or {}
                        tracks.append(
                            {
                                "mbid": t["id"],
                                "recording_mbid": rec.get("id", ""),
                                "disc": medium.get("position", disc_idx),
                                "position": t.get("position", 0),
                                "number": t.get("number", ""),
                                "title": t.get("title") or rec.get("title", ""),
                                "artist_credit": _credit(t.get("artist-credit") or rec.get("artist-credit")),
                                "length_ms": t.get("length") or rec.get("length"),
                            }
                        )
                labels = r.get("label-info") or []
                out.append(
                    {
                        "mbid": r["id"],
                        "title": r["title"],
                        "date": r.get("date") or None,
                        "country": r.get("country"),
                        "status": r.get("status"),
                        "disambiguation": r.get("disambiguation", ""),
                        "formats": " + ".join(sorted({m.get("format") or "?" for m in media})) if media else "",
                        "track_count": len(tracks),
                        "disc_count": max(1, len(media)),
                        "label": (labels[0].get("label") or {}).get("name") if labels else None,
                        "barcode": r.get("barcode") or None,
                        "tracks": tracks,
                    }
                )
            offset += len(releases)
            if not releases or offset >= data.get("release-count", 0):
                break
        return out

    # --- Pochettes ----------------------------------------------------------

    async def cover(self, kind: str, mbid: str, size: int = 500) -> Path | None:
        """Télécharge (et met en cache) la pochette d'un release-group ou d'une release."""
        assert kind in {"release-group", "release"}
        path = self.covers_dir / f"{kind}-{mbid}-{size}.jpg"
        missing = self.covers_dir / f"{kind}-{mbid}-{size}.missing"
        if path.exists():
            return path
        if missing.exists() and time.time() - missing.stat().st_mtime < 86400 * 7:
            return None
        try:
            r = await self.client.get(f"{CAA_URL}/{kind}/{mbid}/front-{size}", headers={"Accept": "image/*"})
        except httpx.HTTPError as e:
            log.warning("cover %s %s: %s", kind, mbid, e)
            return None
        if r.status_code != 200:
            missing.touch()
            return None
        path.write_bytes(r.content)
        return path


def _credit(credit: list[dict] | None) -> str:
    if not credit:
        return ""
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in credit)


def pick_default_release(releases: list[dict]) -> dict | None:
    """Édition par défaut : officielle, la plus ancienne, en privilégiant CD/digital et l'absence de bonus."""
    if not releases:
        return None

    def key(r: dict) -> tuple:
        official = 0 if (r.get("status") or "").lower() == "official" else 1
        fmt = (r.get("formats") or "").lower()
        fmt_rank = 0 if ("cd" in fmt or "digital" in fmt) else 1
        date = r.get("date") or "9999"
        return (official, fmt_rank, date)

    return sorted(releases, key=key)[0]
