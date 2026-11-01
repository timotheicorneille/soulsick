"""Un fichier refusé est repris chez un autre pair ; un fichier annulé ne fait pas échouer sa source."""

from fakes import TRACKS, peer_with_album
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import Transfer, TransferState

FILE2 = f"02 - {TRACKS[1][0]}.flac"


async def _album(client, core, engine, peers: list[str]) -> tuple[int, dict]:
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album(p) for p in peers]
    core.hunter.retry_delays = [0, 0]
    await core.hunter.hunt(rid)
    cands = {c["username"]: c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"]}
    return rid, cands


async def _status(client, rid, want):
    async def check():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == want
    await wait_for(check)


async def test_fichier_refuse_repris_chez_un_autre_pair(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, cands = await _album(client, core, engine, ["goodpeer", "otherpeer"])
    engine.script[f"goodpeer:{FILE2}"] = ["fail", "fail", "fail"]
    await client.post(f"/api/candidates/{cands['goodpeer']['id']}/approve")

    await _status(client, rid, "imported")
    asked = [(u, p.rsplit("\\", 1)[-1]) for u, p, _ in engine.downloads if u == "otherpeer"]
    assert asked == [("otherpeer", FILE2)], "seule la piste refusée est reprise ailleurs"
    events = [e["kind"] for e in (await client.get("/api/events", params={"release_group_id": rid})).json()]
    assert "download.relayed" in events


async def test_personne_d_autre_ne_l_a_l_album_est_a_completer(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, cands = await _album(client, core, engine, ["goodpeer"])
    engine.script[f"goodpeer:{FILE2}"] = ["fail", "fail", "fail"]
    await client.post(f"/api/candidates/{cands['goodpeer']['id']}/approve")

    await _status(client, rid, "pending_approval")
    album = (await client.get(f"/api/albums/{rid}")).json()
    assert album["status_reason_t"]["key"] == "reason.incomplete_action", "à compléter, pas relancé ailleurs"
    assert {u for u, _, _ in engine.downloads} == {"goodpeer"}


async def test_fichier_annule_par_toi_pas_de_relais(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, cands = await _album(client, core, engine, ["goodpeer", "otherpeer"])
    engine.script[f"goodpeer:{FILE2}"] = ["hold"]
    await client.post(f"/api/candidates/{cands['goodpeer']['id']}/approve")

    async def held():
        async with core.db.session() as s:
            ts = list((await s.execute(select(Transfer).where(Transfer.release_group_id == rid))).scalars())
        return sum(t.state == TransferState.COMPLETE for t in ts) == 2

    await wait_for(held)
    async with core.db.session() as s:
        t2 = (await s.execute(select(Transfer).where(Transfer.remote_path.endswith(FILE2)))).scalar_one()
    assert (await client.post(f"/api/transfers/{t2.id}/cancel")).status_code == 200

    await _status(client, rid, "pending_approval")
    assert {u for u, _, _ in engine.downloads} == {"goodpeer"}, "tu l'as annulée : on ne va pas la chercher ailleurs"
    good = next(c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"] if c["username"] == "goodpeer")
    assert good["status"] == "downloaded", "sa source n'est pas en échec pour autant"
