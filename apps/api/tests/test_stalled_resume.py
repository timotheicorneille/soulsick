"""A source taken back gets its full STALL_MINUTES from that moment, not from when its files
were first asked: approved again after a failure, it was dropped at the next check."""

from datetime import UTC, datetime, timedelta

from fakes import TRACKS, peer_with_album
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import Transfer


async def _album_held(client, core, engine):
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post(
        "/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"}
    )
    groups = (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
    rid = next(rg["id"] for rg in groups if rg["title"] == "Nevermind")
    engine.results = [peer_with_album()]
    for i, (title, _) in enumerate(TRACKS):
        engine.script[f"{i + 1:02d} - {title}.flac"] = ["hold"] * 3  # never sends, even asked again
    await core.hunter.hunt(rid)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")
    return rid, cid


async def _age_transfers(core, hours=2):
    async with core.db.session() as s:
        for t in (await s.execute(select(Transfer))).scalars():
            t.created_at = datetime.now(UTC) - timedelta(hours=hours)
        await s.commit()


async def _status(client, rid, cid):
    album = (await client.get(f"/api/albums/{rid}")).json()
    return next(c for c in album["candidates"] if c["id"] == cid)["status"]


async def test_a_source_approved_again_is_not_dropped_at_once(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, cid = await _album_held(client, core, engine)
    await _age_transfers(core)
    await core.hunter.check_stalled()
    assert await _status(client, rid, cid) == "failed", "30 min without a byte: dropped"

    # You give that peer another chance: its files are asked again, on their old rows
    r = await client.post(f"/api/candidates/{cid}/approve")
    assert r.json()["resumed"] is True
    await core.hunter.check_stalled()
    assert await _status(client, rid, cid) == "chosen", "it has 30 min from now, not from 2 h ago"


async def test_a_source_resumed_after_a_long_pause_is_not_dropped_at_once(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, cid = await _album_held(client, core, engine)
    async with core.db.session() as s:
        ids = [t.id for t in (await s.execute(select(Transfer))).scalars()]
    await client.post("/api/transfers/batch", json={"ids": ids, "action": "pause"})
    await _age_transfers(core)
    await core.hunter.check_stalled()
    assert await _status(client, rid, cid) == "chosen", "paused by you: not a stall"

    for tid in ids:
        await client.post(f"/api/transfers/{tid}/resume")
    await core.hunter.check_stalled()
    assert await _status(client, rid, cid) == "chosen", "it has 30 min from the resume"
