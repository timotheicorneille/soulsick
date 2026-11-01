"""Import : fichiers téléchargés → tags MusicBrainz → renommage → /music.

Règle d'or : jamais de déplacement silencieux. Si l'association fichiers ↔ pistes n'est pas
assez sûre, l'album passe en « à vérifier » avec un plan proposé, modifiable dans l'interface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import TYPE_CHECKING

from sqlalchemy import select

from .. import lyrics
from ..audiotags import TagSet, read_meta, write_tags
from ..matching import Assignment, FileRef, MatchResult, TrackRef, match_files
from ..messages import M, Msg, UserError, set_reason
from .libguard import HELD
from ..models import (
    AlbumStatus,
    Artist,
    Candidate,
    CandidateStatus,
    LibraryFile,
    QualityProfile,
    Release,
    ReleaseGroup,
    Track,
    Transfer,
    TransferState,
)
from ..naming import TrackNaming, render_path
from ..quality import ALL_FORMATS, is_audio, is_better_or_equal, worst_format

if TYPE_CHECKING:
    from ..core import Core

log = logging.getLogger(__name__)
MIN_CONFIDENCE = 0.75
_TERMINAL = {TransferState.COMPLETE, TransferState.FAILED, TransferState.ABORTED}


def pick_key(fmt: str | None, failed: bool, candidate_id: int, a: Assignment) -> tuple:
    """Which file to keep when several sources have the same track (the smallest wins): first the
    one that really is that song, then the best format, an active source, the most recent one."""
    order = [k for k, _ in ALL_FORMATS]
    fit = 0 if a.title_score >= 0.8 else 1 if a.title_score >= 0.5 else 2
    return (fit, order.index(fmt) if fmt in order else len(order), int(failed), -candidate_id, -a.score)


def destination(t: Track, artist: Artist, rg: ReleaseGroup, rel: Release, template: str, music: str, ext: str) -> str:
    year = (rel.date or rg.first_release_date or "")[:4]
    rel_path = render_path(
        template,
        TrackNaming(
            albumartist=artist.name,
            artist=t.artist_credit or artist.name,
            album=rg.title,
            year=year,
            title=t.title,
            track=t.position,
            disc=t.disc,
            disc_count=rel.disc_count,
            ext=ext,
            release_type=rg.primary_type or "",
        ),
    )
    return os.path.join(music, rel_path)


async def place_track(
    src: str, t: Track, artist: Artist, rg: ReleaseGroup, rel: Release, settings, music: str, cover: bytes | None,
    replaceable: set[str] | None = None,
) -> str:
    """Tags a file with its track's MusicBrainz info, then shelves it with the naming template.

    `replaceable`: files of this album already in the library, which may be overwritten
    (a partial album being completed, or a quality upgrade).
    """
    dest = destination(t, artist, rg, rel, settings.library.naming_template, music, src.rsplit(".", 1)[-1].lower())
    if settings.library.write_tags:
        tags = TagSet(
            title=t.title,
            artist=t.artist_credit or artist.name,
            album=rg.title,
            albumartist=artist.name,
            date=rel.date or rg.first_release_date or "",
            tracknumber=t.position,
            tracktotal=sum(1 for x in rel.tracks if x.disc == t.disc),
            discnumber=t.disc,
            disctotal=rel.disc_count,
            musicbrainz_trackid=t.recording_mbid,
            musicbrainz_releasetrackid=t.mbid,
            musicbrainz_albumid=rel.mbid,
            musicbrainz_releasegroupid=rg.mbid,
            musicbrainz_artistid=artist.mbid,
            musicbrainz_albumartistid=artist.mbid,
            releasetype=rg.primary_type or "",
            label=rel.label or "",
            barcode=rel.barcode or "",
        )
        await asyncio.to_thread(write_tags, src, tags, cover if settings.library.embed_cover else None)
    if os.path.realpath(src) != os.path.realpath(dest):
        if os.path.exists(dest):
            if replaceable and os.path.realpath(dest) in replaceable:
                os.remove(dest)
            else:
                raise UserError("error.destination_taken", path=dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        await asyncio.to_thread(shutil.move, src, dest)
        await asyncio.to_thread(lyrics.follow, src, dest)
    return dest


class ImportService:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._lock = asyncio.Lock()

    async def _context(self, candidate_id: int):
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c:
                return None
            rg = await s.get(ReleaseGroup, c.release_group_id)
            assert rg
            artist = await s.get(Artist, rg.artist_id)
            assert artist
            profile = await s.get(QualityProfile, artist.quality_profile_id) if artist.quality_profile_id else None
        releases = await self.core.catalog.releases_with_tracks(rg.id)
        return c, rg, artist, profile, releases

    async def import_candidate(self, candidate_id: int, automatic: bool = False) -> None:
        """A candidate's files arrived: try importing the album (all sources together).
        `automatic`: started by the engine, not by you; skipped if the album was just shelved
        (the end of a source and the album rule can both fire for the same last file)."""
        if not await self.core.guard.check():
            # Library folder missing or not mounted: the album waits, imported once it is back
            ctx = await self._context(candidate_id)
            if ctx:
                await self._status(ctx[1].id, AlbumStatus.NEEDS_REVIEW, M(HELD, problem=self.core.guard.problem))
            return
        async with self._lock:
            if automatic and await self._just_imported(candidate_id):
                return
            try:
                await self._import(candidate_id)
            except Exception as e:
                log.exception("import")
                ctx = await self._context(candidate_id)
                if ctx:
                    _, rg, *_ = ctx
                    await self._status(rg.id, AlbumStatus.NEEDS_REVIEW, M("reason.import_error", error=str(e)))
                    await self.core.journal.emit(
                        "import.error", M("import.error", title=rg.title, error=str(e)), level="error",
                        release_group_id=rg.id, candidate_id=candidate_id,
                    )

    async def _just_imported(self, candidate_id: int) -> bool:
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            rg = await s.get(ReleaseGroup, c.release_group_id) if c else None
            return bool(rg and rg.status == AlbumStatus.IMPORTED)

    async def import_if_ready(self, rg_id: int) -> bool:
        """The album rule: every track of the edition aimed at is received (from any peer, even a
        dropped one) or already shelved, and nothing is in flight any more → it is imported,
        whatever state its sources are in. True if the import was started."""
        async with self.core.db.session() as s:
            rg = await s.get(ReleaseGroup, rg_id)
            if not rg or rg.status != AlbumStatus.DOWNLOADING:
                return False
            transfers = list((await s.execute(select(Transfer).where(Transfer.release_group_id == rg_id))).scalars())
        if any(t.state not in _TERMINAL and is_audio(t.remote_path) for t in transfers):
            return False
        cov = await self.core.coverage.album(rg_id, offers=0)
        states = [t["state"] for t in cov["tracks"]]
        if not states or "received" not in states or any(x not in ("received", "in_library") for x in states):
            return False
        # Imported through the source that delivered last (the import gathers every source anyway)
        last = max(
            (t for t in transfers if t.candidate_id and t.state == TransferState.COMPLETE),
            key=lambda t: (t.completed_at is not None, t.completed_at, t.id),
            default=None,
        )
        if last is None:
            return False
        await self.import_candidate(last.candidate_id, automatic=True)
        return True

    async def _status(self, rg_id: int, status: str, reason: Msg | None) -> None:
        async with self.core.db.session() as s:
            rg = await s.get(ReleaseGroup, rg_id)
            if rg:
                rg.status = status
                set_reason(rg, "status_reason", reason)
                await s.commit()
        self.core.hub.publish("album", {"id": rg_id, "status": status, "status_reason": reason.text if reason else None})

    def _local_files(self, directory: str) -> list[str]:
        out = []
        for dirpath, _, filenames in os.walk(directory):
            for fn in filenames:
                if is_audio(fn) and not fn.endswith(".incomplete"):
                    out.append(os.path.join(dirpath, fn))
        return sorted(out)

    async def _sources(self, rg_id: int) -> tuple[list[tuple[Candidate, list[str]]], list[Candidate]]:
        """Sources d'un album : candidats actifs ou abandonnés ayant des fichiers reçus EN ENTIER.

        Retourne (sources avec leurs fichiers complets, candidats ayant encore des transferts en cours).
        Un fichier en échec (souvent partiel) n'est jamais utilisé.
        """
        async with self.core.db.session() as s:
            cands = list(
                (
                    await s.execute(
                        select(Candidate).where(
                            Candidate.release_group_id == rg_id,
                            Candidate.status.in_(
                                [CandidateStatus.CHOSEN, CandidateStatus.DOWNLOADED, CandidateStatus.FAILED]
                            ),
                        )
                    )
                ).scalars()
            )
            rows = list(
                (
                    await s.execute(
                        select(Transfer).where(Transfer.candidate_id.in_([c.id for c in cands]))
                    )
                ).scalars()
            ) if cands else []
        by_cand: dict[int, list[Transfer]] = {}
        for t in rows:
            by_cand.setdefault(t.candidate_id or 0, []).append(t)
        sources: list[tuple[Candidate, list[str]]] = []
        inflight: list[Candidate] = []
        for c in cands:
            ts = [t for t in by_cand.get(c.id, []) if is_audio(t.remote_path)]
            if c.status == CandidateStatus.CHOSEN and any(t.state not in _TERMINAL for t in ts):
                inflight.append(c)
            if ts:
                files = []
                for t in ts:
                    if t.state != TransferState.COMPLETE or not t.local_path or not os.path.isfile(t.local_path):
                        continue
                    files.append(await self._reclaim(c, t))
            else:
                files = self._local_files(c.download_dir or "")  # anciens téléchargements sans lignes
            if files:
                sources.append((c, sorted(files)))
        return sources, inflight

    async def _reclaim(self, c: Candidate, t: Transfer) -> str:
        """A file received from this source but stored elsewhere (an aioslsk re-queue used to put it
        in downloads/manual): moved back into the source's download folder, row updated.
        Only files recorded as received from this very source are concerned."""
        if not c.download_dir or not t.local_path:
            return t.local_path or ""
        home = os.path.realpath(c.download_dir)
        if os.path.realpath(t.local_path).startswith(home + os.sep):
            return t.local_path
        os.makedirs(home, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(t.local_path))
        dest, n = os.path.join(home, base + ext), 1
        while os.path.exists(dest):
            dest, n = os.path.join(home, f"{base} ({n}){ext}"), n + 1
        await asyncio.to_thread(shutil.move, t.local_path, dest)
        async with self.core.db.session() as s:
            row = await s.get(Transfer, t.id)
            if row:
                row.local_path = dest
                await s.commit()
        log.info("moved %s back into %s", t.local_path, home)
        t.local_path = dest
        return dest

    async def build_album_plan(self, rg_id: int) -> tuple[list[dict], dict]:
        """Plan d'import multi-sources : pour chaque piste MusicBrainz, le meilleur fichier reçu.

        Doublons (même piste chez deux pairs) : meilleur format d'abord, puis source active,
        puis la plus récente. Les fichiers non retenus sont listés, jamais importés.
        """
        async with self.core.db.session() as s:
            rg = await s.get(ReleaseGroup, rg_id)
            assert rg
            artist = await s.get(Artist, rg.artist_id)
            assert artist
        releases = await self.core.catalog.releases_with_tracks(rg_id)
        sources, _ = await self._sources(rg_id)
        # Tracks of this album already in the library (e.g. one shelved by hand): they count as present
        async with self.core.db.session() as s:
            shelved = set(
                (await s.execute(select(LibraryFile.recording_mbid).where(
                    LibraryFile.release_group_mbid == rg.mbid, LibraryFile.recording_mbid.is_not(None),
                ))).scalars()
            )
        meta: dict[str, object] = {}
        for _, files in sources:
            for f in files:
                meta[f] = await asyncio.to_thread(read_meta, f)

        preferred = next((c.matched_release_id for c, _ in sources if c.matched_release_id), None)
        ordered = sorted(releases, key=lambda r: r.id != (preferred or rg.selected_release_id))
        best = None
        for rel in ordered:
            tracks = [TrackRef(t.disc, t.position, t.title, (t.length_ms or 0) / 1000 or None) for t in rel.tracks]
            picks: dict[int, tuple[tuple, dict]] = {}
            all_files: list[tuple[Candidate, str]] = []
            unmatched_files: list[str] = []
            for c, files in sources:
                refs = [
                    FileRef(path=f, duration_s=meta[f].duration, tag_title=meta[f].title,
                            tag_track=meta[f].track, tag_disc=meta[f].disc)
                    for f in files
                ]
                m = match_files(tracks, refs, artist_hint=artist.name)
                for i in m.unmatched_files:
                    unmatched_files.append(files[i])
                for a in m.assignments:
                    f = files[a.file_index]
                    key = pick_key(meta[f].format, c.status == CandidateStatus.FAILED, c.id, a)
                    entry = {"file": f, "candidate": c, "assignment": a}
                    if a.track_index not in picks or key < picks[a.track_index][0]:
                        picks[a.track_index] = (key, entry)
                all_files.extend((c, f) for f in files)
            in_library = [i for i, t in enumerate(rel.tracks) if i not in picks and t.recording_mbid in shelved]
            result = MatchResult()
            result.assignments = [e["assignment"] for _, e in picks.values()]
            result.unmatched_tracks = [i for i in range(len(tracks)) if i not in picks and i not in in_library]
            result.title_score = (sum(a.title_score for a in result.assignments) + len(in_library)) / max(1, len(tracks))
            result.count_ratio = (len(picks) + len(in_library)) / max(1, len(tracks))
            deltas = [a.duration_delta for a in result.assignments if a.duration_delta is not None]
            if deltas:
                result.duration_score = sum(1 for d in deltas if d <= 3.0) / len(tracks)
            if best is None or result.confidence > best[0].confidence + 0.02:
                best = (result, rel, picks, all_files, unmatched_files, in_library)
        if best is None:
            return [], {"confidence": 0, "release_id": None, "unmatched_tracks": [], "unmatched_files": [], "sources": 0}
        result, rel, picks, all_files, unmatched_files, in_library = best
        chosen = {e["file"]: (ti, e) for ti, (_, e) in picks.items()}
        plan = []
        for c, f in all_files:
            m = meta[f]
            hit = chosen.get(f)
            t = rel.tracks[hit[0]] if hit else None
            plan.append(
                {
                    "file": f,
                    "name": os.path.basename(f),
                    "format": m.format,
                    "duration": m.duration,
                    "source": c.username,
                    "candidate_id": c.id,
                    "track_id": t.id if t else None,
                    "track_title": t.title if t else None,
                    "disc": t.disc if t else None,
                    "position": t.position if t else None,
                    "score": hit[1]["assignment"].score if hit else None,
                    "duplicate": hit is None and f not in unmatched_files,
                }
            )
        info = {
            "confidence": round(result.confidence, 3),
            "release_id": rel.id,
            "release_title": rel.title,
            "unmatched_tracks": [rel.tracks[i].title for i in result.unmatched_tracks],
            "in_library": [rel.tracks[i].title for i in in_library],
            "unmatched_files": [os.path.basename(f) for f in unmatched_files],
            "sources": len(sources),
            "per_source": {c.username: sum(1 for _, e in picks.values() if e["candidate"].id == c.id) for c, _ in sources},
        }
        return plan, info

    async def build_plan(self, candidate_id: int) -> tuple[list[dict], dict] | None:
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c:
                return None
            rg_id = c.release_group_id
        return await self.build_album_plan(rg_id)

    async def _import(self, candidate_id: int) -> None:
        ctx = await self._context(candidate_id)
        if not ctx:
            return
        c, rg, artist, profile, releases = ctx
        label = f"{artist.name} – {rg.title}"
        sources, inflight = await self._sources(rg.id)
        if inflight:
            # Another source is still downloading: import once everything has arrived
            names = ", ".join(x.username for x in inflight)
            await self._status(rg.id, AlbumStatus.DOWNLOADING, M("reason.waiting_files", peer=c.username, names=names))
            return

        await self._status(rg.id, AlbumStatus.IMPORTING, None)
        plan, info = await self.build_album_plan(rg.id)
        missing_ok = profile.max_missing_tracks if profile else 0
        missing = info.get("unmatched_tracks", [])

        async with self.core.db.session() as s:
            row = await s.get(Candidate, candidate_id)
            assert row
            row.import_plan = plan
            row.matched_release_id = info["release_id"]
            await s.commit()

        if len(missing) > missing_ok:
            received = sum(1 for p in plan if p["track_id"])
            total = received + len(missing)
            reason = M(
                "reason.incomplete", received=received, total=total,
                missing=", ".join(missing[:4]) + ("…" if len(missing) > 4 else ""),
            )
            await self._status(rg.id, AlbumStatus.PENDING_APPROVAL, M("reason.incomplete_action", reason=reason))
            await self.core.journal.emit(
                "import.incomplete",
                M("import.incomplete", label=label, reason=reason),
                level="warning", artist_id=artist.id, release_group_id=rg.id, candidate_id=candidate_id, data=info,
            )
            return

        problems: list[Msg] = []
        if info["confidence"] < MIN_CONFIDENCE:
            problems.append(M("reason.low_confidence", score=round(info["confidence"] * 100), min=round(MIN_CONFIDENCE * 100)))
        if info.get("unmatched_files") and info.get("sources", 1) <= 1:
            problems.append(M("reason.unmatched_files", n=len(info["unmatched_files"])))
        if problems:
            joined: Msg = problems[0] if len(problems) == 1 else M("join", a=problems[0], b=problems[1])
            reason = M("reason.uncertain", problems=joined)
            await self._status(rg.id, AlbumStatus.NEEDS_REVIEW, reason)
            await self.core.journal.emit(
                "import.needs_review",
                M("import.needs_review", label=label, reason=reason),
                level="warning", artist_id=artist.id, release_group_id=rg.id, candidate_id=candidate_id,
                data=info,
            )
            return
        await self.apply_plan(candidate_id, plan, info["release_id"])

    async def apply_plan(self, candidate_id: int, plan: list[dict], release_id: int, by_user: bool = False) -> None:
        self.core.guard.require()
        ctx = await self._context(candidate_id)
        if not ctx:
            return
        c, rg, artist, profile, releases = ctx
        rel = next((r for r in releases if r.id == release_id), None)
        if rel is None:
            raise UserError("error.edition_not_found")
        tracks = {t.id: t for t in rel.tracks}
        # The plan may come from the UI: only files from the album's download folders are accepted
        async with self.core.db.session() as s:
            album_cands = list(
                (await s.execute(select(Candidate).where(Candidate.release_group_id == rg.id))).scalars()
            )
        roots = [os.path.realpath(x.download_dir) for x in album_cands if x.download_dir]
        for item in plan:
            real = os.path.realpath(item.get("file", ""))
            if not any(real.startswith(r + os.sep) for r in roots):
                raise UserError("error.file_outside_downloads", path=item.get("file"))
        used = [i for i in plan if tracks.get(i.get("track_id"))]
        if len({i["track_id"] for i in used}) != len(used):
            raise UserError("error.track_mapped_twice")
        settings = await self.core.settings.get()
        music = str(self.core.env.music_dir)

        # Cover: always written as cover.jpg (Navidrome), embedded in files if asked
        p = await self.core.mb.cover("release", rel.mbid, 1200) or await self.core.mb.cover(
            "release-group", rg.mbid, 1200
        )
        cover = p.read_bytes() if p else None

        # Replacing a previous version (quality upgrade)
        async with self.core.db.session() as s:
            old = list(
                (await s.execute(select(LibraryFile).where(LibraryFile.release_group_mbid == rg.mbid))).scalars()
            )
        replaceable = {os.path.realpath(f.path) for f in old}
        moved: list[dict] = []
        new_paths: list[str] = []
        for item in plan:
            t = tracks.get(item.get("track_id"))
            if t is None:
                continue
            src = item["file"]
            dest = await place_track(src, t, artist, rg, rel, settings, music, cover, replaceable)
            moved.append({"from": src, "to": dest, "track": t.title, "source": item.get("source")})
            new_paths.append(dest)

        album_dir = os.path.commonpath([os.path.dirname(p) for p in new_paths]) if new_paths else None
        if album_dir and cover:
            with open(os.path.join(album_dir, "cover.jpg"), "wb") as fh:
                fh.write(cover)

        # Older files of this album go only when the plan brings a new version of the same track;
        # tracks the plan doesn't cover (already shelved, e.g. by hand) stay
        placed = {tracks[i["track_id"]].recording_mbid: m["to"] for i, m in zip(used, moved, strict=True)}
        removed_old = []
        kept = []
        for f in old:
            if f.path in new_paths or not os.path.exists(f.path):
                continue
            if f.recording_mbid in placed:
                lyrics.drop_replaced(f.path, placed[f.recording_mbid])  # its lyrics stay, on the new file
                removed_old.append(f.path)
            else:
                kept.append(f.path)

        # Downloads cleaned: unused files (duplicates, dropped sources) removed
        leftovers = 0
        for x in album_cands:
            if x.download_dir and os.path.isdir(x.download_dir):
                leftovers += sum(len(f) for _, _, f in os.walk(x.download_dir))
                shutil.rmtree(x.download_dir, ignore_errors=True)

        # Index files BEFORE announcing the album as shelved (otherwise: shelved with 0 files)
        await self.core.library.scan()
        self.core.lyrics.after_import(rg.mbid)  # lyrics of the tracks that came without

        metas = [read_meta(p) for p in [*new_paths, *kept]]
        fmt = worst_format([m.format for m in metas], [])
        cutoff = profile.upgrade_until if profile else None
        upgrade = bool(cutoff and not is_better_or_equal(fmt, cutoff))
        contributors = {i.get("candidate_id") or candidate_id for i in used}
        per_source: dict[str, int] = {}
        for i in used:
            per_source[i.get("source") or c.username] = per_source.get(i.get("source") or c.username, 0) + 1
        async with self.core.db.session() as s:
            for cid in contributors:
                row = await s.get(Candidate, cid)
                if row:
                    row.status = CandidateStatus.IMPORTED
                    set_reason(row, "reject_reason", None)
            g = await s.get(ReleaseGroup, rg.id)
            assert g
            g.selected_release_id = rel.id
            g.imported_format = fmt
            g.imported_path = album_dir
            g.status = AlbumStatus.WANTED if upgrade else AlbumStatus.IMPORTED
            set_reason(g, "status_reason", M("reason.imported_upgrading", format=fmt) if upgrade else None)
            await s.commit()

        await self.core.journal.emit(
            "import.done",
            M(
                "import.done", artist=artist.name, title=rg.title, n=len(moved), dir=album_dir,
                details=[
                    *([M("import.detail.tags_cover" if cover else "import.detail.tags")] if settings.library.write_tags else []),
                    *([M("import.detail.sources", sources=", ".join(f"{u} ({n})" for u, n in per_source.items()))]
                      if len(per_source) > 1 else []),
                    *([M("import.detail.replaced", n=len(removed_old))] if removed_old else []),
                    *([M("import.detail.dropped", n=leftovers - len(moved))] if leftovers > len(moved) else []),
                    *([M("import.detail.by_you")] if by_user else []),
                ],
            ),
            artist_id=artist.id, release_group_id=rg.id, candidate_id=candidate_id,
            data={"moves": moved, "replaced": removed_old, "format": fmt, "release": rel.title, "sources": per_source},
        )
        self.core.hub.publish("album", {"id": rg.id, "status": g.status})
        await self.core.navidrome.trigger_scan()
