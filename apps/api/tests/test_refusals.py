"""Refus d'un pair : un refus définitif n'est pas réessayé, un refus passager est relancé sans
attendre les autres fichiers de la source."""

from fakes import TRACKS, peer_with_album
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import Transfer

FILE2 = f"02 - {TRACKS[1][0]}.flac"


async def _rows(core):
    async with core.db.session() as s:
        return list((await s.execute(select(Transfer).order_by(Transfer.id))).scalars())


async def _status(client, rid):
    return (await client.get(f"/api/albums/{rid}")).json()["status"]


async def _downloading(client, core, engine, mode="validation"):
    """goodpeer et otherpeer ont l'album ; goodpeer est approuvé (ou pris tout seul en auto)."""
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    artist = {"mbid": "x", "mode": mode, "primary_types": ["album"], "monitor": "all"}
    aid = (await client.post("/api/artists", json=artist)).json()["id"]
    albums = (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
    rid = next(rg["id"] for rg in albums if rg["title"] == "Nevermind")
    engine.results = [peer_with_album("goodpeer"), peer_with_album("otherpeer")]
    core.hunter.retry_delays = [0.05, 0.05]
    await core.hunter.hunt(rid)
    if mode == "validation":
        cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
        good = next(c for c in cands if c["username"] == "goodpeer")
        await client.post(f"/api/candidates/{good['id']}/approve")
    return rid


def _asked(engine, user, name):
    return sum(1 for u, p, _ in engine.downloads if u == user and p.endswith(name))


async def test_refus_definitif_pas_reessaye_la_piste_est_reprise_ailleurs(ctx):  # noqa: F811
    """Refus "File not shared." : redemander donnerait la même réponse. Avant : 3 essais, 2 min 30."""
    client, core, engine, env = ctx
    engine.script[f"goodpeer:{FILE2}"] = ["fail:File not shared."]
    rid = await _downloading(client, core, engine)

    async def imported():
        return await _status(client, rid) == "imported"

    await wait_for(imported)
    assert _asked(engine, "goodpeer", FILE2) == 1, "une seule demande, pas de nouvel essai"
    assert _asked(engine, "otherpeer", FILE2) == 1, "la piste vient d'un autre pair"
    second = next(t for t in await _rows(core) if t.username == "goodpeer" and t.remote_path.endswith(FILE2))
    assert second.attempts == 1


async def test_source_qui_refuse_tout_pour_de_bon_la_suivante_part_aussitot(ctx):  # noqa: F811
    client, core, engine, env = ctx
    for i, (title, _) in enumerate(TRACKS):
        engine.script[f"goodpeer:{i + 1:02d} - {title}.flac"] = ["fail:File not shared."]
    rid = await _downloading(client, core, engine, mode="auto")

    async def imported():
        return await _status(client, rid) == "imported"

    await wait_for(imported)
    assert sum(1 for u, _, _ in engine.downloads if u == "goodpeer") == 3, "aucun nouvel essai chez goodpeer"
    assert sum(1 for u, _, _ in engine.downloads if u == "otherpeer") == 3
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    good = next(c for c in cands if c["username"] == "goodpeer")
    assert good["reject_reason_t"]["key"] == "reason.refused", "le motif ne parle pas de 3 essais"


async def test_refus_passager_relance_sans_attendre_les_autres_fichiers(ctx):  # noqa: F811
    """La piste 2 est refusée une fois pendant que la 3 attend encore chez le pair : elle est
    redemandée tout de suite. Avant : seulement une fois tous les autres fichiers terminés."""
    client, core, engine, env = ctx
    engine.script[f"goodpeer:{FILE2}"] = ["fail"]
    engine.script[f"goodpeer:03 - {TRACKS[2][0]}.flac"] = ["hold"]
    await _downloading(client, core, engine)

    async def second_received():
        rows = [t for t in await _rows(core) if t.remote_path.endswith(FILE2)]
        return bool(rows) and rows[0].state == "complete"

    await wait_for(second_received)
    assert _asked(engine, "goodpeer", FILE2) == 2
    third = next(t for t in await _rows(core) if t.remote_path.endswith(f"03 - {TRACKS[2][0]}.flac"))
    assert third.state == "queued", "la 3 attend toujours : rien d'autre n'a bougé"
    assert _asked(engine, "otherpeer", FILE2) == 0
