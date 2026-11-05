"""A request to a peer always ends: one that hung held the peer's lock, and no other file was
ever asked from that peer until Soulsick restarted."""

import asyncio

import pytest
from aioslsk.exceptions import PeerConnectionError
from aioslsk.network.connection import ServerConnection
from aioslsk.network.network import ExpectedResponse
from aioslsk.protocol.messages import GetPeerAddress
from aioslsk.transfer.state import TransferState as S

from soulsick.slsk.engine import SafeNetwork, SafeTransferManager


class _Bus:
    async def emit(self, event):
        pass


def _network() -> SafeNetwork:
    net = SafeNetwork.__new__(SafeNetwork)
    net._expected_response_futures = []
    net._event_bus = _Bus()
    net._MESSAGE_MAP = {}
    return net


def _address_future() -> ExpectedResponse:
    return ExpectedResponse(ServerConnection, GetPeerAddress.Response, fields={"username": "piaga"})


async def test_an_answer_reaches_every_waiting_request_even_after_a_done_one():
    net = _network()
    cancelled, waiting = _address_future(), _address_future()
    net._expected_response_futures += [cancelled, waiting]
    # A direct connection that lost the race: cancelled, its cleanup not run yet
    cancelled.cancel()
    answer = GetPeerAddress.Response(username="piaga", ip="176.65.89.192", port=50300)
    await net.on_message_received(answer, ServerConnection.__new__(ServerConnection))
    assert waiting.done()
    assert waiting.result()[1] is answer


async def test_an_address_the_server_never_gives_ends_in_a_connection_error():
    class Server:
        async def send_message(self, message):
            pass  # the answer is lost

    net = _network()
    net.server_connection = Server()
    net.PEER_ADDRESS_TIMEOUT = 0.05
    with pytest.raises(PeerConnectionError):
        await net._get_peer_address("piaga")
    assert net._expected_response_futures == [] or all(f.done() for f in net._expected_response_futures)


async def test_a_queue_request_that_never_ends_frees_the_peer():
    class Net:
        def __init__(self):
            self.calls = 0

        async def send_peer_messages(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()  # never answers

    class State:
        VALUE = S.QUEUED

        def __init__(self):
            self.queued = 0

        async def queue(self, remotely=False):
            self.queued += 1

    class T:
        username, remote_path, remotely_queued = "piaga", "x.flac", False

        def __init__(self):
            self.state = State()

        def increase_queue_attempts(self):
            pass

        def reset_queue_attempts(self):
            pass

    mgr = SafeTransferManager.__new__(SafeTransferManager)
    mgr._network = Net()
    mgr.request_management_cycle = lambda flag: None
    mgr.QUEUE_REQUEST_TIMEOUT = 0.05
    mgr.PEER_FAILURE_COOLDOWN = 0
    stuck, nxt = T(), T()
    await asyncio.wait_for(asyncio.gather(mgr._queue_remotely(stuck), mgr._queue_remotely(nxt)), 2)
    # The hung request gives up and goes back to the queue; the next file is asked
    assert stuck.state.queued == 1 and not stuck.remotely_queued
    assert nxt.remotely_queued
    assert mgr._network.calls == 2
