"""Prendre une piste chez n'importe quel pair, même en plein téléchargement."""

from fakes import TRACKS, peer_with_album
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import Transfer, TransferState
from soulsick.scoring import PeerResult

FILE2 = f"02 - {TRACKS[1][0]}.flac"


async def _waiting_on_file2(client, core, engine, extra: list[PeerResult]):
    """goodpeer livre les pistes 1 et 3, la 2 reste en file chez lui."""
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album("goodpeer"), *extra]
    engine.script[f"goodpeer:{FILE2}"] = ["hold"]
    await core.hunter.hunt(rid)
    cands = {c["username"]: c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"]}
    await client.post(f"/api/candidates/{cands['goodpeer']['id']}/approve")

    async def two_done():
        async with core.db.session() as s:
            ts = list((await s.execute(select(Transfer).where(Transfer.release_group_id == rid))).scalars())
        return sum(t.state == TransferState.COMPLETE for t in ts) == 2

    await wait_for(two_done)
    cov = (await client.get(f"/api/albums/{rid}/coverage")).json()
    track2 = next(t for t in cov["tracks"] if t["title"] == TRACKS[1][0])
    return rid, cands, track2


async def test_prendre_une_piste_en_file_chez_un_autre_pair(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, cands, track2 = await _waiting_on_file2(client, core, engine, [peer_with_album("otherpeer")])
    assert [h["username"] for h in track2["holders"]] == ["otherpeer"], "pas goodpeer, qui l'a déjà en file"

    r = await client.post(f"/api/albums/{rid}/tracks/{track2['track_id']}/take",
                          json={"candidate_id": cands["otherpeer"]["id"]})
    assert r.status_code == 200, r.text

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    asked = [(u, p.rsplit("\\", 1)[-1]) for u, p, _ in engine.downloads if u == "otherpeer"]
    assert asked == [("otherpeer", FILE2)]
    async with core.db.session() as s:
        old = (await s.execute(select(Transfer).where(
            Transfer.username == "goodpeer", Transfer.remote_path.endswith(FILE2),
        ))).scalar_one()
    assert old.state == TransferState.ABORTED
    good = next(c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"] if c["username"] == "goodpeer")
    assert good["status"] != "failed", "goodpeer a livré le reste : pas en échec"


async def test_prendre_chez_un_pair_qui_n_a_pas_la_piste(ctx):  # noqa: F811
    client, core, engine, env = ctx
    partial = peer_with_album("partial")
    partial.files = [f for f in partial.files if not f.filename.endswith(FILE2)]
    rid, cands, track2 = await _waiting_on_file2(client, core, engine, [partial])

    r = await client.post(f"/api/albums/{rid}/tracks/{track2['track_id']}/take",
                          json={"candidate_id": cands["partial"]["id"]})
    assert r.status_code == 400
    async with core.db.session() as s:
        t2 = (await s.execute(select(Transfer).where(Transfer.remote_path.endswith(FILE2)))).scalar_one()
    assert t2.state == TransferState.QUEUED, "rien n'est annulé"
