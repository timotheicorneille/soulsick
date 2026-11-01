"""Bibliothèque locale (/music) : scan, rapprochement avec le catalogue via les tags MusicBrainz."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from ..audiotags import read_meta
from ..lyrics import LYRICS_EXTENSIONS, best_sidecar
from ..messages import M, set_reason
from ..models import AlbumStatus, Artist, LibraryFile, QualityProfile, Release, ReleaseGroup, of_artist
from ..quality import is_audio, is_better_or_equal, worst_format

if TYPE_CHECKING:
    from ..core import Core

ACTIVE = {
    AlbumStatus.SEARCHING,
    AlbumStatus.DOWNLOADING,
    AlbumStatus.IMPORTING,
    AlbumStatus.NEEDS_REVIEW,
    AlbumStatus.PENDING_APPROVAL,
}


class LibraryService:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._lock = asyncio.Lock()
        self.scanning = False
        self._tasks: set[asyncio.Task] = set()

    async def scan(self, confirm_empty: bool = False) -> dict:
        """`confirm_empty`: the person confirmed the library folder really is empty now
        (otherwise an empty folder that used to hold files is taken for an unmounted disk)."""
        async with self._lock:
            guard = self.core.guard
            if not await guard.check():
                emptied = guard.problem is not None and guard.problem.key == "library.guard.emptied"
                if not (confirm_empty and emptied):
                    # Never forget the library because its folder is missing or not mounted
                    await self.core.journal.emit(
                        "library.scan_skipped", M("library.scan_skipped", problem=guard.problem), level="warning"
                    )
                    return {"skipped": True}
            self.scanning = True
            try:
                stats = await self._scan()
            finally:
                self.scanning = False
        if confirm_empty:
            await self.core.journal.emit(
                "library.guard.confirmed_empty", M("library.guard.confirmed_empty", n=stats["removed"]), level="warning"
            )
            await guard.check()
        return stats

    async def _scan(self) -> dict:
        root = str(self.core.env.music_dir)
        on_disk: dict[str, tuple[int, float]] = {}
        # Lyrics files next to the tracks, by folder and name: (dir, stem) -> extensions
        sidecars: dict[tuple[str, str], set[str]] = defaultdict(set)

        def walk() -> None:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    stem, ext = os.path.splitext(fn)
                    if ext[1:].lower() in LYRICS_EXTENSIONS:
                        sidecars[(dirpath, stem)].add(ext[1:].lower())
                    elif is_audio(fn):
                        p = os.path.join(dirpath, fn)
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        on_disk[p] = (st.st_size, st.st_mtime)

        await asyncio.to_thread(walk)

        async with self.core.db.session() as s:
            known = {f.path: f for f in (await s.execute(select(LibraryFile))).scalars()}
            removed = [p for p in known if p not in on_disk]
            if removed:
                await s.execute(delete(LibraryFile).where(LibraryFile.path.in_(removed)))
            # Files from before lyrics were tracked (lyrics_embedded unknown) are read once more
            changed = [
                p for p, (_, mt) in on_disk.items()
                if p not in known or known[p].mtime != mt or known[p].lyrics_embedded is None
            ]
            for p in changed:
                meta = await asyncio.to_thread(read_meta, p)
                row = known.get(p) or LibraryFile(path=p)
                row.size, row.mtime = on_disk[p]
                row.format = meta.format
                row.bitrate = meta.bitrate
                row.duration = meta.duration
                row.tag_artist = meta.albumartist or meta.artist
                row.tag_album = meta.album
                row.tag_title = meta.title
                row.release_group_mbid = meta.release_group_mbid
                row.release_mbid = meta.release_mbid
                row.recording_mbid = meta.recording_mbid
                row.lyrics_embedded = meta.lyrics or ""  # "": read, none
                row.scanned_at = datetime.now(UTC)
                s.add(row)
                known[p] = row
            # Checked on every scan: a .lrc added or removed doesn't touch its track
            for p in on_disk:
                stem = os.path.splitext(os.path.basename(p))[0]
                sidecar = best_sidecar(sidecars.get((os.path.dirname(p), stem), set()))
                if known[p].lyrics_file != sidecar:
                    known[p].lyrics_file = sidecar
            await s.commit()

        await self.refresh_all()
        # Albums tagged with MusicBrainz but unknown to the catalog: added (not followed), in the background
        task = asyncio.create_task(self.core.catalog.from_library())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        stats = {"files": len(on_disk), "new_or_changed": len(changed), "removed": len(removed)}
        await self.core.journal.emit(
            "library.scanned",
            M("library.scanned", n=len(on_disk), changed=len(changed), removed=len(removed)),
            data=stats,
        )
        return stats

    async def refresh_all(self) -> None:
        async with self.core.db.session() as s:
            ids = list((await s.execute(select(Artist.id))).scalars())
        for aid in ids:
            await self.refresh_artist(aid)

    async def refresh_artist(self, artist_id: int) -> None:
        """Met à jour le statut des albums de l'artiste selon ce qui est présent dans /music."""
        async with self.core.db.session() as s:
            artist = await s.get(Artist, artist_id)
            if not artist:
                return
            profile = await s.get(QualityProfile, artist.quality_profile_id) if artist.quality_profile_id else None
            rgs = list((await s.execute(select(ReleaseGroup).where(of_artist(artist_id)))).scalars())
            if not rgs:
                return
            mbids = [rg.mbid for rg in rgs]
            files = list(
                (await s.execute(select(LibraryFile).where(LibraryFile.release_group_mbid.in_(mbids)))).scalars()
            )
            # Files tagged only with the release id: map back to the known release group
            releases = list(
                (await s.execute(select(Release).where(Release.release_group_id.in_([rg.id for rg in rgs])))).scalars()
            )
            rel_map = {r.mbid: r.release_group_id for r in releases}
            rel_tracks = {r.mbid: r.track_count for r in releases}
            rel_by_id = {r.id: r for r in releases}
            if rel_map:
                extra = (
                    await s.execute(
                        select(LibraryFile).where(
                            LibraryFile.release_group_mbid.is_(None), LibraryFile.release_mbid.in_(list(rel_map))
                        )
                    )
                ).scalars()
                files.extend(extra)

            by_rg: dict[int, list[LibraryFile]] = {}
            mbid_to_id = {rg.mbid: rg.id for rg in rgs}
            for f in files:
                rid = mbid_to_id.get(f.release_group_mbid or "") or rel_map.get(f.release_mbid or "")
                if rid:
                    by_rg.setdefault(rid, []).append(f)

            for rg in rgs:
                fs = by_rg.get(rg.id, [])
                if not fs:
                    rg.present_tracks = rg.total_tracks = None
                    if rg.status == AlbumStatus.IMPORTED:
                        rg.status = AlbumStatus.WANTED if rg.monitored else AlbumStatus.IGNORED
                        set_reason(rg, "status_reason", M("reason.files_missing"))
                        rg.imported_format = None
                        rg.imported_path = None
                    continue
                fmt = worst_format([f.format for f in fs], [])
                rg.imported_format = fmt
                rg.imported_path = os.path.commonpath([os.path.dirname(f.path) for f in fs])
                # Partial album (e.g. a single track shelved by hand): tracks present vs its edition
                present = len({f.recording_mbid or f.path for f in fs})
                totals = [rel_tracks[f.release_mbid] for f in fs if f.release_mbid in rel_tracks]
                selected = rel_by_id.get(rg.selected_release_id) if rg.selected_release_id else None
                total = max(totals) if totals else (selected.track_count if selected else None)
                if selected and selected.track_count:
                    # The edition you chose sets the bar, whatever edition the files were tagged with
                    # (e.g. 11 tracks shelved, the 12-track edition retained: one is missing)
                    total = selected.track_count
                    wanted = {t.recording_mbid for t in selected.tracks}
                    if any(f.recording_mbid for f in fs):
                        present = len({f.recording_mbid for f in fs if f.recording_mbid in wanted})
                rg.present_tracks, rg.total_tracks = present, total
                tolerated = profile.max_missing_tracks if profile else 0
                partial = bool(total and present < total - tolerated)
                if rg.status in ACTIVE:
                    continue
                cutoff = profile.upgrade_until if profile else None
                if partial and rg.monitored:
                    # Followed album only partly here: the hunt completes it
                    rg.status = AlbumStatus.WANTED
                    set_reason(rg, "status_reason", M("reason.partial", n=present, total=total))
                elif rg.monitored and cutoff and not is_better_or_equal(fmt, cutoff):
                    rg.status = AlbumStatus.WANTED
                    set_reason(rg, "status_reason", M("reason.upgrading", format=fmt or M("word.unknown_format")))
                else:
                    rg.status = AlbumStatus.IMPORTED
                    set_reason(rg, "status_reason", None)
            await s.commit()

    async def unmatched_files(self, limit: int = 500) -> list[LibraryFile]:
        async with self.core.db.session() as s:
            res = await s.execute(
                select(LibraryFile)
                .where(LibraryFile.release_group_mbid.is_(None), LibraryFile.release_mbid.is_(None))
                .order_by(LibraryFile.path)
                .limit(limit)
            )
            return list(res.scalars())
