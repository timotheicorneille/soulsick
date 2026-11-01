"""Assemblage des services (un seul objet partagé par l'application)."""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update

from .auth import Auth
from .config import Env
from .db import Database
from .journal import Hub, Journal
from .legacy import convert_legacy_reasons
from .messages import M, Msg
from .models import AlbumStatus, Artist, Candidate, CandidateStatus, ReleaseGroup, Transfer, TransferState
from .musicbrainz import MusicBrainz
from .services.catalog import CatalogService
from .services.chat import ChatService
from .services.coverage import CoverageService
from .services.discover import DiscoverService
from .services.existing import ExistingImportService
from .services.hunter import HunterService
from .services.importer import ImportService
from .services.libguard import LibraryGuard
from .services.library import LibraryService
from .services.lyrics_fetch import LyricsFetchService
from .services.navidrome import NavidromeService
from .services.peers import PeerService
from .services.shares import ShareService
from .services.storage import StorageService
from .services.transfers import TransferService
from .settings_store import SettingsStore
from .slsk.engine import SlskEngine

log = logging.getLogger(__name__)


class Core:
    def __init__(self, env: Env, mb: MusicBrainz | None = None, engine: SlskEngine | None = None) -> None:
        self.env = env
        env.ensure_dirs()
        self.db = Database(f"sqlite+aiosqlite:///{env.db_path}")
        self.hub = Hub()
        self.journal = Journal(self.db, self.hub)
        self.settings = SettingsStore(self.db)
        self.auth = Auth(self.settings)
        self.mb = mb or MusicBrainz(env.covers_dir)
        self.engine = engine or SlskEngine(str(env.downloads_dir))
        self.catalog = CatalogService(self)
        self.library = LibraryService(self)
        self.lyrics = LyricsFetchService(self)
        self.hunter = HunterService(self)
        self.importer = ImportService(self)
        self.transfers = TransferService(self)
        self.shares = ShareService(self)
        self.navidrome = NavidromeService(self)
        self.coverage = CoverageService(self)
        self.existing = ExistingImportService(self)
        self.chat = ChatService(self)
        self.storage = StorageService(self)
        self.guard = LibraryGuard(self)
        self.peers = PeerService(self)
        self.discover = DiscoverService(self)
        self._tasks: list[asyncio.Task] = []

        self.engine.on_transfer = self.transfers.on_update
        self.engine.on_status = lambda st: self.hub.publish("status", st)
        self.engine.on_info = self._engine_info
        self.engine.on_chat = self.chat.on_message
        self.engine.on_room = self.chat.on_room
        self.engine.rooms_wanted = self.chat.joined_rooms

    def _share_salt(self) -> bytes:
        """Secret behind the shared folder aliases, created once and kept in the config folder."""
        path = self.env.config_dir / "share_alias_salt"
        if not path.is_file():
            path.write_bytes(os.urandom(32))
        return path.read_bytes()

    async def _engine_info(self, kind: str, message: Msg, data: dict) -> None:
        level = data.pop("level", "info")
        await self.journal.emit(kind, message, level=level, data=data)

    async def start(self) -> None:
        await self.db.create_all()
        async with self.db.session() as s:
            # Databases from before shared releases: each release is linked to its main artist
            await s.execute(text(
                "INSERT OR IGNORE INTO artist_release_group (artist_id, release_group_id) "
                "SELECT artist_id, id FROM release_group"
            ))
            await s.commit()
        await self.catalog.ensure_default_profiles()
        await convert_legacy_reasons(self.db)
        await self._drop_retired_source()
        await self._recover()
        await self.transfers.load_cancelled()
        await self.journal.emit("app.started", M("app.started"))
        await self.guard.check()
        self._tasks.append(asyncio.create_task(self.guard.loop()))
        if not self.env.disable_engine:
            await self.restart_engine()
            self._tasks.append(asyncio.create_task(self.hunter.loop()))
            self._tasks.append(asyncio.create_task(self._sync_loop()))
            self._tasks.append(asyncio.create_task(self._queue_loop()))
            self._tasks.append(asyncio.create_task(self._ready_loop()))
            self._tasks.append(asyncio.create_task(self.discover.loop()))
            if self.env.forwarded_port_file:
                self._tasks.append(asyncio.create_task(self._port_loop()))
        self._tasks.append(asyncio.create_task(self.library.scan()))

    async def _recover(self) -> None:
        """After a restart, steps in progress can't resume as they were."""
        async with self.db.session() as s:
            await s.execute(
                update(ReleaseGroup)
                .where(ReleaseGroup.status == AlbumStatus.SEARCHING)
                .values(
                    status=AlbumStatus.WANTED,
                    status_reason=M("reason.restart_interrupted").text,
                    status_reason_t=M("reason.restart_interrupted").to_json(),
                )
            )
            # An import cut short: the album stayed "importing", which the library never re-examines.
            # Back to wanted: the library puts it back as imported if its files were shelved, and
            # files still in the downloads are imported again.
            interrupted = list((await s.execute(
                select(ReleaseGroup.id).where(ReleaseGroup.status == AlbumStatus.IMPORTING)
            )).scalars())
            reimport: list[int] = []
            if interrupted:
                await s.execute(
                    update(ReleaseGroup)
                    .where(ReleaseGroup.id.in_(interrupted))
                    .values(
                        status=AlbumStatus.WANTED,
                        status_reason=M("reason.import_interrupted").text,
                        status_reason_t=M("reason.import_interrupted").to_json(),
                    )
                )
                reimport = [
                    c.id for c in (await s.execute(select(Candidate).where(
                        Candidate.release_group_id.in_(interrupted),
                        Candidate.status == CandidateStatus.DOWNLOADED,
                    ))).scalars()
                    if c.download_dir and os.path.isdir(c.download_dir) and os.listdir(c.download_dir)
                ]
            await s.execute(
                update(Transfer)
                .where(Transfer.direction == "upload", Transfer.state.not_in(["complete", "failed", "aborted"]))
                .values(state=TransferState.ABORTED, error=M("transfer.restart_interrupted").text)
            )
            # Downloads left "queued" by a source that failed (older versions): nothing would ever
            # download them, and they still counted as active
            await s.execute(
                update(Transfer)
                .where(
                    Transfer.direction == "download",
                    Transfer.state.not_in(["complete", "failed", "aborted"]),
                    Transfer.candidate_id.in_(
                        select(Candidate.id).where(Candidate.status == CandidateStatus.FAILED)
                    ),
                )
                .values(state=TransferState.ABORTED, error=M("transfer.source_failed").text)
            )
            await s.commit()
        for cid in reimport:
            self._tasks.append(asyncio.create_task(self.importer.import_candidate(cid)))
        await self._merge_orphan_downloads()
        await self._repair_requeued_imports()

    async def _repair_requeued_imports(self) -> None:
        """Repairs rows touched by an aioslsk bug (now fixed): files of an album already imported
        were put back in the queue and downloaded again. Those rows are received, they are closed."""
        async with self.db.session() as s:
            rows = list((await s.execute(
                select(Transfer).join(Candidate, Candidate.id == Transfer.candidate_id).where(
                    Candidate.status == CandidateStatus.IMPORTED,
                    Transfer.direction == "download",
                    Transfer.state.not_in(["complete", "failed", "aborted"]),
                )
            )).scalars())
            for t in rows:
                t.state = TransferState.COMPLETE
                t.bytes_transferred = t.size or t.bytes_transferred
                t.speed = 0
                t.place_in_queue = None
                t.remotely_queued = False
            await s.commit()
        if rows:
            await self.journal.emit("transfer.repaired_requeued", M("transfer.repaired_requeued", n=len(rows)))

    async def _merge_orphan_downloads(self) -> None:
        """Repairs duplicates left by older versions: a retry created a row without its album link.
        The orphan is folded into the original row, which keeps the link."""
        merged = 0
        async with self.db.session() as s:
            orphans = list(
                (
                    await s.execute(
                        select(Transfer).where(Transfer.direction == "download", Transfer.candidate_id.is_(None))
                    )
                ).scalars()
            )
            for o in orphans:
                linked = (
                    await s.execute(
                        select(Transfer)
                        .where(
                            Transfer.direction == "download",
                            Transfer.username == o.username,
                            Transfer.remote_path == o.remote_path,
                            Transfer.candidate_id.is_not(None),
                        )
                        .order_by(Transfer.id.desc())
                    )
                ).scalars().first()
                if not linked:
                    continue
                if linked.state != TransferState.COMPLETE:
                    linked.state = o.state
                    linked.error = o.error
                    linked.bytes_transferred = o.bytes_transferred
                    linked.completed_at = o.completed_at
                    linked.attempts = (linked.attempts or 1) + 1
                await s.delete(o)
                merged += 1
            await s.commit()
        if merged:
            await self.journal.emit(
                "transfer.repaired", M("transfer.repaired", n=merged)
            )

    def _read_forwarded_port(self) -> int | None:
        path = self.env.forwarded_port_file
        if not path:
            return None
        try:
            port = int(path.read_text().strip())
        except (OSError, ValueError):
            return None
        return port if 1024 <= port <= 65535 else None

    async def _port_loop(self) -> None:
        """Behind gluetun: follows the port the VPN forwards (it changes on each reconnection)."""
        while True:
            await asyncio.sleep(20)
            port = self._read_forwarded_port()
            if port == self.engine.forwarded_port:
                continue
            if port:
                await self.journal.emit(
                    "network.port_forwarded", M("network.port_forwarded", port=port, previous=self.engine.forwarded_port or "—"),
                )
            await self.restart_engine()  # sans port : le moteur se coupe et attend

    async def restart_engine(self) -> None:
        cfg = (await self.settings.get()).soulseek
        if self.env.forwarded_port_file:
            self.engine.forwarded_port = self._read_forwarded_port()
            if not self.engine.forwarded_port:
                # Derrière le VPN, jamais sur le port configuré : personne ne pourrait nous joindre
                waiting = M("network.port_waiting", path=str(self.env.forwarded_port_file))
                await self.engine.hold(cfg, waiting)
                await self.journal.emit("network.port_waiting", waiting, level="warning")
                return
        rules = await self.shares.rules()
        pic = self.env.profile_picture_path
        self.engine.picture = pic.read_bytes() if pic.is_file() else None
        self.engine.share_salt = self._share_salt()
        await self.engine.start(cfg, rules)
        # Restart unfinished downloads (aioslsk keeps no state between runs)
        self._tasks.append(asyncio.create_task(self._resume_downloads()))

    async def _drop_retired_source(self) -> None:
        """Qobuz was removed as a download source: what it was still downloading is cancelled,
        its albums still proposed are set aside (they can't be downloaded any more), and its
        settings (the account token) are dropped from the database."""
        retired = "qobuz:"
        raw = await self.settings.get_raw(self.settings.KEY)
        if raw and "qobuz" in raw:
            await self.settings.save(await self.settings.get())
        async with self.db.session() as s:
            await s.execute(
                update(Transfer)
                .where(Transfer.remote_path.startswith(retired), Transfer.state.not_in(["complete", "failed", "aborted"]))
                .values(state=TransferState.ABORTED, error=M("transfer.source_removed").text)
            )
            await s.execute(
                update(Candidate)
                .where(Candidate.directory.startswith(retired), Candidate.status.in_([CandidateStatus.PROPOSED, CandidateStatus.CHOSEN]))
                .values(status=CandidateStatus.REJECTED)
            )
            await s.commit()

    async def _resume_downloads(self) -> None:
        async with self.db.session() as s:
            pending = list(
                (
                    await s.execute(
                        select(Transfer).where(
                            Transfer.direction == "download",
                            Transfer.state.in_(["queued", "initializing", "transferring"]),
                        )
                    )
                ).scalars()
            )
        if not pending:
            return
        for _ in range(60):
            if self.engine.logged_in:
                break
            await asyncio.sleep(2)
        else:
            return
        await self._restart(pending)
        await self.journal.emit("download.resumed", M("download.resumed", n=len(pending)))

    async def _restart(self, transfers: list[Transfer]) -> None:
        for cid in {t.candidate_id for t in transfers if t.candidate_id}:
            await self.hunter.reactivate(cid, M("reason.restart_retried", n=2))
        for t in transfers:
            if not t.local_path:
                continue
            self.transfers.register("download", t.username, t.remote_path, t.id)
            try:
                await self.engine.download(t.username, t.remote_path, t.local_path.rsplit("/", 1)[0])
            except Exception as e:
                log.warning("resume %s: %s", t.remote_path, e)

    async def _ready_tick(self) -> None:
        """Safety net: every album still downloading whose tracks are all there is shelved,
        whatever chain of events left it there."""
        async with self.db.session() as s:
            ids = list((await s.execute(
                select(ReleaseGroup.id).where(ReleaseGroup.status == AlbumStatus.DOWNLOADING)
            )).scalars())
        for rid in ids:
            await self.importer.import_if_ready(rid)

    async def _ready_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await self._ready_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("album safety net")

    async def _queue_loop(self) -> None:
        """Regularly asks peers for our position in their upload queue."""
        while True:
            await asyncio.sleep(90)
            if not self.engine.logged_in:
                continue
            try:
                await self.engine.refresh_queue_positions()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("queue positions")

    async def _sync_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                hours = (await self.settings.get()).library.sync_interval_hours
                limit = datetime.now(UTC) - timedelta(hours=hours)
                async with self.db.session() as s:
                    ids = list(
                        (
                            await s.execute(
                                select(Artist.id).where(
                                    Artist.monitored.is_(True),
                                    (Artist.last_synced_at.is_(None)) | (Artist.last_synced_at < limit),
                                )
                            )
                        ).scalars()
                    )
                for aid in ids:
                    await self.catalog.sync_artist(aid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("MusicBrainz sync")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.engine.stop()
        await self.mb.close()
        await self.discover.client.aclose()
        await self.db.dispose()
