"""A queue request that fails is tried again once the peer's cooldown is over.

aioslsk's management cycle only runs when something asks for it (a transfer changing state, a
user status...). After a failed queue request the download stays QUEUED, and QueuedState has no
queue() transition: nothing asks for a cycle, so the file was only asked again if another event
happened to wake the manager. These tests use aioslsk's real transfers, states and cycle.
"""

import asyncio
import time

from aioslsk.events import EventBus
from aioslsk.exceptions import PeerConnectionError
from aioslsk.settings import CredentialsSettings, Settings
from aioslsk.transfer.state import TransferState as S
from aioslsk.user.model import UserStatus

from soulsick.slsk.engine import SafeTransferManager

COOLDOWN = 0.3


class _User:
    status = UserStatus.UNKNOWN


class _Users:
    def get_user_object(self, username):
        return _User()

    async def track_user(self, username, flag):
        pass

    async def untrack_user(self, username, flag):
        pass


class _Net:
    """A peer that does not answer the first `failures` queue requests."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls: list[tuple[float, str]] = []

    async def send_peer_messages(self, username, *messages, **kwargs):
        self.calls.append((time.monotonic(), messages[0].filename))
        if len(self.calls) <= self.failures:
            raise PeerConnectionError("peer unreachable")


async def _manager(net: _Net) -> SafeTransferManager:
    settings = Settings(credentials=CredentialsSettings(username="me", password="PASSWORD"))
    mgr = SafeTransferManager(settings, EventBus(), _Users(), None, net)
    mgr.PEER_FAILURE_COOLDOWN = COOLDOWN
    await mgr.start()
    return mgr


async def _until(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def test_a_failed_queue_request_is_tried_again_after_the_cooldown():
    net = _Net(failures=1)
    mgr = await _manager(net)
    try:
        t = await mgr.download("goodpeer", "album/01.flac")
        assert await _until(lambda: len(net.calls) >= 1)
        # Nothing else happens on the client: no other transfer, no status change
        assert await _until(lambda: t.remotely_queued, timeout=COOLDOWN * 5), "never asked again"
        assert t.state.VALUE == S.QUEUED
        assert len(net.calls) == 2
        # Not before the peer's cooldown
        assert net.calls[1][0] - net.calls[0][0] >= COOLDOWN
    finally:
        await asyncio.gather(*await mgr.stop(), return_exceptions=True)


async def test_the_other_files_of_the_peer_wait_for_the_same_retry():
    net = _Net(failures=1)
    mgr = await _manager(net)
    try:
        first = await mgr.download("goodpeer", "album/01.flac")
        assert await _until(lambda: len(net.calls) >= 1)
        # Asked during the cooldown: no request sent, it waits like the first one
        second = await mgr.download("goodpeer", "album/02.flac")
        await asyncio.sleep(COOLDOWN / 3)
        assert len(net.calls) == 1 and not second.remotely_queued
        assert await _until(lambda: first.remotely_queued and second.remotely_queued, timeout=COOLDOWN * 5)
        assert len(net.calls) == 3
    finally:
        await asyncio.gather(*await mgr.stop(), return_exceptions=True)


async def test_a_peer_that_never_answers_is_asked_once_per_cooldown():
    net = _Net(failures=10**6)
    mgr = await _manager(net)
    try:
        await mgr.download("goodpeer", "album/01.flac")
        await mgr.download("goodpeer", "album/02.flac")
        await asyncio.sleep(COOLDOWN * 3.5)
        # One request per cooldown, the other file is not sent meanwhile: no busy loop
        assert 2 <= len(net.calls) <= 5
        gaps = [b[0] - a[0] for a, b in zip(net.calls, net.calls[1:], strict=False)]
        assert all(g >= COOLDOWN * 0.9 for g in gaps), gaps
    finally:
        await asyncio.gather(*await mgr.stop(), return_exceptions=True)


async def test_stopping_the_manager_cancels_the_pending_retry():
    net = _Net(failures=1)
    mgr = await _manager(net)
    await mgr.download("goodpeer", "album/01.flac")
    assert await _until(lambda: len(net.calls) >= 1)
    await asyncio.gather(*await mgr.stop(), return_exceptions=True)
    await asyncio.sleep(COOLDOWN * 2)
    assert len(net.calls) == 1
    assert not mgr._management_flags
