"""Un album dont toutes les pistes sont reçues est rangé, quel que soit l'état de ses sources."""

from fakes import TRACKS, peer_with_album
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import AlbumStatus, Candidate, CandidateStatus, ReleaseGroup, Transfer, TransferState

FILE2 = f"02 - {TRACKS[1][0]}.flac"


async def _stuck_on_file2(client, core, engine):
    """goodpeer livre les pistes 1 et 3 ; la 2 reste en file chez lui."""
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album("goodpeer")]
    engine.script[f"goodpeer:{FILE2}"] = ["hold"]
    await core.hunter.hunt(rid)
    good = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]
    await client.post(f"/api/candidates/{good['id']}/approve")

    async def two_done():
        async with core.db.session() as s:
            ts = list((await s.execute(select(Transfer).where(Transfer.candidate_id == good["id"]))).scalars())
        return sum(t.state == TransferState.COMPLETE for t in ts) == 2

    await wait_for(two_done)
    return rid, good["id"]


async def _wedge(core, cid):
    """La piste 2 finit par arriver sans que la source ne soit jugée « terminée proprement » :
    elle garde un fichier annulé à côté (le genre d'état qui coinçait l'album)."""
    async with core.db.session() as s:
        t2 = (await s.execute(select(Transfer).where(Transfer.remote_path.endswith(FILE2)))).scalar_one()
        t2.state = TransferState.COMPLETE
        s.add(Transfer(
            direction="download", username="goodpeer", remote_path="@@music\\Nirvana\\Nevermind\\bonus.flac",
            state=TransferState.ABORTED, candidate_id=cid, release_group_id=t2.release_group_id,
        ))
        await s.commit()


async def test_tout_recu_l_album_est_range_meme_si_la_source_est_restee_choisie(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, cid = await _stuck_on_file2(client, core, engine)
    await _wedge(core, cid)
    async with core.db.session() as s:
        assert (await s.get(Candidate, cid)).status == CandidateStatus.CHOSEN

    assert await core.importer.import_if_ready(rid)
    assert (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"


async def test_une_piste_encore_en_file_l_album_attend(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, _ = await _stuck_on_file2(client, core, engine)
    assert not await core.importer.import_if_ready(rid)
    async with core.db.session() as s:
        assert (await s.get(ReleaseGroup, rid)).status == AlbumStatus.DOWNLOADING


async def test_le_filet_de_securite_range_un_album_coince(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, cid = await _stuck_on_file2(client, core, engine)
    await _wedge(core, cid)

    await core._ready_tick()
    assert (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"
