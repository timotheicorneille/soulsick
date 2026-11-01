"""Hunting: wanted albums → Soulseek searches → scored candidates → decision → download."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select

from ..lyrics import DOWNLOADED_LYRICS
from ..matching import TrackRef
from ..messages import M, Msg, UserError, set_reason
from ..models import (
    AlbumStatus,
    Artist,
    ArtistMode,
    Candidate,
    CandidateStatus,
    LibraryFile,
    QualityProfile,
    ReleaseGroup,
    SearchRun,
    Transfer,
    TransferState,
)
from ..quality import FORMAT_LABELS, extension_of, is_better_or_equal
from ..scoring import PeerResult, ReleaseOption, ScoredCandidate, rank_candidates, unique_tracklists
from ..slsk.engine import EngineUnavailable
from .coverage import ACTIVE

if TYPE_CHECKING:
    from ..core import Core

log = logging.getLogger(__name__)

TERMINAL = {TransferState.COMPLETE, TransferState.FAILED, TransferState.ABORTED}
STALL_MINUTES = 30
RETRY_HOURS = 12


def clean_query(text: str) -> str:
    text = re.sub(r"[\[\(].*?[\]\)]", " ", text)
    text = re.sub(r"[^\w\s']", " ", text)
    text = text.replace("'", "")
    return re.sub(r"\s+", " ", text).strip()


def build_queries(artist: str, album: str) -> list[str]:
    a, b = clean_query(artist), clean_query(album)
    queries = [f"{a} {b}"]
    if a.lower().startswith("the "):
        queries.append(f"{a[4:]} {b}")
    out: list[str] = []
    for q in queries:
        if q and q.lower() not in [x.lower() for x in out]:
            out.append(q)
    return out


def summarize_rejections(cands: list[ScoredCandidate]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for x in cands:
        if x.reject_reason:
            # Group by kind of reason
            c[x.reject_reason.key if isinstance(x.reject_reason, Msg) else str(x.reject_reason)] += 1
    return dict(c.most_common(6))


class HunterService:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._lock = asyncio.Lock()
        # Automatic retries of a file refused by the peer: 3 attempts, growing delays
        self.max_attempts = 3
        self.retry_delays: list[float] = [30, 120]
        self._retrying: set[int] = set()
        self.current: dict | None = None
        self.last_run_at: datetime | None = None
        self.next_run_at: datetime | None = None

    # --- Loop -------------------------------------------------------------

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("hunter loop")
            await asyncio.sleep(60)

    async def tick(self) -> None:
        await self.check_stalled()
        await self.core.transfers.check_stuck()
        cfg = (await self.core.settings.get()).hunter
        now = datetime.now(UTC)
        if not cfg.enabled:
            self.next_run_at = None
            return
        if self.last_run_at and now - self.last_run_at < timedelta(minutes=cfg.interval_minutes):
            self.next_run_at = self.last_run_at + timedelta(minutes=cfg.interval_minutes)
            return
        self.last_run_at = now
        self.next_run_at = now + timedelta(minutes=cfg.interval_minutes)
        if not self.core.engine.logged_in:
            return
        await self.run_wanted(cfg.max_searches_per_run)

    async def run_wanted(self, limit: int, force: bool = False) -> int:
        """Hunts wanted albums; `force` ignores the delay between two unsuccessful attempts."""
        cutoff = datetime.now(UTC) - (timedelta(0) if force else timedelta(hours=RETRY_HOURS))
        async with self.core.db.session() as s:
            res = await s.execute(
                select(ReleaseGroup.id)
                .join(Artist, Artist.id == ReleaseGroup.artist_id)
                .where(
                    ReleaseGroup.status == AlbumStatus.WANTED,
                    ReleaseGroup.monitored.is_(True),
                    Artist.monitored.is_(True),
                    or_(ReleaseGroup.last_searched_at.is_(None), ReleaseGroup.last_searched_at < cutoff),
                )
                .order_by(ReleaseGroup.last_searched_at.is_not(None), ReleaseGroup.last_searched_at)
                .limit(limit)
            )
            ids = list(res.scalars())
        for rg_id in ids:
            await self.hunt(rg_id, trigger="auto")
        return len(ids)

    # --- Hunting one album -------------------------------------------------

    async def _load(self, rg_id: int) -> tuple[ReleaseGroup, Artist, QualityProfile] | None:
        async with self.core.db.session() as s:
            rg = await s.get(ReleaseGroup, rg_id)
            if not rg:
                return None
            artist = await s.get(Artist, rg.artist_id)
            assert artist
            profile = None
            if artist.quality_profile_id:
                profile = await s.get(QualityProfile, artist.quality_profile_id)
            if profile is None:
                profile = (await s.execute(select(QualityProfile).order_by(QualityProfile.id))).scalars().first()
            assert profile
            return rg, artist, profile

    async def _set_status(self, rg_id: int, status: str, reason: Msg | None = None, searched: bool = False) -> None:
        async with self.core.db.session() as s:
            rg = await s.get(ReleaseGroup, rg_id)
            if not rg:
                return
            rg.status = status
            set_reason(rg, "status_reason", reason)
            if searched:
                rg.last_searched_at = datetime.now(UTC)
            await s.commit()
        self.core.hub.publish("album", {"id": rg_id, "status": status, "status_reason": reason.text if reason else None})

    async def hunt(self, rg_id: int, trigger: str = "manual", replace: bool = False) -> None:
        """`replace`: you asked for this album again — any valid version will do (not only a
        better format), and the best one starts right away, whatever the artist's mode. The
        import then replaces the shelved files."""
        async with self._lock:
            try:
                await self._hunt(rg_id, trigger, replace)
            finally:
                self.current = None
                self.core.hub.publish("hunter", self.state())

    async def _hunt(self, rg_id: int, trigger: str, replace: bool = False) -> None:
        loaded = await self._load(rg_id)
        if not loaded:
            return
        rg, artist, profile = loaded
        previous_status = rg.status
        label = f"{artist.name} – {rg.title}"
        cfg = (await self.core.settings.get()).hunter
        self.current = {"release_group_id": rg_id, "label": label, "step": "tracklist"}
        self.core.hub.publish("hunter", self.state())

        try:
            releases = await self.core.catalog.releases_with_tracks(rg_id)
        except Exception as e:
            await self.core.journal.emit(
                "hunt.error", M("hunt.error", label=label, error=str(e)),
                level="error", artist_id=artist.id, release_group_id=rg_id,
            )
            return
        if not releases:
            await self._set_status(rg_id, previous_status, M("reason.no_edition"), searched=True)
            return

        await self._set_status(rg_id, AlbumStatus.SEARCHING)
        queries = build_queries(artist.name, rg.title)
        await self.core.journal.emit(
            "hunt.started",
            M(
                "hunt.started", label=label, trigger=M("word.manual" if trigger == "manual" else "word.automatic"),
                profile=profile.name, queries=", ".join(f"“{q}”" for q in queries),
            ),
            artist_id=artist.id, release_group_id=rg_id,
            data={"queries": queries, "profile": profile.name, "formats": profile.formats, "trigger": trigger},
        )

        all_results: list[PeerResult] = []
        run_ids: list[int] = []
        options = await self._options(rg, releases)
        for q in queries:
            self.current = {"release_group_id": rg_id, "label": label, "step": "search", "query": q}
            self.core.hub.publish("hunter", self.state())
            try:
                results = await self.core.engine.search(q, cfg.search_wait_seconds)
            except EngineUnavailable as e:
                await self._set_status(rg_id, AlbumStatus.WANTED, M("reason.soulseek_unavailable", error=e.msg))
                await self.core.journal.emit(
                    "hunt.skipped", M("hunt.skipped", label=label, error=e.msg),
                    level="warning", artist_id=artist.id, release_group_id=rg_id,
                )
                return
            files = sum(len(r.files) for r in results)
            async with self.core.db.session() as s:
                run = SearchRun(
                    release_group_id=rg_id, query=q, kind=trigger, result_count=files, peer_count=len(results)
                )
                s.add(run)
                await s.commit()
                run_ids.append(run.id)
            await self.core.journal.emit(
                "search.done",
                M("search.done", query=q, files=files, peers=len(results)),
                artist_id=artist.id, release_group_id=rg_id, data={"query": q, "files": files, "peers": len(results)},
            )
            all_results.extend(results)
        scored = await self._rank(all_results, options, rg, profile, cfg, artist.name, label, replace)

        valid = [c for c in scored if not c.rejected]
        keep = valid[:30] + [c for c in scored if c.rejected][:30]
        async with self.core.db.session() as s:
            # Unused older proposals are replaced by the new ones
            old = await s.execute(
                select(Candidate).where(
                    Candidate.release_group_id == rg_id,
                    Candidate.status.in_([CandidateStatus.PROPOSED, CandidateStatus.REJECTED]),
                )
            )
            for o in old.scalars():
                await s.delete(o)
            rows = []
            for c in keep:
                row = Candidate(
                    release_group_id=rg_id,
                    search_run_id=run_ids[-1] if run_ids else None,
                    username=c.username,
                    directory=c.directory,
                    files=[f.to_dict() for f in c.audio_files] + [f.to_dict() | {"extra": True} for f in c.extra_files],
                    format=c.format,
                    score=c.score,
                    score_detail=c.detail,
                    status=CandidateStatus.REJECTED if c.rejected else CandidateStatus.PROPOSED,
                    reject_reason=c.reject_reason.text if c.reject_reason else None,
                    reject_reason_t=c.reject_reason.to_json() if c.reject_reason else None,
                    matched_release_id=c.release_id,
                    has_free_slots=c.has_free_slots,
                    queue_size=c.queue_size,
                    avg_speed=c.avg_speed,
                )
                s.add(row)
                rows.append(row)
            await s.commit()
            valid_ids = [r.id for r in rows if r.status == CandidateStatus.PROPOSED]

        rejections = summarize_rejections(scored)
        if not valid:
            reason = M("reason.no_valid", n=len(scored))
            await self._set_status(rg_id, AlbumStatus.WANTED, reason, searched=True)
            await self.core.journal.emit(
                "hunt.no_candidate",
                M("hunt.no_candidate", label=label, reason=reason, hours=RETRY_HOURS),
                level="warning", artist_id=artist.id, release_group_id=rg_id,
                data={"folders": len(scored), "rejections": rejections},
            )
            return

        best = valid[0]
        await self.core.journal.emit(
            "hunt.scored",
            M(
                "hunt.scored", label=label, n=len(valid), total=len(scored), peer=best.username, dir=best.directory,
                score=best.score, format=FORMAT_LABELS.get(best.format or "", best.format),
                matched=best.detail.get("match", {}).get("tracks_matched", "?"),
                expected=best.detail.get("match", {}).get("tracks_expected", "?"),
            ),
            artist_id=artist.id, release_group_id=rg_id, candidate_id=valid_ids[0],
            data={
                "top": [
                    {"username": c.username, "directory": c.directory, "score": c.score, "format": c.format}
                    for c in valid[:5]
                ],
                "rejections": rejections,
            },
        )

        if artist.mode == ArtistMode.AUTO or replace:
            await self._set_status(rg_id, AlbumStatus.SEARCHING, None, searched=True)
            await self.start_download(valid_ids[0], by="auto")
        else:
            n = min(cfg.proposals, len(valid))
            await self._set_status(
                rg_id, AlbumStatus.PENDING_APPROVAL, M("reason.awaiting", n=n), searched=True
            )
            await self.core.journal.emit(
                "hunt.awaiting_approval",
                M("hunt.awaiting_approval", label=label, n=n),
                artist_id=artist.id, release_group_id=rg_id,
            )

    async def _options(self, rg: ReleaseGroup, releases: list) -> list[ReleaseOption]:
        async with self.core.db.session() as s:
            # Tracks already shelved (e.g. by hand): a folder only needs to bring the others
            shelved = set((await s.execute(select(LibraryFile.recording_mbid).where(
                LibraryFile.release_group_mbid == rg.mbid, LibraryFile.recording_mbid.is_not(None),
            ))).scalars())
        options = [
            ReleaseOption(
                r.id,
                f"{r.title} ({r.date or '?'} · {r.country or '?'} · {r.formats or '?'} · {r.track_count} tr.)",
                [TrackRef(t.disc, t.position, t.title, (t.length_ms or 0) / 1000 or None) for t in r.tracks],
                frozenset(i for i, t in enumerate(r.tracks) if t.recording_mbid in shelved),
            )
            for r in releases
            if r.tracks
        ]
        # The selected edition goes first: on equal score it wins
        options.sort(key=lambda o: o.release_id != rg.selected_release_id)
        return options

    async def _rank(
        self, results: list[PeerResult], options: list[ReleaseOption], rg: ReleaseGroup, profile, cfg,
        artist_name: str, label: str, replace: bool = False,
    ) -> list[ScoredCandidate]:
        if not results:
            return []
        self.current = {"release_group_id": rg.id, "label": label, "step": "scoring"}
        # Heavy work (hundreds of folders): off the event loop so the app stays responsive
        t0 = time.perf_counter()
        scored = await asyncio.to_thread(
            rank_candidates,
            results, options, profile.formats, profile.max_missing_tracks, cfg.min_match, artist_name,
        )
        log.info(
            "%s: %d folders scored in %.1f s (%d MusicBrainz editions, %d distinct tracklists)",
            label, len(scored), time.perf_counter() - t0, len(options), len(unique_tracklists(options)),
        )
        # When upgrading, only a strictly better format is useful
        partial = bool(rg.total_tracks and (rg.present_tracks or 0) < rg.total_tracks)
        # A partial album gets completed: any valid folder will do, not only a better format
        if rg.imported_format and not partial and not replace:
            for c in scored:
                if not c.rejected and is_better_or_equal(rg.imported_format, c.format):
                    c.reject_reason = M(
                        "reject.not_better", format=FORMAT_LABELS.get(rg.imported_format, rg.imported_format)
                    )
            scored.sort(key=lambda c: (c.rejected, -c.score))
        return scored

    # --- Download ---------------------------------------------------------

    async def start_download(self, candidate_id: int, by: str = "user") -> None:
        cfg = (await self.core.settings.get()).hunter
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c:
                return
            rg = await s.get(ReleaseGroup, c.release_group_id)
            assert rg
            artist = await s.get(Artist, rg.artist_id)
            assert artist
            dest = os.path.join(str(self.core.env.downloads_dir), f"album-{rg.id}", f"candidate-{c.id}")
            # The same source already failed for this album (a later hunt picks it again): its
            # folder is taken over and only the files it lacks are asked for. Otherwise each hunt
            # downloaded the whole album again, in a new folder, until the disk was full.
            received: dict[str, Transfer] = {}
            prev = (await s.execute(
                select(Candidate).where(
                    Candidate.release_group_id == rg.id, Candidate.id != c.id,
                    Candidate.username == c.username, Candidate.directory == c.directory,
                    Candidate.status == CandidateStatus.FAILED, Candidate.download_dir.is_not(None),
                ).order_by(Candidate.id.desc())
            )).scalars().first()
            if prev and prev.download_dir and os.path.isdir(prev.download_dir):
                dest, prev.download_dir = prev.download_dir, None
                prev_rows = await s.execute(select(Transfer).where(Transfer.candidate_id == prev.id))
                for t in prev_rows.scalars():
                    if t.state == TransferState.COMPLETE and t.local_path and os.path.isfile(t.local_path):
                        received[t.remote_path] = t
            c.status = CandidateStatus.CHOSEN
            c.download_dir = dest
            # Lyrics always (small, and Navidrome reads them); covers and the rest only if asked.
            # Lyrics are asked first: the album is imported as soon as its audio is there, and
            # peers send in the order asked
            is_lyrics = [extension_of(f["filename"]) in DOWNLOADED_LYRICS for f in c.files]
            files = [
                f for f, lyr in sorted(zip(c.files, is_lyrics, strict=True), key=lambda x: not x[1])
                if lyr or not f.get("extra") or cfg.download_covers
            ]
            transfers = []
            for f in files:
                done = received.get(f["filename"])
                if done:
                    # One row per file: the received one moves to this candidate
                    done.candidate_id = c.id
                    transfers.append(done)
                    continue
                t = Transfer(
                    direction="download",
                    username=c.username,
                    remote_path=f["filename"],
                    local_path=os.path.join(dest, f["filename"].replace("\\", "/").rsplit("/", 1)[-1]),
                    size=f.get("size"),
                    state=TransferState.QUEUED,
                    attempts=1,
                    candidate_id=c.id,
                    release_group_id=rg.id,
                )
                s.add(t)
                transfers.append(t)
            rg.status = AlbumStatus.DOWNLOADING
            set_reason(rg, "status_reason", M("reason.from_peer", peer=c.username))
            await s.commit()
            label = f"{artist.name} – {rg.title}"
            username, directory, rg_id, artist_id = c.username, c.directory, rg.id, artist.id
            # Already being downloaded for another album (same file, same source): it is copied
            # here when it arrives, not asked for a second time
            twins = {
                t.remote_path for t in transfers
                if t.state != TransferState.COMPLETE
                and self.core.transfers.register("download", t.username, t.remote_path, t.id)
            }
            paths = [f["filename"] for f in files if f["filename"] not in received.keys() | twins]

        self.core.hub.publish("album", {"id": rg_id, "status": AlbumStatus.DOWNLOADING})
        await self.core.journal.emit(
            "download.started",
            M(
                "download.started", label=label, n=len(paths), peer=username, dir=directory, dest=dest,
                by=M("word.automatically" if by == "auto" else "word.by_you"),
            ),
            artist_id=artist_id, release_group_id=rg_id, candidate_id=candidate_id,
            data={"username": username, "directory": directory, "destination": dest, "files": paths, "by": by},
        )
        if not paths:
            # Every file was already there: straight to the import
            await self.on_transfer_terminal(candidate_id)
            return
        try:
            for p in paths:
                await self.core.engine.download(username, p, dest)
        except EngineUnavailable as e:
            await self.core.journal.emit(
                "download.error", M("download.error", label=label, error=e.msg), level="error",
                artist_id=artist_id, release_group_id=rg_id, candidate_id=candidate_id,
            )
            await self._fail_candidate(candidate_id, e.msg, try_next=False)

    async def on_transfer_terminal(self, candidate_id: int) -> None:
        """Re-evaluates a candidate when one of its transfers reaches a final state.

        - refused file: automatic retry (growing delays), on the same row;
        - attempts exhausted: what arrived stays, the refused tracks are asked to other peers
          (nothing arrived at all: the album goes to the next candidate, as before);
        - cancelled files don't fail the source: it is done with the rest;
        - candidate already dropped but every file arrived after all: import anyway.
        """
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c or c.status not in (CandidateStatus.CHOSEN, CandidateStatus.FAILED):
                return
            status = c.status
            ts = list((await s.execute(select(Transfer).where(Transfer.candidate_id == candidate_id))).scalars())
        audio = [t for t in ts if not _is_extra(t.remote_path)]
        if not audio or any(t.state not in TERMINAL for t in audio):
            return
        # Cancelled files (by you, or a track taken from another peer) are not a failure of
        # the source; only files the peer refused are
        refused = [t for t in audio if t.state == TransferState.FAILED]
        cancelled = [t for t in audio if t.state == TransferState.ABORTED]

        if status == CandidateStatus.FAILED:
            if refused or cancelled:
                return  # dropped, with what it brought: the album rule takes it from there
        elif refused:
            retryable = [t for t in refused if (t.attempts or 1) < self.max_attempts]
            pending = [t for t in retryable if t.id not in self._retrying]
            if retryable:
                if pending:
                    attempt = max(t.attempts or 1 for t in pending)
                    delay = self.retry_delays[min(attempt - 1, len(self.retry_delays) - 1)]
                    for t in pending:
                        self._retrying.add(t.id)
                    names = ", ".join(t.remote_path.replace("\\", "/").rsplit("/", 1)[-1] for t in pending[:3])
                    await self.core.journal.emit(
                        "download.retry_scheduled",
                        M(
                            "download.retry_scheduled", n=len(pending), peer=pending[0].username,
                            names=names + ("…" if len(pending) > 3 else ""), attempt=attempt + 1,
                            max=self.max_attempts, delay=_human_delay(delay),
                        ),
                        release_group_id=c.release_group_id, candidate_id=candidate_id,
                        data={"transfers": [t.id for t in pending], "delay_s": delay, "attempt": attempt + 1},
                    )
                    asyncio.create_task(self._retry_later([t.id for t in pending], delay))
                return
            reasons = Counter(t.error or t.state for t in refused)
            reason = M(
                "reason.refused_after", n=len(refused), max=self.max_attempts,
                reasons=", ".join(f"{k} ×{v}" for k, v in reasons.items()),
            )
            if not any(t.state == TransferState.COMPLETE for t in audio):
                # Nothing came from this peer: the album goes to the next one, as always
                await self._fail_candidate(candidate_id, reason, abandon_pending=False)
                return
            # What arrived stays; the refused tracks are asked to other peers that have them
            await self._mark_failed(candidate_id, reason)
            relayed = await self._relay_refused(c.release_group_id, candidate_id)
            if not relayed:
                # Nobody else has them: the import says what is missing (« à compléter »)
                asyncio.create_task(self.core.importer.import_candidate(candidate_id, automatic=True))
            return

        # Extra files still in flight are dropped
        for t in ts:
            if t.state not in TERMINAL:
                await self.core.engine.abort(t.username, t.remote_path, "download")
        async with self.core.db.session() as s:
            row = await s.get(Candidate, candidate_id)
            assert row
            row.status = CandidateStatus.DOWNLOADED
            set_reason(row, "reject_reason", None)
            await s.commit()
        await self.core.journal.emit(
            "download.complete",
            M("download.complete_late" if status == CandidateStatus.FAILED else "download.complete", peer=c.username, n=len(audio)),
            release_group_id=c.release_group_id, candidate_id=candidate_id,
        )
        asyncio.create_task(self.core.importer.import_candidate(candidate_id, automatic=True))

    async def take_track(self, rg_id: int, track_id: int, candidate_id: int) -> None:
        """« Prendre chez… »: this track of the album, from this peer, now. Where it is being
        fetched (in progress, queued, paused) is cancelled first; nothing changes if the peer
        doesn't have it. A track already received stays as it is."""
        cover = await self.core.coverage.album(rg_id, offers=None)
        track = next((t for t in cover["tracks"] if t["track_id"] == track_id), None)
        if track is None:
            raise UserError("error.no_tracks_for_peer")
        if track["state"] in ("received", "in_library"):
            raise UserError("error.track_already_received")
        if not any(h["candidate_id"] == candidate_id for h in track["holders"]):
            raise UserError("error.no_tracks_for_peer")
        async with self.core.db.session() as s:
            to = await s.get(Candidate, candidate_id)
            assert to
            reason = M("reason.taken_elsewhere", peer=to.username).text
            fetching = [x["transfer_id"] for x in track["sources"] if x["state"] in ACTIVE]
            rows = list((await s.execute(select(Transfer).where(Transfer.id.in_(fetching))))) if fetching else []
            olds = set()
            for (t,) in rows:
                t.state, t.error, t.completed_at = TransferState.ABORTED, reason, datetime.now(UTC)
                if t.candidate_id:
                    olds.add(t.candidate_id)
            await s.commit()
            targets = [(t.id, t.username, t.remote_path) for (t,) in rows]
        for tid, username, path in targets:
            self.core.transfers.forget(tid)
            await self.core.transfers.stop_on_engine(username, path)
            self.core.hub.publish("transfer", {"id": tid, "state": TransferState.ABORTED, "speed": 0, "stuck": False})
        await self.complete_from(candidate_id, [track_id])
        # The previous sources may have nothing left in flight now: they are settled
        for cid in olds:
            asyncio.create_task(self.on_transfer_terminal(cid))

    async def _mark_failed(self, candidate_id: int, reason: Msg) -> None:
        """The source is done, with failures: it stays a source for what it brought."""
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if c:
                c.status = CandidateStatus.FAILED
                set_reason(c, "reject_reason", reason)
                await s.commit()

    async def _relay_refused(self, rg_id: int, from_id: int) -> list[int]:
        """The album's missing tracks, asked to the other peers that have them: best first (same
        format, then whole coverage, free slot, score), never a peer where that very file already
        failed or was cancelled. Returns the tracks relayed."""
        cover = await self.core.coverage.album(rg_id, offers=None)
        want = {t["track_id"] for t in cover["tracks"] if t["state"] == "missing"}
        if not want:
            return []
        async with self.core.db.session() as s:
            src = await s.get(Candidate, from_id)
            fmt = src.format if src else None
            dead = {
                (cid, path) for cid, path in await s.execute(
                    select(Transfer.candidate_id, Transfer.remote_path).where(
                        Transfer.release_group_id == rg_id,
                        Transfer.state.in_([TransferState.FAILED, TransferState.ABORTED]),
                    )
                )
            }
        fillers = [f for f in cover["fillers"] if f["status"] != CandidateStatus.REJECTED]
        fillers.sort(key=lambda f: f["format"] != fmt)  # stable: keeps coverage's order otherwise
        relayed: list[int] = []
        for f in fillers:
            tids = [
                x["track_id"] for x in f["tracks"]
                if x["track_id"] in want and (f["candidate_id"], x["filename"]) not in dead
            ]
            if not tids:
                continue
            await self.complete_from(f["candidate_id"], tids)
            await self.core.journal.emit(
                "download.relayed",
                M("download.relayed", n=len(tids), peer=f["username"]),
                release_group_id=rg_id, candidate_id=f["candidate_id"],
            )
            want -= set(tids)
            relayed += tids
            if not want:
                break
        return relayed

    async def _retry_later(self, transfer_ids: list[int], delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            for tid in transfer_ids:
                async with self.core.db.session() as s:
                    t = await s.get(Transfer, tid)
                    # The peer may have restarted it meanwhile: only re-request what still failed
                    still_failed = bool(t and t.state == TransferState.FAILED)
                if still_failed:
                    try:
                        await self.core.transfers.requeue(tid)
                    except EngineUnavailable as e:
                        log.info("retry impossible: %s", e)
        finally:
            for tid in transfer_ids:
                self._retrying.discard(tid)

    async def complete_from(self, candidate_id: int, track_ids: list[int]) -> int:
        """Mixing: download ONLY the requested tracks from this candidate.

        Tracks already received from other peers are kept; the import picks, track by track,
        the best file available.
        """
        cover = await self.core.coverage.album(await self._rg_of(candidate_id), offers=None)
        wanted = set(track_ids)
        # A peer can bring missing tracks, and better versions of tracks already shelved
        offers = [f for f in cover["fillers"] + cover["upgraders"] if f["candidate_id"] == candidate_id]
        files = list({x["track_id"]: x for f in offers for x in f["tracks"] if x["track_id"] in wanted}.values())
        if not files:
            raise UserError("error.no_tracks_for_peer")
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            assert c
            rg = await s.get(ReleaseGroup, c.release_group_id)
            assert rg
            artist = await s.get(Artist, rg.artist_id)
            assert artist
            dest = os.path.join(str(self.core.env.downloads_dir), f"album-{rg.id}", f"candidate-{c.id}")
            c.status = CandidateStatus.CHOSEN
            c.download_dir = dest
            existing = {
                t.remote_path: t
                for t in (await s.execute(select(Transfer).where(Transfer.candidate_id == c.id))).scalars()
            }
            sizes = {f["filename"]: f.get("size") for f in c.files}
            # A peer taken back (after a switch): its files cancelled back then, and not asked
            # again (another peer brought them), are no longer its. Left attached, they would
            # fail it once this round is done, and the album would never be shelved
            asked = {x["filename"] for x in files}
            for path, t in list(existing.items()):
                if path not in asked and t.state in TERMINAL and t.state != TransferState.COMPLETE:
                    t.candidate_id = None
                    del existing[path]
            rows = []
            for x in files:
                if x["filename"] in existing:
                    rows.append(existing[x["filename"]])
                    continue
                t = Transfer(
                    direction="download", username=c.username, remote_path=x["filename"],
                    local_path=os.path.join(dest, x["filename"].replace("\\", "/").rsplit("/", 1)[-1]),
                    size=sizes.get(x["filename"]), state=TransferState.QUEUED, attempts=1,
                    candidate_id=c.id, release_group_id=rg.id,
                )
                s.add(t)
                rows.append(t)
            rg.status = AlbumStatus.DOWNLOADING
            set_reason(rg, "status_reason", M("reason.complement_from", peer=c.username))
            await s.commit()
            label, username, rg_id, fmt = f"{artist.name} – {rg.title}", c.username, rg.id, c.format
            new_ids = [t.id for t in rows if t.remote_path not in existing]
            again = [t.id for t in rows if t.remote_path in existing and t.state != TransferState.COMPLETE]
            for t in rows:
                self.core.transfers.register("download", t.username, t.remote_path, t.id)
        received_fmts = sorted({x["format"] for x in cover["tracks"] for x in x["sources"] if x["state"] == "complete" and x.get("format")})
        mixed = bool(received_fmts and fmt and fmt not in received_fmts)
        self.core.hub.publish("album", {"id": rg_id, "status": AlbumStatus.DOWNLOADING})
        await self.core.journal.emit(
            "download.complement",
            M(
                "download.complement_mixed" if mixed else "download.complement", label=label, n=len(files),
                peer=username, titles=", ".join(x["title"] for x in files[:4]) + ("…" if len(files) > 4 else ""),
                received=", ".join(received_fmts), format=fmt,
            ),
            level="warning" if mixed else "info", release_group_id=rg_id, candidate_id=candidate_id,
            data={"tracks": [x["title"] for x in files], "files": [x["filename"] for x in files], "mixed_formats": mixed},
        )
        for tid in new_ids:
            async with self.core.db.session() as s:
                t = await s.get(Transfer, tid)
                assert t
                path, dest_dir = t.remote_path, os.path.dirname(t.local_path or dest)
            await self.core.engine.download(username, path, dest_dir)
        for tid in again:
            await self.core.transfers.requeue(tid, reset_attempts=True)
        return len(files)

    async def _rg_of(self, candidate_id: int) -> int:
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c:
                raise UserError("error.candidate_not_found")
            return c.release_group_id

    async def reactivate(self, candidate_id: int, why: Msg) -> None:
        """A file of a dropped candidate restarts: the candidate and the album become active again."""
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c or c.status != CandidateStatus.FAILED:
                return
            c.status = CandidateStatus.CHOSEN
            set_reason(c, "reject_reason", None)
            rg = await s.get(ReleaseGroup, c.release_group_id)
            assert rg
            rg.status = AlbumStatus.DOWNLOADING
            set_reason(rg, "status_reason", M("reason.resume_from", peer=c.username))
            await s.commit()
            rg_id, username = rg.id, c.username
        self.core.hub.publish("album", {"id": rg_id, "status": AlbumStatus.DOWNLOADING})
        await self.core.journal.emit(
            "download.reactivated", M("download.reactivated", peer=username, why=why),
            release_group_id=rg_id, candidate_id=candidate_id,
        )

    async def resume_candidate(self, candidate_id: int) -> int:
        """Resumes a candidate: only files not received are requested again, on their existing rows."""
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c:
                return 0
            ts = list((await s.execute(select(Transfer).where(Transfer.candidate_id == candidate_id))).scalars())
            missing = [t for t in ts if t.state != TransferState.COMPLETE]
            c.status = CandidateStatus.CHOSEN
            set_reason(c, "reject_reason", None)
            rg = await s.get(ReleaseGroup, c.release_group_id)
            assert rg
            rg.status = AlbumStatus.DOWNLOADING
            set_reason(rg, "status_reason", M("reason.resume_from", peer=c.username))
            await s.commit()
            rg_id, username, done = rg.id, c.username, len(ts) - len(missing)
        self.core.hub.publish("album", {"id": rg_id, "status": AlbumStatus.DOWNLOADING})
        await self.core.journal.emit(
            "download.resumed_candidate",
            M("download.resumed_candidate", peer=username, n=len(missing), done=done),
            release_group_id=rg_id, candidate_id=candidate_id,
        )
        for t in missing:
            if t.state in (TransferState.QUEUED, TransferState.INITIALIZING, TransferState.TRANSFERRING):
                continue
            await self.core.transfers.requeue(t.id, reset_attempts=True)
        if not missing:
            await self.on_transfer_terminal(candidate_id)
        return len(missing)

    async def abandon(self, candidate_id: int) -> int:
        """You drop a source: its unfinished files are cancelled, what arrived is kept,
        and the album waits for your choice (another candidate, or a new search)."""
        dropped = await self._drop_source(candidate_id, M("reason.abandoned"))
        if dropped is None:
            return 0
        await self.core.journal.emit(
            "download.abandoned", M("download.abandoned", label=dropped["label"], peer=dropped["username"], n=len(dropped["ids"])),
            level="warning", release_group_id=dropped["rg_id"], candidate_id=candidate_id,
        )
        if not dropped["still_active"]:
            # Nothing else downloading for this album: back to you
            others = dropped["others"]
            await self._set_status(
                dropped["rg_id"],
                AlbumStatus.PENDING_APPROVAL if others else AlbumStatus.WANTED,
                M("reason.abandoned_pick") if others else M("reason.abandoned"),
            )
        return len(dropped["ids"])

    async def redownload(self, rg_id: int) -> int:
        """You want this album again: what is downloading is dropped, Soulseek is searched and its
        best version starts. The shelved files are replaced only once the new version is
        imported: if nobody has the album, nothing is deleted."""
        async with self.core.db.session() as s:
            rg = await s.get(ReleaseGroup, rg_id)
            if not rg:
                raise UserError("error.album_not_found")
            artist = await s.get(Artist, rg.artist_id)
            assert artist
            active = list((await s.execute(select(Candidate.id).where(
                Candidate.release_group_id == rg_id, Candidate.status == CandidateStatus.CHOSEN,
            ))).scalars())
            label, artist_id = f"{artist.name} – {rg.title}", artist.id
        name = "Soulseek"
        for cid in active:
            await self._drop_source(cid, M("reason.redownload", source=name))
        await self.core.journal.emit(
            "album.redownload", M("album.redownload", label=label, source=name, n=len(active)),
            artist_id=artist_id, release_group_id=rg_id, data={"dropped": active},
        )
        asyncio.create_task(self.hunt(rg_id, "manual", replace=True))
        return len(active)

    async def _drop_source(self, candidate_id: int, reason: Msg) -> dict | None:
        """Cancels the unfinished files of a source (what arrived is kept) and marks it failed.
        The album status is left to the caller."""
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c or c.status not in (CandidateStatus.CHOSEN, CandidateStatus.FAILED):
                return None
            rg = await s.get(ReleaseGroup, c.release_group_id)
            artist = await s.get(Artist, rg.artist_id) if rg else None
            assert rg and artist
            pending = list((await s.execute(select(Transfer).where(
                Transfer.candidate_id == candidate_id, Transfer.state.not_in(list(TERMINAL)),
            ))).scalars())
            for t in pending:
                # Paused or never answered: the engine may not even track them any more
                t.state = TransferState.ABORTED
                t.error = M("reason.cancelled_by_you", n=1).text
                t.completed_at = datetime.now(UTC)
            c.status = CandidateStatus.FAILED
            set_reason(c, "reject_reason", reason)
            others = (await s.execute(select(func.count()).where(
                Candidate.release_group_id == rg.id, Candidate.status == CandidateStatus.PROPOSED,
            ))).scalar_one()
            still_active = (await s.execute(select(func.count()).where(
                Candidate.release_group_id == rg.id, Candidate.status == CandidateStatus.CHOSEN,
            ))).scalar_one()
            await s.commit()
            out = {
                "rg_id": rg.id, "label": f"{artist.name} – {rg.title}", "username": c.username,
                "ids": [t.id for t in pending], "others": others, "still_active": still_active,
            }
            targets = [(t.username, t.remote_path) for t in pending]
        for tid in out["ids"]:
            self.core.transfers.forget(tid)
        for username_, path in targets:
            await self.core.transfers.stop_on_engine(username_, path)
        for tid in out["ids"]:
            self.core.hub.publish("transfer", {"id": tid, "state": TransferState.ABORTED, "speed": 0, "stuck": False})
        return out

    async def switch_peer(self, candidate_id: int, to: int | None = None, search: bool = False) -> int:
        """Switches a downloading source to another peer: what arrived is kept, the tracks not received
        yet are asked to `to` (or a new search is started). Returns the number of tracks asked."""
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c:
                raise UserError("error.candidate_not_found")
            if c.status != CandidateStatus.CHOSEN:
                raise UserError("error.source_not_active")
        track_ids: list[int] = []
        new_peer = None
        if not search:
            alts = await self.core.coverage.alternatives(candidate_id)
            target = next((a for a in alts["alternatives"] if a["candidate_id"] == to), None)
            if target is None:
                raise UserError("error.no_tracks_for_peer")
            track_ids, new_peer = target["track_ids"], target["username"]
        reason = M("reason.switched", peer=new_peer) if new_peer else M("reason.switched_search", peer=c.username)
        dropped = await self._drop_source(candidate_id, reason)
        if dropped is None:
            raise UserError("error.source_not_active")
        if search:
            await self.core.journal.emit(
                "download.switched_search", M("download.switched_search", label=dropped["label"], peer=dropped["username"]),
                release_group_id=dropped["rg_id"], candidate_id=candidate_id,
            )
            if not dropped["still_active"]:
                await self._set_status(dropped["rg_id"], AlbumStatus.WANTED, reason)
            asyncio.create_task(self.hunt(dropped["rg_id"], "manual"))
            return 0
        assert to is not None
        await self.core.journal.emit(
            "download.switched",
            M("download.switched", label=dropped["label"], old=dropped["username"], new=new_peer, n=len(track_ids)),
            release_group_id=dropped["rg_id"], candidate_id=to,
        )
        return await self.complete_from(to, track_ids)

    async def _fail_candidate(
        self, candidate_id: int, reason: Msg, try_next: bool = True, abandon_pending: bool = True,
    ) -> None:
        """`abandon_pending` : its files still queued are marked aborted (nothing will download
        them). Not when every file already reached an end: what is queued then was retried since."""
        async with self.core.db.session() as s:
            c = await s.get(Candidate, candidate_id)
            if not c:
                return
            c.status = CandidateStatus.FAILED
            set_reason(c, "reject_reason", reason)
            rg = await s.get(ReleaseGroup, c.release_group_id)
            assert rg
            artist = await s.get(Artist, rg.artist_id)
            assert artist
            pending = list(
                (await s.execute(select(Transfer).where(
                    Transfer.candidate_id == candidate_id, Transfer.state.not_in(list(TERMINAL))
                ))).scalars()
            )
            # Its unfinished files go with it: left "queued", nothing would ever download them,
            # and they still counted as active
            if abandon_pending:
                for t in pending:
                    t.state = TransferState.ABORTED
                    t.error = M("transfer.source_failed").text
                    t.completed_at = datetime.now(UTC)
            targets = [(t.id, t.username, t.remote_path) for t in pending]
            nxt = (
                await s.execute(
                    select(Candidate)
                    .where(Candidate.release_group_id == rg.id, Candidate.status == CandidateStatus.PROPOSED)
                    .order_by(Candidate.score.desc())
                )
            ).scalars().first()
            await s.commit()
            rg_id, label, mode = rg.id, f"{artist.name} – {rg.title}", artist.mode
            next_id = nxt.id if nxt else None
            next_label = f"{nxt.username} ({nxt.score}/100)" if nxt else None

        for tid, username, path in targets:
            if not abandon_pending:
                await self.core.engine.abort(username, path, "download")
                continue
            self.core.transfers.forget(tid)
            # Tolerates an engine that is down: often why the source failed in the first place
            await self.core.transfers.stop_on_engine(username, path)
            self.core.hub.publish(
                "transfer", {"id": tid, "state": TransferState.ABORTED, "speed": 0, "stuck": False}
            )

        await self.core.journal.emit(
            "download.failed", M("download.failed", label=label, reason=reason), level="warning",
            release_group_id=rg_id, candidate_id=candidate_id,
        )
        if try_next and next_id and mode == ArtistMode.AUTO:
            await self.core.journal.emit(
                "hunt.fallback", M("hunt.fallback", label=label, next=next_label),
                release_group_id=rg_id, candidate_id=next_id,
            )
            await self.start_download(next_id, by="auto")
        elif try_next and next_id:
            await self._set_status(rg_id, AlbumStatus.PENDING_APPROVAL, M("reason.chosen_failed"))
        else:
            await self._set_status(rg_id, AlbumStatus.WANTED, M("reason.last_failed", reason=reason))

    async def check_stalled(self) -> None:
        """Drops a candidate when none of its files started within STALL_MINUTES and the peer
        never accepted any of them (offline, unreachable, or ignoring us)."""
        limit = datetime.now(UTC) - timedelta(minutes=STALL_MINUTES)
        async with self.core.db.session() as s:
            chosen = list(
                (await s.execute(select(Candidate).where(Candidate.status == CandidateStatus.CHOSEN))).scalars()
            )
            stalled = []
            for c in chosen:
                ts = list((await s.execute(select(Transfer).where(Transfer.candidate_id == c.id))).scalars())
                if not ts:
                    continue
                if any(t.state == TransferState.PAUSED for t in ts):
                    continue  # paused by you: not an absent peer
                if any(t.remotely_queued and t.state not in TERMINAL for t in ts):
                    continue  # the peer accepted: we are waiting in its upload queue, not stalled
                started = any(t.started_at is not None or t.bytes_transferred > 0 for t in ts)
                created = min(t.created_at for t in ts)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if not started and created < limit:
                    stalled.append(c.id)
        for cid in stalled:
            await self._fail_candidate(cid, M("reason.stalled", minutes=STALL_MINUTES))
        # Self-healing: a candidate whose transfers all ended but that was never re-evaluated
        # (restart during a retry delay, for instance) is picked up here.
        for c in chosen:
            if c.id not in stalled:
                await self.on_transfer_terminal(c.id)

    def state(self) -> dict:
        return {
            "current": self.current,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
        }


def _human_delay(seconds: float) -> str:
    return f"{round(seconds)} s" if seconds < 90 else f"{round(seconds / 60)} min"


def _is_extra(path: str) -> bool:
    from ..quality import is_audio

    return not is_audio(path)
