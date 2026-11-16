"""Pause / reprise d'un téléchargement."""

from datetime import UTC, datetime, timedelta

from fakes import TRACKS, peer_with_album
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import Transfer

FILE2 = f"02 - {TRACKS[1][0]}.flac"


async def test_pause_puis_reprise(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album()]
    engine.script[FILE2] = ["hold"]  # le 2e fichier reste en file chez le pair
    await core.hunter.hunt(rid)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")

    async def rows():
        async with core.db.session() as s:
            return list((await s.execute(select(Transfer).order_by(Transfer.id))).scalars())

    async def two_done():
        return sum(t.state == "complete" for t in await rows()) == 2

    await wait_for(two_done)
    t2 = next(t for t in await rows() if t.remote_path.endswith(FILE2))

    assert (await client.post(f"/api/transfers/{t2.id}/pause")).status_code == 200
    assert FILE2 in engine.paused[-1]
    t2 = next(t for t in await rows() if t.id == t2.id)
    assert t2.state == "paused"

    # Une pause n'est pas une panne : pas d'abandon pour inactivité, même longtemps après
    async with core.db.session() as s:
        for t in (await s.execute(select(Transfer))).scalars():
            t.created_at = datetime.now(UTC) - timedelta(hours=3)
            t.started_at = None
            t.bytes_transferred = 0
        await s.commit()
    await core.hunter.check_stalled()
    cand = next(c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"] if c["id"] == cid)
    assert cand["status"] == "chosen"

    # Reprise : le fichier est redemandé et l'album se termine
    assert (await client.post(f"/api/transfers/{t2.id}/resume")).status_code == 200

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    assert len(await rows()) == 3


async def test_abandon_a_source_that_never_delivers(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album(), peer_with_album("otherpeer")]
    engine.script[FILE2] = ["hold"]
    engine.script[f"03 - {TRACKS[2][0]}.flac"] = ["hold"]
    await core.hunter.hunt(rid)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")

    async def one_done():
        async with core.db.session() as s:
            return sum(t.state == "complete" for t in (await s.execute(select(Transfer))).scalars()) == 1

    await wait_for(one_done)
    async with core.db.session() as s:
        held = [t for t in (await s.execute(select(Transfer))).scalars() if t.state != "complete"]
    await client.post(f"/api/transfers/{held[0].id}/pause")

    r = await client.post(f"/api/albums/{rid}/cancel")
    assert r.json()["cancelled"] == 2

    album = (await client.get(f"/api/albums/{rid}")).json()
    assert album["status"] == "pending_approval", "another candidate is there: your choice"
    assert album["status_reason_t"]["key"] == "reason.abandoned_pick"
    cand = next(c for c in album["candidates"] if c["id"] == cid)
    assert cand["status"] == "failed" and cand["reject_reason_t"]["key"] == "reason.abandoned"
    states = sorted(t["state"] for t in album["transfers"])
    assert states == ["aborted", "aborted", "complete"], "what arrived is kept"
    # Nothing restarts on its own: no stall check, no fallback
    await core.hunter.check_stalled()
    assert (await client.get(f"/api/albums/{rid}")).json()["status"] == "pending_approval"


async def test_batch_actions_on_transfers(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "auto", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album(), peer_with_album("otherpeer")]
    for title, _ in TRACKS:
        engine.script[f"{TRACKS.index((title, _)) + 1:02d} - {title}.flac"] = ["hold"]
    await core.hunter.hunt(rid)
    async with core.db.session() as s:
        ids = [t.id for t in (await s.execute(select(Transfer))).scalars()]
    assert len(ids) == 3

    r = await client.post("/api/transfers/batch", json={"ids": ids, "action": "pause"})
    assert r.json()["done"] == 3
    # A paused, never-started file is cancelled for real (closed here even if the engine forgot it)
    r = await client.post("/api/transfers/batch", json={"ids": ids[:1], "action": "cancel"})
    assert r.json()["done"] == 1
    album = (await client.get(f"/api/albums/{rid}")).json()
    assert sorted(t["state"] for t in album["transfers"]) == ["aborted", "paused", "paused"]

    # Cancelling every remaining file of the source drops it, without switching to the other peer
    r = await client.post("/api/transfers/batch", json={"ids": ids[1:], "action": "cancel"})
    assert r.json()["done"] == 2
    album = (await client.get(f"/api/albums/{rid}")).json()
    assert album["status"] == "pending_approval"
    assert {c["username"] for c in album["candidates"] if c["status"] == "chosen"} == set()


async def test_a_source_waiting_in_the_peer_queue_is_not_dropped(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "auto", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album()]
    for i, (title, _) in enumerate(TRACKS):
        engine.script[f"{i + 1:02d} - {title}.flac"] = ["hold"]
    await core.hunter.hunt(rid)
    async with core.db.session() as s:
        for t in (await s.execute(select(Transfer))).scalars():
            t.created_at = datetime.now(UTC) - timedelta(hours=2)
            t.remotely_queued = True  # accepted, position 20 in the peer's queue
            t.place_in_queue = 20
        await s.commit()
    await core.hunter.check_stalled()
    chosen = [c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"] if c["status"] == "chosen"]
    assert len(chosen) == 1, "waiting our turn is not a stall"


async def test_a_cancelled_transfer_stays_cancelled(ctx):  # noqa: F811
    """The engine may still report on a file after you cancelled it: the row stays cancelled."""
    from soulsick.models import TransferState

    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album()]
    engine.script[FILE2] = ["hold"]
    await core.hunter.hunt(rid)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")

    async def rows():
        async with core.db.session() as s:
            return list((await s.execute(select(Transfer))).scalars())

    async def held():
        return any(t.remote_path.endswith(FILE2) and t.state == "queued" for t in await rows())

    await wait_for(held)
    t2 = next(t for t in await rows() if t.remote_path.endswith(FILE2))
    await client.post(f"/api/transfers/{t2.id}/pause")
    assert (await client.post("/api/transfers/batch", json={"ids": [t2.id], "action": "cancel"})).json()["done"] == 1

    # Late updates from the engine (the peer finally answers): ignored
    for state in (TransferState.QUEUED, TransferState.PAUSED, TransferState.TRANSFERRING):
        await engine.emit("goodpeer", t2.remote_path, t2.local_path, state)
    t2 = next(t for t in await rows() if t.id == t2.id)
    assert t2.state == "aborted"
    listed = {t["id"]: t["state"] for t in (await client.get("/api/transfers?direction=download")).json()}
    assert listed[t2.id] == "aborted"

    # Retrying is an explicit choice: then it lives again
    await client.post(f"/api/transfers/{t2.id}/retry")
    await engine.emit("goodpeer", t2.remote_path, t2.local_path, TransferState.TRANSFERRING)
    assert next(t for t in await rows() if t.id == t2.id).state == "transferring"


async def test_aioslsk_requeue_never_restarts_finished_or_stopped_files():
    """aioslsk's queue failure path must not put back a COMPLETE / PAUSED / ABORTED file."""
    from aioslsk.exceptions import PeerConnectionError
    from aioslsk.transfer.state import TransferState as S

    from soulsick.slsk.engine import SafeTransferManager

    class Net:
        async def send_peer_messages(self, *a, **k):
            raise PeerConnectionError("race")

    class State:
        def __init__(self, value):
            self.VALUE = value
            self.queued = 0

        async def queue(self, remotely=False):
            self.queued += 1

    class T:
        username, remote_path = "peer", "a\\b.flac"

        def __init__(self, value):
            self.state = State(value)
            self.attempts = 0

        def increase_queue_attempts(self):
            self.attempts += 1

    mgr = SafeTransferManager.__new__(SafeTransferManager)
    mgr._network = Net()
    for value, expected in ((S.COMPLETE, 0), (S.PAUSED, 0), (S.ABORTED, 0), (S.QUEUED, 1)):
        t = T(value)
        await mgr._queue_remotely(t)
        # Only a file still waiting is retried (later, see test_requeue.py); none is put back
        assert t.state.queued == 0 and t.attempts == expected, value


async def test_a_received_file_is_never_downloaded_again(ctx):  # noqa: F811
    from soulsick.models import TransferState

    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "auto", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album()]
    await core.hunter.hunt(rid)

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    async with core.db.session() as s:
        t = (await s.execute(select(Transfer))).scalars().first()
    # The engine re-queues and re-downloads it elsewhere (the aioslsk bug): the row doesn't move
    for state in (TransferState.QUEUED, TransferState.TRANSFERRING, TransferState.COMPLETE):
        await engine.emit("goodpeer", t.remote_path, "/downloads/manual/dup (1).flac", state)
    async with core.db.session() as s:
        again = await s.get(Transfer, t.id)
    assert again.state == "complete" and again.local_path == t.local_path
    assert (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"


async def test_queue_requests_to_one_peer_go_one_at_a_time():
    """N files from the same peer: the queue requests are sent in sequence (no address-lookup race),
    and after a failure the peer's other files don't each wait for the same timeout."""
    import asyncio

    from aioslsk.exceptions import PeerConnectionError
    from aioslsk.transfer.state import TransferState as S

    from soulsick.slsk.engine import SafeTransferManager

    class Net:
        def __init__(self, fail=False):
            self.active = self.peak = self.calls = 0
            self.fail = fail

        async def send_peer_messages(self, *a, **k):
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            if self.fail:
                raise PeerConnectionError("unreachable")

    class State:
        VALUE = S.QUEUED

        async def queue(self, remotely=False):
            pass

    class T:
        username, remote_path, remotely_queued = "zeow", "x.flac", False

        def __init__(self):
            self.state = State()

        def increase_queue_attempts(self):
            pass

        def reset_queue_attempts(self):
            pass

    for fail, calls in ((False, 33), (True, 1)):
        mgr = SafeTransferManager.__new__(SafeTransferManager)
        mgr._network = Net(fail)
        mgr.request_management_cycle = lambda flag: None
        ts = [T() for _ in range(33)]
        await asyncio.gather(*(mgr._queue_remotely(t) for t in ts))
        assert mgr._network.peak == 1
        assert mgr._network.calls == calls
        assert all(t.remotely_queued for t in ts) is (not fail)


async def test_a_received_file_stored_elsewhere_is_brought_back_for_import(ctx):  # noqa: F811
    """A file of the source that ended up in downloads/manual (old aioslsk re-queue) is moved back, not refused."""
    import shutil

    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album()]
    engine.script[FILE2] = ["hold"]
    await core.hunter.hunt(rid)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")

    async def two_done():
        async with core.db.session() as s:
            return sum(t.state == "complete" for t in (await s.execute(select(Transfer))).scalars()) == 2

    await wait_for(two_done)
    # The last file arrives, but in downloads/manual (where the aioslsk bug used to put it)
    async with core.db.session() as s:
        t2 = next(t for t in (await s.execute(select(Transfer))).scalars() if t.remote_path.endswith(FILE2))
    stray = env.downloads_dir / "manual" / FILE2
    stray.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(next((env.downloads_dir).rglob("01 - *.flac")), stray)
    from fakes import TRACKS as _T, tiny_flac
    tiny_flac(stray, _T[1][1] // 1000)
    from soulsick.models import TransferState
    await engine.emit("goodpeer", t2.remote_path, str(stray), TransferState.COMPLETE)

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    assert not stray.exists()
    assert (env.music_dir / "Nirvana" / "1991 - Nevermind" / "02 - In Bloom.flac").exists()
