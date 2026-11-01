"""Moteur Soulseek : enveloppe d'aioslsk exposant une API simple au reste de Soulsick.

- recherches (collecte des résultats pendant une fenêtre de temps) ;
- téléchargements routés vers un dossier précis (pas de dossier de téléchargement global) ;
- parcours des partages d'un pair ;
- partages filtrés par nos règles (extensions autorisées, motifs exclus).
"""

import asyncio
import base64
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from aioslsk.client import SoulSeekClient
from aioslsk.commands import (
    GetRoomListCommand,
    GetUserInterestsCommand,
    GetUserStatsCommand,
    GetUserStatusCommand,
    JoinRoomCommand,
    LeaveRoomCommand,
    PeerGetSharesCommand,
    PeerGetUserInfoCommand,
    PrivateMessageCommand,
    RoomMessageCommand,
)
from aioslsk.events import (
    PrivateMessageEvent,
    RoomJoinedEvent,
    RoomLeftEvent,
    RoomMessageEvent,
    SearchResultEvent,
    SessionDestroyedEvent,
    SessionInitializedEvent,
    TransferAddedEvent,
    TransferProgressEvent,
)
from aioslsk.constants import TRANSFER_REPLY_TIMEOUT
from aioslsk.exceptions import AuthenticationError, ConnectionReadError, ConnectionWriteError, PeerConnectionError
from aioslsk.network.connection import CloseReason, PeerConnectionType
from aioslsk.protocol.messages import PeerTransferQueue, PeerTransferReply, PeerTransferRequest, SendUploadSpeed
from aioslsk.protocol.primitives import AttributeKey, FileData, uint32
from aioslsk.search.model import SearchResult
from aioslsk.settings import (
    CredentialsSettings,
    NetworkLimitSettings,
    NetworkSettings,
    ReconnectSettings,
    ServerSettings,
    Settings,
    SharedDirectorySettingEntry,
    SharesSettings,
    TransferLimitSettings,
    TransfersSettings,
    UpnpSettings,
    UserInfoSettings,
)
from aioslsk.settings import ListeningSettings as SlskListening
from aioslsk.shares.manager import SharesManager
from aioslsk.shares.model import SharedDirectory
from aioslsk.transfer.manager import TransferManager, _RequestFlag
from aioslsk.transfer.model import Transfer as SlskTransfer
from aioslsk.transfer.model import TransferDirection
from aioslsk.transfer.state import TransferState as SlskState

from ..messages import M, Msg
from ..models import TransferState
from ..naming import safe_component
from ..scoring import PeerResult, RemoteFile
from ..settings_store import SoulseekSettings
from .rules import ShareRules

log = logging.getLogger(__name__)


class EngineUnavailable(Exception):
    """The Soulseek engine can't do it right now (not configured, offline…)."""

    def __init__(self, msg: Msg | str | None = None) -> None:
        self.msg = msg if isinstance(msg, Msg) else M("raw", text=msg) if msg else M("engine.offline")
        super().__init__(self.msg.text)


_STATE_MAP = {
    SlskState.VIRGIN: TransferState.QUEUED,
    SlskState.QUEUED: TransferState.QUEUED,
    SlskState.INITIALIZING: TransferState.INITIALIZING,
    SlskState.INCOMPLETE: TransferState.QUEUED,  # aioslsk relance automatiquement
    SlskState.DOWNLOADING: TransferState.TRANSFERRING,
    SlskState.UPLOADING: TransferState.TRANSFERRING,
    SlskState.COMPLETE: TransferState.COMPLETE,
    SlskState.FAILED: TransferState.FAILED,
    SlskState.ABORTED: TransferState.ABORTED,
    SlskState.PAUSED: TransferState.PAUSED,
}


@dataclass
class TransferUpdate:
    direction: str
    username: str
    remote_path: str
    local_path: str | None
    size: int | None
    bytes_transferred: int
    speed: float
    state: str
    place_in_queue: int | None
    error: str | None
    state_changed: bool
    # Le pair a accepté la demande et l'a mise dans sa file d'envoi
    remotely_queued: bool = False


@dataclass
class LiveSearch:
    ticket: int
    query: str
    started: float
    results: list[PeerResult] = field(default_factory=list)
    # Journal des réponses, dans l'ordre d'arrivée (pour l'affichage en direct)
    responses: list[dict] = field(default_factory=list)


search_log = logging.getLogger("soulsick.search")


def remote_file(fd: FileData, directory: str | None = None) -> RemoteFile:
    attrs = fd.get_attribute_map()
    name = fd.filename if directory is None else f"{directory}\\{fd.filename}"
    return RemoteFile(
        filename=name,
        size=fd.filesize,
        bitrate=attrs.get(AttributeKey.BITRATE),
        duration=attrs.get(AttributeKey.DURATION),
        sample_rate=attrs.get(AttributeKey.SAMPLE_RATE),
        bit_depth=attrs.get(AttributeKey.BIT_DEPTH),
        vbr=bool(attrs.get(AttributeKey.VBR)),
    )


class FilteringSharesManager(SharesManager):
    """SharesManager qui applique nos règles après chaque scan : un fichier exclu n'est
    ni indexé, ni proposé en résultat de recherche, ni téléchargeable."""

    rules: dict[str, ShareRules]
    # Per-install secret behind the folder aliases ("@@abcde")
    salt: bytes = b""

    def generate_alias(self, path: str, offset: int = 0) -> str:
        """Same alias for the same folder across restarts.

        aioslsk mixes in the MAC address, which changes each time the container is recreated:
        every path peers had (search results, their download queues) then answered
        "File not shared." A secret kept in the config folder plays that part instead.
        """
        if not self.salt:
            return super().generate_alias(path, offset)
        digest = hashlib.sha256(self.salt + offset.to_bytes(2, "big") + path.encode()).digest()
        return "".join(chr(0x61 + b % 26) for b in digest[: self._ALIAS_LENGTH])

    async def stop(self) -> list[asyncio.Task]:
        """aioslsk waits, on the event loop, for its pool to finish: the whole scan of the
        shares (minutes over sshfs), during which nothing else answered. The scan is dropped
        instead: the next client scans again."""
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None
        return []

    async def scan_directory_files(self, shared_directory: SharedDirectory):
        await super().scan_directory_files(shared_directory)
        rules = getattr(self, "rules", {}).get(shared_directory.absolute_path)
        if rules is None:
            return
        kept = {
            item
            for item in shared_directory.items
            if rules.check(os.path.join(item.subdir, item.filename)) is None
        }
        removed = shared_directory.items - kept
        if removed:
            shared_directory.items = kept
            self._term_map = {}
            for d in self._shared_directories:
                self._build_term_map(d)
            self._cleanup_term_map()


class SafeTransferManager(TransferManager):
    """aioslsk's transfer manager, with one fix.

    When asking a peer to queue a file fails (a connection error, often after the
    `GetPeerAddress … invalid state` race on several files to the same peer), aioslsk puts
    the transfer back in the queue WHATEVER its state. If the file arrived meanwhile through
    another connection, it is COMPLETE and gets downloaded again (as "name (1).flac"…);
    a PAUSED or ABORTED one restarts on its own. Here it is re-queued only if it was
    still waiting to be queued.
    """

    def manage_transfers(self):
        """aioslsk's cycle, except that a download whose queue request is still running
        doesn't get a second one (aioslsk starts a new request on every cycle)."""
        downloads, uploads = self._get_queued_transfers()
        free_upload_slots = self.get_free_upload_slots()
        for download in downloads:
            running = getattr(download, "_remotely_queue_task", None)
            if running is not None and not running.done():
                continue
            download._remotely_queue_task = asyncio.create_task(self._queue_remotely(download))
            download._remotely_queue_task.add_done_callback(download._remotely_queue_task_complete)
        for upload in uploads[:free_upload_slots]:
            upload._transfer_task = asyncio.create_task(self._initialize_upload(upload))
            upload._transfer_task.add_done_callback(upload._transfer_task_complete)

    # A peer that just failed to answer: its other files wait for the next cycle instead of
    # each waiting for the same connection timeout
    PEER_FAILURE_COOLDOWN = 30.0

    def _peer_lock(self, username: str) -> asyncio.Lock:
        locks = self.__dict__.setdefault("_peer_locks", {})
        return locks.setdefault(username, asyncio.Lock())

    async def _queue_remotely(self, transfer: SlskTransfer):
        # One request at a time per peer. Sent all at once, the requests for N files each ask the
        # server for the peer's address; aioslsk hands every answer to the first request, fails
        # on the second (`GetPeerAddress … invalid state`) and the N-1 others never get one.
        # In sequence, the first opens the connection and the next ones reuse it.
        failures = self.__dict__.setdefault("_peer_failures", {})
        async with self._peer_lock(transfer.username):
            recent = time.monotonic() - failures.get(transfer.username, -1e9) < self.PEER_FAILURE_COOLDOWN
            try:
                if recent:
                    raise PeerConnectionError("peer unreachable a moment ago")
                await self._network.send_peer_messages(transfer.username, PeerTransferQueue.Request(transfer.remote_path))
            except (ConnectionWriteError, PeerConnectionError) as exc:
                if not recent:
                    failures[transfer.username] = time.monotonic()
                log.debug("failed to queue transfer remotely: %s: %r", transfer, exc)
                if transfer.state.VALUE == SlskState.QUEUED:
                    transfer.increase_queue_attempts()
                    await transfer.state.queue()
                return
            failures.pop(transfer.username, None)
        if transfer.state.VALUE == SlskState.QUEUED:
            transfer.remotely_queued = True
            transfer.reset_queue_attempts()
        self.request_management_cycle(_RequestFlag.TRANSFER_CHANGE)


    # How long an upload waits for its file connection to be ready (ticket sent, offset received)
    FILE_CONNECTION_TIMEOUT = 30.0

    async def _initialize_upload(self, transfer: SlskTransfer):
        """aioslsk's upload start, except for the file connection.

        aioslsk races a direct and an indirect connection and keeps the first one *opened*. With a
        peer whose port is closed, the direct one can open and lead nowhere (a router or firewall
        accepting it): aioslsk then dropped the indirect one, the real one, and waited for an offset
        that never came. And when slskd / Soulseek.NET opens the file connection itself (its
        "second-chance transfer connection", sent with our ticket), aioslsk dropped it as unknown.
        Firewalled slskd users could never download from us.

        Here every way in — direct, indirect, and the peer's own connection — goes through the
        whole handshake (ticket, then the peer's offset); the first one complete carries the file.
        The rest is aioslsk's flow, unchanged.
        """
        await transfer.state.initialize()
        ticket = next(self._ticket_generator)
        try:
            await self._network.send_peer_messages(
                transfer.username,
                PeerTransferRequest.Request(
                    TransferDirection.DOWNLOAD.value, ticket, transfer.remote_path, filesize=transfer.filesize
                ),
            )
        except (ConnectionWriteError, PeerConnectionError) as exc:
            log.debug("failed to send request to upload : %s : %r", transfer, exc)
            await transfer.state.queue()
            return

        # Registered before the peer can answer: its own file connection may come right after
        incoming: asyncio.Future = asyncio.get_running_loop().create_future()
        self._file_connection_futures[ticket] = incoming
        try:
            try:
                async with asyncio.timeout(TRANSFER_REPLY_TIMEOUT):
                    _, response = await self._network.create_peer_response_future(
                        transfer.username, PeerTransferReply.Request, fields={"ticket": ticket}
                    )
            except TimeoutError:
                log.debug("timeout waiting for transfer reply : %s", transfer)
                await transfer.state.queue()
                return
            if not response.allowed:
                await transfer.state.fail(reason=response.reason)
                return
            ready = await self._file_connection(transfer, ticket, incoming)
        finally:
            self._file_connection_futures.pop(ticket, None)
            if not incoming.done():
                incoming.cancel()

        if ready is None:
            log.info("failed to create peer connection for transfer : %s", transfer)
            await transfer.state.queue()
            return
        connection, transfer.bytes_transfered = ready
        await self._upload_file(transfer, connection)
        if transfer.state.VALUE == SlskState.COMPLETE:
            self._network.queue_server_messages(SendUploadSpeed.Request(int(transfer.get_speed())))

    async def _file_connection(self, transfer: SlskTransfer, ticket: int, incoming: asyncio.Future):
        """(connection, offset) of the first way in to complete the handshake, else None."""
        net = self._network
        user, typ = transfer.username, PeerConnectionType.FILE
        attempts = {
            asyncio.create_task(_handshake(lambda: net._make_direct_connection(next(net._ticket_generator), user, typ), ticket)),
            asyncio.create_task(_handshake(lambda: net._make_indirect_connection(next(net._ticket_generator), user, typ), ticket)),
            # The peer's own connection: it already sent our ticket on it
            asyncio.create_task(_handshake(lambda: incoming, ticket, send_ticket=False)),
        }
        winner = None
        pending = attempts
        try:
            async with asyncio.timeout(self.FILE_CONNECTION_TIMEOUT):
                while pending and winner is None:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        if not task.cancelled() and task.exception() is None:
                            winner = task
                            break
                        log.debug("file connection attempt failed : %s : %r", transfer, task.exception() if not task.cancelled() else "cancelled")
        except TimeoutError:
            pass
        losers = [t for t in attempts if t is not winner]
        for task in losers:
            task.cancel()
        await asyncio.gather(*losers, return_exceptions=True)
        for task in losers:
            # Completed at the same moment as the winner: not needed
            if not task.cancelled() and task.exception() is None:
                await task.result()[0].disconnect(CloseReason.REQUESTED)
        return winner.result() if winner else None


async def _handshake(open_connection, ticket: int, send_ticket: bool = True):
    """Opens a file connection and completes it: our ticket, then the downloader's offset."""
    connection = await open_connection()
    try:
        if send_ticket:
            await connection.send_message(uint32(ticket).serialize())
        offset = await connection.receive_transfer_offset()
    except BaseException:
        await connection.disconnect(CloseReason.REQUESTED)
        raise
    return connection, offset


def _shares_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=2, thread_name_prefix="slsk-shares")


class _Client(SoulSeekClient):
    def create_shares_manager(self, cache, executor_factory=None):
        # aioslsk reads the tags of every shared file at start-up (thousands, over the network for a
        # Storage Box) on Python's default thread pool: everything else using that pool — scoring a
        # hunt, reading a tag — waited behind them for minutes. Its own pool keeps them apart.
        return FilteringSharesManager(
            self.settings, self.events, self.network, cache=cache,
            executor_factory=executor_factory or _shares_executor,
        )

    def create_transfer_manager(self, cache):
        return SafeTransferManager(self.settings, self.events, self.users, self.shares, self.network, cache=cache)


@dataclass
class ChatEvent:
    """A chat message seen on the network (private or in a room), or a room membership change."""

    kind: str  # private | room
    target: str  # the other user (private) or the room name
    username: str  # author
    text: str
    timestamp: float
    outgoing: bool = False


TransferCallback = Callable[[TransferUpdate], Awaitable[None]]
StatusCallback = Callable[[dict], None]


class SlskEngine:
    def __init__(self, downloads_dir: str) -> None:
        self.downloads_dir = downloads_dir
        self.client: _Client | None = None
        self.config: SoulseekSettings | None = None
        self.shares: list[ShareRules] = []
        self.logged_in = False
        self.connecting = False
        self.error: Msg | None = None
        self.searches: dict[int, LiveSearch] = {}
        self.on_transfer: TransferCallback | None = None
        self.on_status: StatusCallback | None = None
        self.on_info: Callable[[str, str, dict], Awaitable[None]] | None = None
        self.on_chat: Callable[[ChatEvent], Awaitable[None]] | None = None
        # Port opened by the VPN (gluetun); overrides the configured one, single port, no UPnP
        self.forwarded_port: int | None = None
        self.on_room: Callable[[str, bool], Awaitable[None]] | None = None
        # Rooms to (re)join after each login
        self.rooms_wanted: Callable[[], Awaitable[list[str]]] | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._stopping = False
        # The VPN port loop and the settings both restart the engine: one at a time, or the
        # client of an overlapping restart stays logged in next to the new one.
        self._lifecycle = asyncio.Lock()
        # Your profile picture, sent to peers who ask for your user info
        self.picture: bytes | None = None
        # Keeps shared folder aliases stable across restarts (see FilteringSharesManager)
        self.share_salt: bytes = b""

    # --- Cycle de vie ---------------------------------------------------------

    def _build_settings(self, cfg: SoulseekSettings) -> Settings:
        return Settings(
            credentials=CredentialsSettings(
                username=cfg.username,
                password=cfg.password,
                info=UserInfoSettings(description=cfg.description, picture=self.picture),
            ),
            network=NetworkSettings(
                server=ServerSettings(reconnect=ReconnectSettings(auto=True)),
                listening=(
                    SlskListening(port=self.forwarded_port, obfuscated_port=0)
                    if self.forwarded_port
                    else SlskListening(port=cfg.listen_port, obfuscated_port=cfg.listen_port + 1)
                ),
                upnp=UpnpSettings(enabled=cfg.upnp and not self.forwarded_port),
                limits=NetworkLimitSettings(
                    upload_speed_kbps=cfg.upload_speed_kbps,
                    download_speed_kbps=cfg.download_speed_kbps,
                ),
            ),
            shares=SharesSettings(
                scan_on_start=True,
                download=os.path.join(self.downloads_dir, "manual"),
                directories=[SharedDirectorySettingEntry(path=s.path) for s in self.shares],
            ),
            transfers=TransfersSettings(limits=TransferLimitSettings(upload_slots=cfg.upload_slots)),
        )

    def status(self) -> dict:
        c = self.client
        dirs, files = c.shares.get_stats() if c else (0, 0)
        return {
            "configured": bool(self.config and self.config.username and self.config.password),
            "connected": self.logged_in,
            "connecting": self.connecting,
            "username": self.config.username if self.config else None,
            "error": self.error.text if self.error else None,
            "error_t": self.error.to_json() if self.error else None,
            "listen_port": self.forwarded_port or (self.config.listen_port if self.config else None),
            "forwarded_port": self.forwarded_port,
            "download_speed": c.transfers.get_download_speed() if c else 0,
            "upload_speed": c.transfers.get_upload_speed() if c else 0,
            "free_upload_slots": c.transfers.get_free_upload_slots() if c else (self.config.upload_slots if self.config else 0),
            "upload_slots": self.config.upload_slots if self.config else 0,
            "shared_folders": dirs,
            "shared_files": files,
        }

    def _publish_status(self) -> None:
        if self.on_status:
            self.on_status(self.status())

    async def start(self, cfg: SoulseekSettings, shares: list[ShareRules]) -> None:
        async with self._lifecycle:
            await self._halt()
            self._begin(cfg, shares)

    def _begin(self, cfg: SoulseekSettings, shares: list[ShareRules]) -> None:
        self._stopping = False
        self.config = cfg
        self.shares = [s for s in shares if os.path.isdir(s.path)]
        self.error = None
        if not (cfg.username and cfg.password):
            self.error = M("engine.not_configured")
            self._publish_status()
            return
        self._reconnect_task = asyncio.create_task(self._connect_loop())

    async def _connect_loop(self) -> None:
        delay = 5
        while not self._stopping:
            self.connecting = True
            self._publish_status()
            try:
                await self._connect_once()
                self.connecting = False
                self._publish_status()
                return
            except AuthenticationError as e:
                self.error = M("engine.auth_refused", reason=e.reason or M("engine.invalid_credentials"))
                self.connecting = False
                await self._info("slsk.auth_failed", self.error, {})
                self._publish_status()
                await self._teardown()
                return
            except Exception as e:  # network, ports…
                self.error = M("engine.connect_failed", error=repr(e))
                await self._info("slsk.connect_failed", M("slsk.connect_retry", error=self.error, delay=delay), {})
                await self._teardown()
                self._publish_status()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def _connect_once(self) -> None:
        assert self.config
        client = _Client(self._build_settings(self.config))
        client.shares.rules = {os.path.normpath(os.path.abspath(s.path)): s for s in self.shares}
        client.shares.salt = self.share_salt
        self.client = client
        client.events.register(SessionInitializedEvent, self._on_session_init)
        client.events.register(SessionDestroyedEvent, self._on_session_destroyed)
        client.events.register(SearchResultEvent, self._on_search_result)
        client.events.register(TransferProgressEvent, self._on_progress)
        client.events.register(TransferAddedEvent, self._on_transfer_added)
        client.events.register(PrivateMessageEvent, self._on_private_message)
        client.events.register(RoomMessageEvent, self._on_room_message)
        client.events.register(RoomJoinedEvent, self._on_room_joined)
        client.events.register(RoomLeftEvent, self._on_room_left)
        await client.start()
        await client.login()

    async def _teardown(self) -> None:
        c, self.client = self.client, None
        self.logged_in = False
        if c:
            try:
                await c.stop()
            except Exception:
                log.exception("stopping the aioslsk client")

    async def stop(self) -> None:
        async with self._lifecycle:
            await self._halt()

    async def hold(self, cfg: SoulseekSettings, reason: Msg) -> None:
        """Stays off the network, saying why (behind the VPN, until it forwards a port)."""
        async with self._lifecycle:
            await self._halt()
            self.config = cfg
            self.error = reason
            self._publish_status()

    async def _halt(self) -> None:
        self._stopping = True
        task, self._reconnect_task = self._reconnect_task, None
        if task:
            task.cancel()
            # Let it finish: a client it is still connecting must not outlive it
            await asyncio.wait([task])
        await self._teardown()
        self.connecting = False

    async def _info(self, kind: str, message: Msg, data: dict, level: str = "warning") -> None:
        if self.on_info:
            await self.on_info(kind, message, {**data, "level": level})

    def _require(self) -> _Client:
        if not self.client or not self.logged_in:
            raise EngineUnavailable(self.error or M("engine.offline"))
        return self.client

    # --- aioslsk events -------------------------------------------------------

    async def _on_session_init(self, event: SessionInitializedEvent) -> None:
        self.logged_in = True
        self.error = None
        await self._info(
            "slsk.connected",
            M("slsk.connected", user=event.session.user.name),
            {"ip": event.session.ip_address, "greeting": event.session.greeting},
            level="info",
        )
        self._publish_status()
        asyncio.create_task(self._rejoin_rooms())

    async def _rejoin_rooms(self) -> None:
        if not self.rooms_wanted or not self.client:
            return
        for room in await self.rooms_wanted():
            try:
                await self.client.execute(JoinRoomCommand(room))
            except Exception as e:
                log.info("rejoin %s: %s", room, e)

    # --- Chat events -----------------------------------------------------------

    async def _on_private_message(self, event: PrivateMessageEvent) -> None:
        m = event.message
        if self.on_chat:
            await self.on_chat(ChatEvent("private", m.user.name, m.user.name, m.message, float(m.timestamp)))

    async def _on_room_message(self, event: RoomMessageEvent) -> None:
        m = event.message
        me = self.config.username if self.config else None
        if self.on_chat:
            await self.on_chat(
                ChatEvent("room", m.room.name, m.user.name, m.message, float(m.timestamp), outgoing=m.user.name == me)
            )

    async def _on_room_joined(self, event: RoomJoinedEvent) -> None:
        if event.user is None and self.on_room:
            await self.on_room(event.room.name, True)

    async def _on_room_left(self, event: RoomLeftEvent) -> None:
        if event.user is None and self.on_room:
            await self.on_room(event.room.name, False)

    async def _on_session_destroyed(self, event: SessionDestroyedEvent) -> None:
        self.logged_in = False
        if not self._stopping:
            await self._info("slsk.disconnected", M("slsk.disconnected"), {})
        self._publish_status()

    async def _on_search_result(self, event: SearchResultEvent) -> None:
        live = self.searches.get(event.query.ticket)
        if live is None:
            return
        r: SearchResult = event.result
        files = [remote_file(f) for f in r.shared_items]
        live.results.append(
            PeerResult(
                username=r.username,
                has_free_slots=r.has_free_slots,
                avg_speed=r.avg_speed,
                queue_size=r.queue_size,
                files=files,
            )
        )
        folders = len({f.filename.replace("/", "\\").rsplit("\\", 1)[0] for f in files})
        elapsed = round(time.time() - live.started, 1)
        live.responses.append(
            {
                "at": elapsed,
                "username": r.username,
                "files": len(files),
                "folders": folders,
                "locked": len(r.locked_results),
                "free_slot": r.has_free_slots,
                "speed": r.avg_speed,
                "queue": r.queue_size,
            }
        )
        search_log.info(
            "“%s” +%.1fs: %s answers — %d file(s) in %d folder(s)%s, %s, queue %d, %d KB/s",
            live.query, elapsed, r.username, len(files), folders,
            f", {len(r.locked_results)} locked" if r.locked_results else "",
            "free slot" if r.has_free_slots else "no free slot", r.queue_size, r.avg_speed // 1024,
        )

    async def _on_transfer_added(self, event: TransferAddedEvent) -> None:
        t = event.transfer
        await self._emit_transfer(t, t.progress_snapshot.state, changed=True)

    async def _on_progress(self, event: TransferProgressEvent) -> None:
        for t, prev, cur in event.updates:
            await self._emit_transfer(t, cur.state, changed=prev.state != cur.state)

    async def _emit_transfer(self, t: SlskTransfer, state: SlskState.State, changed: bool) -> None:
        if not self.on_transfer:
            return
        mapped = _STATE_MAP.get(state, TransferState.QUEUED)
        upd = TransferUpdate(
            direction="upload" if t.direction == TransferDirection.UPLOAD else "download",
            username=t.username,
            remote_path=t.remote_path,
            local_path=t.local_path,
            size=t.filesize,
            bytes_transferred=t.bytes_transfered,
            speed=t.get_speed() if hasattr(t, "get_speed") else 0.0,
            state=mapped,
            place_in_queue=t.place_in_queue,
            error=t.fail_reason or t.abort_reason,
            state_changed=changed,
            remotely_queued=bool(getattr(t, "remotely_queued", False)),
        )
        try:
            await self.on_transfer(upd)
        except Exception:
            log.exception("transfer callback")

    # --- Search -------------------------------------------------------------

    async def start_search(self, query: str) -> LiveSearch:
        c = self._require()
        req = await c.searches.search(query)
        live = LiveSearch(ticket=req.ticket, query=query, started=time.time())
        search_log.info("Query “%s” sent to the server (ticket %d) — waiting for peers to answer", query, req.ticket)
        self.searches[req.ticket] = live
        # Drop old searches (keep the last 50)
        if len(self.searches) > 50:
            for k in sorted(self.searches, key=lambda k: self.searches[k].started)[:-50]:
                self.searches.pop(k, None)
        return live

    async def search(self, query: str, wait_s: float, min_wait_s: float = 8.0, quiet_s: float = 4.0) -> list[PeerResult]:
        """Listens for answers; stops as soon as they dry up (≥ min_wait_s, ≤ wait_s).

        Popular searches fill the whole window; rare ones don't wait for nothing.
        """
        live = await self.start_search(query)
        last_count, last_change = 0, time.monotonic()
        start = time.monotonic()
        while (elapsed := time.monotonic() - start) < wait_s:
            await asyncio.sleep(0.5)
            if len(live.results) != last_count:
                last_count, last_change = len(live.results), time.monotonic()
            elif elapsed >= min_wait_s and time.monotonic() - last_change >= quiet_s:
                search_log.info(
                    "“%s”: no new answer for %.0f s, stopped listening after %.0f s (%d peers)",
                    query, quiet_s, elapsed, last_count,
                )
                break
        return list(live.results)

    # --- Transfers ----------------------------------------------------------

    async def download(self, username: str, remote_path: str, dest_dir: str) -> None:
        """Télécharge un fichier distant vers `dest_dir` (le nom de fichier distant est conservé)."""
        c = self._require()
        os.makedirs(dest_dir, exist_ok=True)
        name = safe_component(remote_path.replace("\\", "/").rsplit("/", 1)[-1])
        existing = c.transfers.find_transfer(username, remote_path, TransferDirection.DOWNLOAD)
        if existing is not None:
            existing.local_path = os.path.join(dest_dir, name)
            await c.transfers.queue(existing)
            return
        t = await c.transfers.download(username, remote_path, paused=True)
        t.local_path = os.path.join(dest_dir, name)
        await c.transfers.queue(t)

    def _find(self, username: str, remote_path: str, direction: str) -> SlskTransfer | None:
        c = self._require()
        d = TransferDirection.UPLOAD if direction == "upload" else TransferDirection.DOWNLOAD
        return c.transfers.find_transfer(username, remote_path, d)

    async def abort(self, username: str, remote_path: str, direction: str) -> None:
        t = self._find(username, remote_path, direction)
        if t:
            try:
                await self._require().transfers.abort(t)
            except Exception as e:
                log.info("abort: %s", e)

    async def pause(self, username: str, remote_path: str, direction: str = "download") -> None:
        t = self._find(username, remote_path, direction)
        if t:
            try:
                await self._require().transfers.pause(t)
            except Exception as e:
                log.info("pause: %s", e)

    async def retry(self, username: str, remote_path: str, direction: str = "download") -> bool:
        t = self._find(username, remote_path, direction)
        if t is None:
            return False
        await self._require().transfers.queue(t)
        return True

    async def remove(self, username: str, remote_path: str, direction: str) -> None:
        t = self._find(username, remote_path, direction)
        if t:
            await self._require().transfers.remove(t)

    async def refresh_queue_positions(self) -> None:
        """Demande aux pairs notre place dans leur file pour les téléchargements en attente."""
        c = self._require()
        waiting = [
            t for t in c.transfers.get_downloads()
            if t.state.VALUE == SlskState.QUEUED and getattr(t, "remotely_queued", False)
        ]
        for t in waiting:
            try:
                place = await c.transfers.request_place_in_queue(t)
            except Exception:
                continue
            if place is not None and place != t.place_in_queue:
                t.place_in_queue = place
                await self._emit_transfer(t, t.state.VALUE, changed=False)

    # --- Parcourir un pair ----------------------------------------------------

    async def browse(self, username: str, timeout: float = 90) -> dict:
        c = self._require()
        directories, locked = await c.execute(PeerGetSharesCommand(username), response=True, timeout=timeout)
        def conv(dirs):
            return [
                {
                    "path": d.name,
                    "files": [
                        remote_file(f, d.name).to_dict() | {"name": f.filename}
                        for f in d.files
                    ],
                }
                for d in dirs
            ]
        return {"directories": conv(directories), "locked": conv(locked)}

    # --- Peer profile ---------------------------------------------------------

    async def user_status(self, username: str) -> str | None:
        """online / away / offline according to the server (None when it doesn't say)."""
        try:
            st = await self._require().execute(GetUserStatusCommand(username), response=True, timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return st.status.name.lower()

    async def peer_profile(self, username: str, timeout: float = 20) -> dict:
        """What the network says about a peer: status and stats (server), description,
        picture and upload slots (asked to the peer itself), interests. Each part may be missing."""
        c = self._require()

        async def ask(cmd, t: float):
            try:
                return await c.execute(cmd, response=True, timeout=t)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # offline peer, timeout, refused connection…
                log.debug("peer %s: %s %s", username, type(cmd).__name__, e)
                return None

        status, stats, info, interests = await asyncio.gather(
            ask(GetUserStatusCommand(username), 10),
            ask(GetUserStatsCommand(username), 10),
            ask(PeerGetUserInfoCommand(username), timeout),
            ask(GetUserInterestsCommand(username), 10),
        )
        out: dict = {"username": username, "status": None, "privileged": None, "reachable": info is not None}
        if status is not None:
            out["status"] = status.status.name.lower()
            out["privileged"] = bool(status.privileged)
        if stats is not None:
            out |= {
                "avg_speed": stats.avg_speed,
                "uploads": stats.uploads,
                "shared_files": stats.shared_file_count,
                "shared_folders": stats.shared_folder_count,
            }
        if info is not None:
            out |= {
                "description": (info.description or "").strip() or None,
                "picture": _picture_data_url(info.picture),
                "has_free_slots": info.has_slots_free,
                "upload_slots": info.upload_slots,
                "queue_size": info.queue_length,
                "upload_permissions": info.upload_permissions.name.lower(),
            }
        if interests is not None:
            liked, hated = interests
            out["interests"], out["hated_interests"] = sorted(liked), sorted(hated)
        country = c.users.get_user_object(username).country
        out["country"] = country or None
        return out

    # --- Chat -----------------------------------------------------------------

    async def send_private(self, username: str, text: str) -> None:
        await self._require().execute(PrivateMessageCommand(username, text))

    async def send_room(self, room: str, text: str) -> None:
        # The server echoes our own room messages back: they arrive through _on_room_message
        await self._require().execute(RoomMessageCommand(room, text))

    async def join_room(self, room: str, timeout: float = 15) -> None:
        await self._require().execute(JoinRoomCommand(room), response=True, timeout=timeout)

    async def leave_room(self, room: str) -> None:
        await self._require().execute(LeaveRoomCommand(room))

    async def room_list(self, timeout: float = 20) -> list[dict]:
        rooms = await self._require().execute(GetRoomListCommand(), response=True, timeout=timeout)
        return [{"name": r.name, "users": r.user_count, "private": r.private} for r in rooms]

    def room_users(self, room: str) -> list[str]:
        c = self._require()
        r = c.rooms.rooms.get(room)
        return sorted((u.name for u in r.users), key=str.lower) if r else []

    # --- Shares ---------------------------------------------------------------

    async def apply_shares(self, shares: list[ShareRules]) -> None:
        """Met à jour les dossiers partagés à chaud puis relance un scan."""
        self.shares = [s for s in shares if os.path.isdir(s.path)]
        c = self.client
        if not c:
            return
        mgr: FilteringSharesManager = c.shares  # type: ignore[assignment]
        mgr.rules = {os.path.normpath(os.path.abspath(s.path)): s for s in self.shares}
        wanted = set(mgr.rules)
        for d in list(mgr.shared_directories):
            if d.absolute_path not in wanted:
                mgr.remove_shared_directory(d)
        for path in wanted:
            if not mgr.is_directory_shared(path):
                mgr.add_shared_directory(path)
        await mgr.scan()
        self._publish_status()

    def set_picture(self, data: bytes | None) -> None:
        """Changes your profile picture; peers get the new one from their next request, no reconnection."""
        self.picture = data
        if self.client:
            self.client.settings.credentials.info.picture = data

    def uploads_snapshot(self) -> list[SlskTransfer]:
        return self.client.transfers.get_uploads() if self.client else []


PICTURE_MAX_BYTES = 1_000_000


def picture_mime(data: bytes | None) -> str | None:
    """Image type from its first bytes (common formats only), None for anything else."""
    if not data:
        return None
    return (
        "image/jpeg" if data[:3] == b"\xff\xd8\xff"
        else "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n"
        else "image/gif" if data[:4] == b"GIF8"
        else "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        else None
    )


def _picture_data_url(data: bytes | None) -> str | None:
    """A peer's picture, as an inline image (only common formats, at most 1 MB)."""
    if not data or len(data) > PICTURE_MAX_BYTES:
        return None
    mime = picture_mime(data)
    return f"data:{mime};base64,{base64.b64encode(data).decode()}" if mime else None
