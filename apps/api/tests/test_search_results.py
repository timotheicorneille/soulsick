"""Manual search results: sort and filter happen before the 300-folder cut."""

import time

import test_flow
from fakes import peer_with_album

from soulsick.scoring import PeerResult, RemoteFile
from soulsick.slsk.engine import LiveSearch

ctx = test_flow.ctx
TICKET = 4242


def folder(
    user: str, directory: str, *, free: bool = True, speed: int = 100_000, queue: int = 0, size: int = 10,
) -> PeerResult:
    files = [RemoteFile(f"@@music\\{directory}\\{i:02d} - Track.flac", size=size) for i in range(1, 4)]
    return PeerResult(user, free, speed, queue, files)


async def open_search(client, engine, results: list[PeerResult]) -> None:
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    live = LiveSearch(ticket=TICKET, query="nevermind", started=time.time(), results=results)
    engine.searches[TICKET] = live


async def get(client, **params) -> dict:
    r = await client.get(f"/api/search/{TICKET}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def crowd(n: int) -> list[PeerResult]:
    """n folders that come first in queue order: free slot, empty queue, small."""
    return [folder(f"peer{i:03d}", f"Crowd\\Album {i:03d}") for i in range(n)]


async def test_size_sort_reaches_beyond_the_cut(ctx):
    client, _core, engine, _env = ctx
    big = folder("bigpeer", "Nirvana\\Nevermind (24bit)", free=False, queue=50, size=50_000_000)
    await open_search(client, engine, [*crowd(310), big])

    # Default order (free slot, queue): the big folder from a busy peer falls past the 300 kept
    snap = await get(client)
    assert snap["folder_count"] == 311
    assert snap["match_count"] == 311
    assert len(snap["folders"]) == 300
    assert all(f["username"] != "bigpeer" for f in snap["folders"])

    # Sorted by size, it comes first: the browser alone could never have shown it
    snap = await get(client, sort="size")
    assert snap["folders"][0]["username"] == "bigpeer"
    assert len(snap["folders"]) == 300


async def test_speed_sort(ctx):
    client, _core, engine, _env = ctx
    await open_search(client, engine, [
        folder("slowpeer", "A\\Slow", speed=10_000),
        folder("fastpeer", "A\\Fast", free=False, queue=9, speed=9_000_000),
        folder("midpeer", "A\\Mid", speed=500_000),
    ])
    assert [f["username"] for f in (await get(client))["folders"]] == ["midpeer", "slowpeer", "fastpeer"]
    by_speed = (await get(client, sort="speed"))["folders"]
    assert [f["username"] for f in by_speed] == ["fastpeer", "midpeer", "slowpeer"]


async def test_filter_before_the_cut(ctx):
    client, _core, engine, _env = ctx
    target = folder("lastpeer", "Nirvana\\Bleach", free=False, queue=80)
    await open_search(client, engine, [*crowd(310), target, peer_with_album()])

    snap = await get(client, filter="BLEACH")
    assert [f["username"] for f in snap["folders"]] == ["lastpeer"]
    assert snap["folder_count"] == 312
    assert snap["match_count"] == 1

    # Every word must match, "-word" excludes; the peer name counts too
    snap = await get(client, filter="nirvana -bleach")
    assert [f["directory"] for f in snap["folders"]] == ["@@music\\Nirvana\\Nevermind"]
    snap = await get(client, filter="goodpeer")
    assert [f["username"] for f in snap["folders"]] == ["goodpeer"]
    snap = await get(client, filter="nirvana unplugged")
    assert snap["folders"] == [] and snap["match_count"] == 0


async def test_unknown_sort_refused(ctx):
    client, _core, engine, _env = ctx
    await open_search(client, engine, [peer_with_album()])
    assert (await client.get(f"/api/search/{TICKET}", params={"sort": "random"})).status_code == 422
