"""Music that is already there: recognise it, tag it, shelve it.

Two sources:
- files in /music without MusicBrainz IDs (downloaded with slskd, ripped…), grouped by folder;
- manual downloads (downloads/manual/<peer>/<folder>), even a single track of an album.

1. Identification: artist + album guessed (tags, else folder names) → MusicBrainz search →
   track-by-track mapping with a confidence score.
2. Confirmation: each file gets its MusicBrainz tags and is shelved with the naming template. Extras
   (covers, .cue, .log) follow; the old folder is removed when empty. Nothing is downloaded.
A partial album (a few tracks only) is shelved as such and shows as "n/total" in the library.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from .. import lyrics
from ..audiotags import read_meta
from ..matching import FileRef, TrackRef, match_files
from ..messages import M, UserError
from ..models import Artist, ArtistReleaseGroup, LibraryFile, Release, ReleaseGroup, Transfer
from ..quality import is_audio, worst_format
from .importer import destination, place_track

if TYPE_CHECKING:
    from ..core import Core

log = logging.getLogger(__name__)

EXTRAS = {"jpg", "jpeg", "png", "cue", "log", "txt", "nfo", "m3u", "m3u8", "pdf"}
SAFE_CONFIDENCE = 0.85
_YEAR = re.compile(r"^\s*[\[(]?(19|20)\d{2}[\])]?\s*[-–.]?\s*|\s*[\[(](19|20)\d{2}[\])]\s*$")
_NOISE = re.compile(r"\s*[\[(](flac|mp3|320|v0|web|cd|vinyl|24bit|16bit|24-?\d+|deluxe[^\])]*)[\])]\s*", re.I)


def clean_name(name: str) -> str:
    name = _NOISE.sub(" ", name)
    name = _YEAR.sub("", name)
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip(" -–")


@dataclass
class LooseFile:
    """An audio file outside the library index (manual download), seen like a LibraryFile."""

    path: str
    size: int
    format: str | None
    tag_artist: str | None
    tag_album: str | None


def guess(dir_path: str, files: list) -> tuple[str, str]:
    """Most likely (artist, album): majority tags, else "Artist/Album" or "Artist - Album" folders."""
    artist = Counter(f.tag_artist for f in files if f.tag_artist).most_common(1)
    album = Counter(f.tag_album for f in files if f.tag_album).most_common(1)
    a = artist[0][0] if artist else ""
    b = album[0][0] if album else ""
    parts = [p for p in dir_path.replace("\\", "/").split("/") if p]
    folder = parts[-1] if parts else ""
    if not b:
        if " - " in folder:
            left, right = folder.split(" - ", 1)
            b = clean_name(right)
            a = a or clean_name(left)
        else:
            b = clean_name(folder)
    if not a and len(parts) >= 2:
        a = clean_name(parts[-2])
    return a, b


@dataclass
class Proposal:
    release_group_mbid: str
    release_mbid: str
    title: str
    artist_name: str
    artist_mbid: str
    date: str | None
    release_title: str
    release_info: str
    track_count: int
    confidence: float
    matched: int
    mapping: list[dict] = field(default_factory=list)
    unmatched_files: list[str] = field(default_factory=list)
    missing_tracks: list[str] = field(default_factory=list)
    destination: str | None = None
    # A few tracks of an album (manual download): judged file by file, missing tracks are expected
    partial: bool = False
    file_confidence: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__ | {"safe": self.safe}

    @property
    def safe(self) -> bool:
        if self.partial:
            return self.file_confidence >= SAFE_CONFIDENCE and not self.unmatched_files
        return self.confidence >= SAFE_CONFIDENCE and not self.unmatched_files and not self.missing_tracks


class ExistingImportService:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.results: dict[str, dict] = {}  # dossier → {"proposals": [...], "query": {...}, "error": str?}
        self.job: dict | None = None
        self._lock = asyncio.Lock()

    def _roots(self) -> tuple[str, str]:
        return (
            os.path.realpath(str(self.core.env.music_dir)),
            os.path.realpath(os.path.join(str(self.core.env.downloads_dir), "manual")),
        )

    def _root_of(self, d: str) -> str:
        """The allowed root containing this folder (library or manual downloads), else refused."""
        real = os.path.realpath(d)
        for root in self._roots():
            if real.startswith(root + os.sep):
                return root
        raise UserError("error.folder_outside_library")

    def _check_dir(self, d: str) -> str:
        self._root_of(d)
        return os.path.realpath(d)

    def is_manual(self, d: str) -> bool:
        return self._root_of(d) == self._roots()[1]

    async def _files(self, d: str) -> list:
        """Unrecognised audio files of a folder: from the library index, or read from disk (manual)."""
        if self.is_manual(d):
            names = sorted(n for n in os.listdir(d) if is_audio(n) and os.path.isfile(os.path.join(d, n))) if os.path.isdir(d) else []
            out = []
            for n in names:
                p = os.path.join(d, n)
                m = await asyncio.to_thread(read_meta, p)
                out.append(LooseFile(p, os.path.getsize(p), m.format, m.albumartist or m.artist, m.album))
            return out
        async with self.core.db.session() as s:
            fs = list(
                (await s.execute(select(LibraryFile).where(LibraryFile.path.like(f"{d}{os.sep}%")))).scalars()
            )
        return [f for f in fs if os.path.dirname(f.path) == d and not f.release_group_mbid and not f.release_mbid]

    async def describe(self, d: str) -> dict:
        """A folder as shown in the UI (files, size, format, guess, last identification)."""
        d = self._check_dir(d)
        fs = await self._files(d)
        a, b = guess(d, fs)
        return {
            "dir": d, "files": len(fs), "size": sum(f.size for f in fs), "format": worst_format([f.format for f in fs], []),
            "guess": {"artist": a, "album": b}, "result": self.results.get(d),
        }

    async def groups(self) -> list[dict]:
        files = await self.core.library.unmatched_files(limit=20000)
        by_dir: dict[str, list[LibraryFile]] = {}
        for f in files:
            by_dir.setdefault(os.path.dirname(f.path), []).append(f)
        out = []
        for d, fs in sorted(by_dir.items()):
            a, b = guess(d, fs)
            out.append(
                {
                    "dir": d,
                    "files": len(fs),
                    "size": sum(f.size for f in fs),
                    "format": worst_format([f.format for f in fs], []),
                    "guess": {"artist": a, "album": b},
                    "result": self.results.get(d),
                }
            )
        return out

    # --- Identification -------------------------------------------------------

    async def identify(self, d: str, artist: str | None = None, album: str | None = None) -> dict:
        d = self._check_dir(d)
        partial = self.is_manual(d)
        fs = await self._files(d)
        if not fs:
            raise UserError("error.no_unrecognised_files")
        ga, gb = guess(d, fs)
        a, b = (artist if artist is not None else ga), (album if album is not None else gb)
        metas = [await asyncio.to_thread(read_meta, f.path) for f in fs]
        refs = [
            FileRef(path=f.path, duration_s=m.duration, tag_title=m.title, tag_track=m.track, tag_disc=m.disc)
            for f, m in zip(fs, metas, strict=True)
        ]
        try:
            groups = await self.core.mb.search_release_groups(a, b)
        except Exception as e:
            res = {"proposals": [], "query": {"artist": a, "album": b}, "error": f"MusicBrainz: {e}"}
            self.results[d] = res
            return res

        settings = await self.core.settings.get()
        music = str(self.core.env.music_dir)
        proposals: list[Proposal] = []
        for g in groups[:3]:
            releases = await self.core.mb.release_group_releases(g["mbid"])
            seen: set[tuple] = set()
            best = None
            for r in releases:
                if not r["tracks"]:
                    continue
                sig = tuple(t["title"].lower() for t in r["tracks"])
                if sig in seen:
                    continue
                seen.add(sig)
                tracks = [TrackRef(t["disc"], t["position"], t["title"], (t["length_ms"] or 0) / 1000 or None) for t in r["tracks"]]
                m = match_files(tracks, refs, artist_hint=g["artist_name"])
                # Partial (a few tracks): the edition whose titles fit the files best wins
                score = (sum(x.title_score for x in m.assignments), m.confidence) if partial else (m.confidence, 0)
                if best is None or score > best[2]:
                    best = (m, r, score)
            if best is None:
                continue
            m, r, _ = best
            by_file = {x.file_index: x for x in m.assignments}
            file_conf = sum(x.title_score for x in m.assignments) / len(fs)
            mapping = []
            for i, f in enumerate(fs):
                x = by_file.get(i)
                t = r["tracks"][x.track_index] if x else None
                mapping.append(
                    {
                        "file": f.path,
                        "name": os.path.basename(f.path),
                        "track_mbid": t["mbid"] if t else None,
                        "track": f"{t['disc']}-{t['position']:02d} {t['title']}" if t else None,
                    }
                )
            dest = None
            first = next((t for t in r["tracks"]), None)
            if first:
                fake_artist = Artist(name=g["artist_name"], mbid=g["artist_mbid"])
                fake_rg = ReleaseGroup(title=g["title"], mbid=g["mbid"], primary_type=g["primary_type"],
                                       first_release_date=g["first_release_date"])
                fake_rel = Release(title=r["title"], mbid=r["mbid"], date=r["date"], disc_count=r["disc_count"])
                fake_track = _TrackLike(first)
                dest = os.path.dirname(destination(fake_track, fake_artist, fake_rg, fake_rel,  # type: ignore[arg-type]
                                                   settings.library.naming_template, music, "flac"))
            proposals.append(
                Proposal(
                    release_group_mbid=g["mbid"],
                    release_mbid=r["mbid"],
                    title=g["title"],
                    artist_name=g["artist_name"],
                    artist_mbid=g["artist_mbid"],
                    date=g["first_release_date"],
                    release_title=r["title"],
                    release_info=" · ".join(x for x in [r.get("date"), r.get("country"), r.get("formats")] if x),
                    track_count=r["track_count"],
                    confidence=round(m.confidence, 3),
                    matched=len(m.assignments),
                    mapping=mapping,
                    unmatched_files=[os.path.basename(fs[i].path) for i in m.unmatched_files],
                    missing_tracks=[r["tracks"][i]["title"] for i in m.unmatched_tracks],
                    destination=dest,
                    partial=partial and len(fs) < r["track_count"],
                    file_confidence=round(file_conf, 3),
                )
            )
        proposals.sort(key=lambda p: -(p.file_confidence if p.partial else p.confidence))
        res = {"proposals": [p.to_dict() for p in proposals], "query": {"artist": a, "album": b}}
        self.results[d] = res
        return res

    async def identify_all(self) -> None:
        """Identifie en arrière-plan tous les dossiers pas encore identifiés (1 requête MusicBrainz/s)."""
        if self.job and self.job.get("running"):
            return
        todo = [g["dir"] for g in await self.groups() if not g["result"]]
        self.job = {"running": True, "done": 0, "total": len(todo)}
        await self.core.journal.emit(
            "library.identify", M("library.identify_started", n=len(todo))
        )
        try:
            for d in todo:
                try:
                    await self.identify(d)
                except Exception as e:
                    self.results[d] = {"proposals": [], "error": str(e)}
                self.job["done"] += 1
                self.core.hub.publish("existing", self.job)
        finally:
            self.job["running"] = False
            self.core.hub.publish("existing", self.job)
            safe = sum(
                1 for r in self.results.values() if r.get("proposals") and r["proposals"][0].get("safe")
            )
            await self.core.journal.emit(
                "library.identify",
                M("library.identify_done", n=self.job["total"], safe=safe),
            )

    # --- Import -----------------------------------------------------------------

    async def apply(self, d: str, release_group_mbid: str, release_mbid: str, follow: bool) -> dict:
        async with self._lock:
            return await self._apply(d, release_group_mbid, release_mbid, follow)

    async def _apply(self, d: str, rg_mbid: str, rel_mbid: str, follow: bool) -> dict:
        await self.core.guard.check()
        self.core.guard.require()
        d = self._check_dir(d)
        res = self.results.get(d) or await self.identify(d)
        prop = next(
            (p for p in res["proposals"] if p["release_group_mbid"] == rg_mbid and p["release_mbid"] == rel_mbid), None
        )
        if not prop:
            raise UserError("error.unknown_match")

        # Artiste et sortie dans le catalogue (sans rien suivre ni télécharger)
        artist_row = await self._ensure_artist(prop["artist_mbid"], follow)
        rg_row = await self._ensure_release_group(rg_mbid, artist_row.id)
        await self.core.catalog.ensure_releases(rg_row.id)
        releases = await self.core.catalog.releases_with_tracks(rg_row.id)
        rel = next((r for r in releases if r.mbid == rel_mbid), None)
        if rel is None:
            raise UserError("error.edition_not_found")
        by_mbid = {t.mbid: t for t in rel.tracks}

        settings = await self.core.settings.get()
        music = str(self.core.env.music_dir)
        p = await self.core.mb.cover("release", rel.mbid, 1200) or await self.core.mb.cover("release-group", rg_mbid, 1200)
        cover = p.read_bytes() if p else None

        async with self.core.db.session() as s:
            # Tracks of this album already shelved (a partial album being filled) may be replaced
            shelved = list(
                (await s.execute(select(LibraryFile).where(LibraryFile.release_group_mbid == rg_mbid))).scalars()
            )
        replaceable = {os.path.realpath(f.path) for f in shelved}
        moved, skipped, replaced = [], [], []
        for item in prop["mapping"]:
            t = by_mbid.get(item["track_mbid"]) if item["track_mbid"] else None
            if t is None or not os.path.isfile(item["file"]):
                skipped.append(item["name"])
                continue
            dest = await place_track(item["file"], t, artist_row, rg_row, rel, settings, music, cover, replaceable)
            moved.append({"from": item["file"], "to": dest, "track": t.title})
            # The same track of this album already shelved under another name (other format or
            # edition): the new file replaces it, so the album never holds a track twice
            for old in shelved:
                if old.recording_mbid == t.recording_mbid and os.path.realpath(old.path) != os.path.realpath(dest):
                    if os.path.exists(old.path):
                        lyrics.drop_replaced(old.path, dest)  # its lyrics stay, on the new file
                        replaced.append(old.path)
        manual = self.is_manual(d)
        if manual and moved:
            # The manual download now lives in the library: remember where it went
            async with self.core.db.session() as s:
                by_src = {os.path.realpath(m["from"]): m["to"] for m in moved}
                for tr in (await s.execute(select(Transfer).where(Transfer.local_path.like(f"{d}{os.sep}%")))).scalars():
                    if tr.local_path and os.path.realpath(tr.local_path) in by_src:
                        tr.shelved_path = by_src[os.path.realpath(tr.local_path)]
                        tr.release_group_id = rg_row.id
                await s.commit()

        album_dir = os.path.commonpath([os.path.dirname(m["to"]) for m in moved]) if moved else None
        extras_moved = []
        if album_dir and os.path.realpath(album_dir) != d:
            # Les annexes suivent l'album ; l'ancien dossier disparaît s'il est vide
            for name in sorted(os.listdir(d)):
                src = os.path.join(d, name)
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if os.path.isfile(src) and ext in EXTRAS and not os.path.exists(os.path.join(album_dir, name)):
                    await asyncio.to_thread(shutil.move, src, os.path.join(album_dir, name))
                    extras_moved.append(name)
            # Folders left empty are removed going up, never past the root (library or manual downloads)
            root = self._roots()[1 if manual else 0]
            cur = d
            while cur != root and cur.startswith(root + os.sep):
                try:
                    if os.listdir(cur):
                        break
                    os.rmdir(cur)
                except OSError:
                    break
                cur = os.path.dirname(cur)
        if album_dir and cover and not os.path.exists(os.path.join(album_dir, "cover.jpg")):
            with open(os.path.join(album_dir, "cover.jpg"), "wb") as fh:
                fh.write(cover)

        self.results.pop(d, None)
        await self.core.library.scan()
        self.core.lyrics.after_import(rg_mbid)  # lyrics of the tracks that have none
        await self.core.journal.emit(
            "import.existing",
            M(
                "import.manual" if manual else "import.existing", artist=artist_row.name, title=rg_row.title, n=len(moved), dir=album_dir,
                details=[
                    *([M("import.existing_extras", n=len(extras_moved))] if extras_moved else []),
                    *([M("import.existing_skipped", n=len(skipped))] if skipped else []),
                    *([M("import.detail.replaced", n=len(replaced))] if replaced else []),
                ],
            ),
            artist_id=artist_row.id, release_group_id=rg_row.id,
            data={"from": d, "moves": moved, "extras": extras_moved, "skipped": skipped, "replaced": replaced, "follow": follow},
        )
        await self.core.navidrome.trigger_scan()
        return {"moved": len(moved), "skipped": skipped, "album_dir": album_dir, "release_group_id": rg_row.id}

    async def _ensure_artist(self, mbid: str, follow: bool) -> Artist:
        """Artiste présent dans le catalogue. Créé ici : suivi seulement si demandé (jamais de téléchargement)."""
        async with self.core.db.session() as s:
            a = (await s.execute(select(Artist).where(Artist.mbid == mbid))).scalar_one_or_none()
        created = a is None
        if created:
            a = await self.core.catalog.add_artist(
                mbid, mode="validation", quality_profile_id=None, primary_types=["album", "ep"],
                secondary_types=[], monitor="none",
            )
        async with self.core.db.session() as s:
            row = await s.get(Artist, a.id)
            assert row
            if follow != row.monitored and (created or follow):
                row.monitored = follow
                await s.commit()
            return row

    async def _ensure_release_group(self, mbid: str, artist_id: int) -> ReleaseGroup:
        async with self.core.db.session() as s:
            rg = (await s.execute(select(ReleaseGroup).where(ReleaseGroup.mbid == mbid))).scalar_one_or_none()
            if rg is None:
                # Sortie hors discographie officielle (bootleg, compilation…) : on la crée à la main
                g = await self.core.mb.get_release_group(mbid)
                rg = ReleaseGroup(
                    mbid=mbid, artist_id=artist_id, title=g["title"], primary_type=g["primary_type"],
                    secondary_types=g["secondary_types"], first_release_date=g["first_release_date"],
                )
                s.add(rg)
                await s.flush()
            linked = (
                await s.execute(
                    select(ArtistReleaseGroup).where(
                        ArtistReleaseGroup.artist_id == artist_id, ArtistReleaseGroup.release_group_id == rg.id
                    )
                )
            ).first()
            if not linked:
                s.add(ArtistReleaseGroup(artist_id=artist_id, release_group_id=rg.id))
            await s.commit()
            await s.refresh(rg)
            return rg


class _TrackLike:
    """Adaptateur : une piste MusicBrainz brute (dict) vue comme un Track pour calculer la destination."""

    def __init__(self, t: dict) -> None:
        self.title = t["title"]
        self.artist_credit = t.get("artist_credit") or ""
        self.position = t["position"]
        self.disc = t["disc"]
