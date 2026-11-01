"""An album, track by track: what was received and from where, what is missing, what is below the
rest of the album in quality, and which peers can complete or upgrade it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ..matching import FileRef, TrackRef, has_title, match_files
from ..models import Artist, Candidate, CandidateStatus, LibraryFile, Release, ReleaseGroup, Transfer
from ..quality import ALL_FORMATS, is_audio, is_better_or_equal

if TYPE_CHECKING:
    from ..core import Core

ACTIVE = ("queued", "initializing", "transferring", "paused")
SOURCE_STATUSES = {CandidateStatus.CHOSEN, CandidateStatus.DOWNLOADED, CandidateStatus.FAILED}
# Sources to complete or upgrade from (a dropped source can still deliver a single track)
FILLER_STATUSES = {CandidateStatus.PROPOSED, CandidateStatus.REJECTED, CandidateStatus.FAILED}
_RANK = {"complete": 0, "transferring": 1, "initializing": 2, "queued": 3, "paused": 4, "failed": 5, "aborted": 6}


def _track_map(c: Candidate, release: Release, artist: str) -> dict[int, dict]:
    """Associe les fichiers distants d'un candidat aux pistes de l'édition (par nom + durée annoncée)."""
    audio = [f for f in c.files if not f.get("extra") and is_audio(f["filename"])]
    tracks = [TrackRef(t.disc, t.position, t.title, (t.length_ms or 0) / 1000 or None) for t in release.tracks]
    m = match_files(tracks, [FileRef(f["filename"], f.get("duration")) for f in audio], artist_hint=artist)
    out = {}
    for a in m.assignments:
        # The number alone only counts for a file without a title: "06-deviantart.flac" from another
        # album of the same peer is not track 06 "A7"
        if a.title_score < 0.5 and not (a.number_match and not has_title(audio[a.file_index]["filename"])):
            continue
        t = release.tracks[a.track_index]
        out[t.id] = audio[a.file_index]
    return out


class CoverageService:
    def __init__(self, core: Core) -> None:
        self.core = core

    async def album(self, rg_id: int, offers: int | None = 8) -> dict:
        releases = await self.core.catalog.releases_with_tracks(rg_id)
        async with self.core.db.session() as s:
            rg = await s.get(ReleaseGroup, rg_id)
            assert rg
            artist = await s.get(Artist, rg.artist_id)
            assert artist
            cands = list((await s.execute(select(Candidate).where(Candidate.release_group_id == rg_id))).scalars())
            transfers = list(
                (await s.execute(select(Transfer).where(Transfer.release_group_id == rg_id))).scalars()
            )
            shelved = (
                await s.execute(
                    select(LibraryFile.recording_mbid, LibraryFile.format).where(LibraryFile.release_group_mbid == rg.mbid)
                )
            ).all()
        in_library = {r for r, _ in shelved}
        library_format = {r: f for r, f in shelved}
        order = [f for f, _ in ALL_FORMATS]
        # The best format of the album as shelved: tracks below it stand out (e.g. an MP3 among FLACs)
        album_best = min((f for _, f in shelved if f in order), key=order.index, default=None)
        sources = [c for c in cands if c.status in SOURCE_STATUSES]
        preferred = next((c.matched_release_id for c in sorted(sources, key=lambda c: -c.id) if c.matched_release_id), None)
        release = next((r for r in releases if r.id == (preferred or rg.selected_release_id)), releases[0] if releases else None)
        if release is None:
            return {"release_id": None, "tracks": [], "summary": {}, "fillers": []}

        by_path = {(t.candidate_id, t.remote_path): t for t in transfers}
        per_track: dict[int, list[dict]] = {t.id: [] for t in release.tracks}
        for c in sources:
            for track_id, f in _track_map(c, release, artist.name).items():
                t = by_path.get((c.id, f["filename"]))
                if t is None:
                    continue  # fichier du candidat jamais demandé (complément partiel)
                live = self.core.transfers.live(t.id) or {}
                per_track[track_id].append(
                    {
                        "candidate_id": c.id,
                        "username": c.username,
                        "transfer_id": t.id,
                        "state": live.get("state", t.state),
                        "format": f.get("format"),
                        "filename": f["filename"],
                        "bytes": live.get("bytes_transferred", t.bytes_transferred),
                        "size": t.size,
                    }
                )

        tracks = []
        counts = {"received": 0, "downloading": 0, "paused": 0, "missing": 0, "in_library": 0}
        for t in release.tracks:
            srcs = sorted(per_track[t.id], key=lambda x: _RANK.get(x["state"], 9))
            ok = [x for x in srcs if x["state"] not in ("failed", "aborted")]
            if any(x["state"] == "complete" for x in srcs):
                state = "received"
            elif any(x["state"] in ("queued", "initializing", "transferring") for x in srcs):
                state = "downloading"
            elif any(x["state"] == "paused" for x in srcs):
                state = "paused"
            elif t.recording_mbid in in_library:
                state = "in_library"
            else:
                state = "missing"
            if state in ("received", "in_library"):
                # La piste est obtenue : les tentatives ratées ou annulées ailleurs ne sont plus pertinentes
                srcs = ok
            counts[state] += 1
            lib_fmt = library_format.get(t.recording_mbid) if state == "in_library" else None
            tracks.append(
                {
                    "track_id": t.id, "disc": t.disc, "position": t.position, "title": t.title,
                    "length_ms": t.length_ms, "state": state, "sources": srcs,
                    "library_format": lib_fmt,
                    "upgrade": bool(lib_fmt and album_best and not is_better_or_equal(lib_fmt, album_best)),
                }
            )

        if offers != 0:
            # Who else has each track, to take it from them (« Prendre chez… »): any peer of this
            # album but the one already bringing it
            holders: dict[int, list[dict]] = {t["track_id"]: [] for t in tracks}
            providers = {
                (t["track_id"], x["candidate_id"]) for t in tracks for x in t["sources"]
                if x["state"] == "complete" or x["state"] in ACTIVE
            }
            for c in sorted(cands, key=lambda c: -c.score)[:60]:
                for tid, f in _track_map(c, release, artist.name).items():
                    if (tid, c.id) not in providers:
                        holders[tid].append({
                            "candidate_id": c.id, "username": c.username, "status": c.status,
                            "format": f.get("format") or c.format, "filename": f["filename"],
                            "has_free_slots": c.has_free_slots, "queue_size": c.queue_size, "avg_speed": c.avg_speed,
                        })
            for t in tracks:
                t["holders"] = sorted(holders[t["track_id"]], key=lambda h: not h["has_free_slots"])[:6]

        missing_ids = {t["track_id"] for t in tracks if t["state"] == "missing"}
        # Tracks to upgrade, with the format they have now: only better files are offered
        upgrade_fmt = {t["track_id"]: t["library_format"] for t in tracks if t["upgrade"]}
        fillers, upgraders = [], []
        if missing_ids or upgrade_fmt:
            titles = {x.id: x.title for x in release.tracks}
            for c in sorted((c for c in cands if c.status in FILLER_STATUSES), key=lambda c: -c.score)[:60]:
                m = _track_map(c, release, artist.name)
                info = {
                    "candidate_id": c.id, "username": c.username, "directory": c.directory,
                    "format": c.format, "score": c.score, "status": c.status,
                    "has_free_slots": c.has_free_slots, "queue_size": c.queue_size, "avg_speed": c.avg_speed,
                }

                def entry(tid: int, f: dict) -> dict:
                    return {"track_id": tid, "title": titles[tid], "filename": f["filename"], "format": f.get("format")}

                got = [entry(tid, f) for tid, f in m.items() if tid in missing_ids]
                if got:
                    fillers.append(info | {"tracks": sorted(got, key=lambda x: x["track_id"])})
                better = [
                    entry(tid, f) | {"from_format": upgrade_fmt[tid]}
                    for tid, f in m.items()
                    if tid in upgrade_fmt and f.get("format") and not is_better_or_equal(upgrade_fmt[tid], f["format"])
                ]
                if better:
                    upgraders.append(info | {"tracks": sorted(better, key=lambda x: x["track_id"])})
            for lst in (fillers, upgraders):
                lst.sort(key=lambda f: (-len(f["tracks"]), not f["has_free_slots"], -f["score"]))
        received_formats = sorted(
            {x["format"] for t in tracks for x in t["sources"] if x["state"] == "complete" and x["format"]}
        )
        return {
            "release_id": release.id,
            "release_title": release.title,
            "tracks": tracks,
            "summary": {**counts, "total": len(tracks), "upgrade": len(upgrade_fmt)},
            "received_formats": received_formats,
            "fillers": fillers[:offers],
            "upgraders": upgraders[:offers],
        }

    async def alternatives(self, candidate_id: int) -> dict:
        """Other peers that have tracks this source has not delivered yet (to switch peer).

        A track counts when it is neither received nor shelved, and no OTHER source is fetching it.
        """
        async with self.core.db.session() as s:
            current = await s.get(Candidate, candidate_id)
            if not current:
                return {"release_group_id": None, "missing": 0, "alternatives": []}
            rg_id = current.release_group_id
        cov = await self.album(rg_id, offers=None)
        wanted = {
            t["track_id"] for t in cov["tracks"]
            if t["state"] not in ("received", "in_library")
            and not any(x["candidate_id"] != candidate_id and x["state"] in ACTIVE for x in t["sources"])
        }
        out: dict = {"release_group_id": rg_id, "missing": len(wanted), "alternatives": []}
        if not wanted:
            return out
        release = next(r for r in await self.core.catalog.releases_with_tracks(rg_id) if r.id == cov["release_id"])
        async with self.core.db.session() as s:
            rg = await s.get(ReleaseGroup, rg_id)
            assert rg
            artist = await s.get(Artist, rg.artist_id)
            assert artist
            cands = list((await s.execute(select(Candidate).where(
                Candidate.release_group_id == rg_id, Candidate.id != candidate_id,
                Candidate.status.in_(list(FILLER_STATUSES)),
            ))).scalars())
        alts = []
        for c in cands:
            got = sorted(tid for tid in _track_map(c, release, artist.name) if tid in wanted)
            if got:
                alts.append({
                    "candidate_id": c.id, "username": c.username, "format": c.format, "score": c.score,
                    "status": c.status, "has_free_slots": c.has_free_slots, "queue_size": c.queue_size,
                    "avg_speed": c.avg_speed, "tracks": len(got), "track_ids": got,
                })
        # Whole album first, then a free slot, then the best score
        alts.sort(key=lambda a: (-a["tracks"], not a["has_free_slots"], -a["score"]))
        out["alternatives"] = alts[:8]
        return out
