"""Téléchargements bloqués : détection, et changement de pair en gardant ce qui est arrivé."""

from datetime import timedelta

from fakes import TRACKS, peer_with_album
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import Transfer, TransferState
from soulsick.slsk.engine import TransferUpdate

FILE2 = f"02 - {TRACKS[1][0]}.flac"


async def _album_waiting_on_file2(client, core, engine):
    """goodpeer livre les pistes 1 et 3, la 2 reste chez lui ; otherpeer a tout l'album."""
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album("goodpeer"), peer_with_album("otherpeer")]
    engine.script[f"goodpeer:{FILE2}"] = ["hold"]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    good = next(c for c in cands if c["username"] == "goodpeer")
    other = next(c for c in cands if c["username"] == "otherpeer")
    await client.post(f"/api/candidates/{good['id']}/approve")

    async def two_done():
        async with core.db.session() as s:
            ts = list((await s.execute(select(Transfer).where(Transfer.candidate_id == good["id"]))).scalars())
        return sum(t.state == "complete" for t in ts) == 2

    await wait_for(two_done)
    async with core.db.session() as s:
        t2 = (await s.execute(select(Transfer).where(Transfer.remote_path.endswith(FILE2)))).scalar_one()
    return rid, good, other, t2


async def _update(core, t2, state, transferred=0, queued=False, place=None):
    await core.transfers.on_update(TransferUpdate(
        direction="download", username=t2.username, remote_path=t2.remote_path, local_path=t2.local_path,
        size=100, bytes_transferred=transferred, speed=0.0, state=state, place_in_queue=place, error=None,
        state_changed=True, remotely_queued=queued,
    ))


async def _row(client, tid):
    return next(t for t in (await client.get("/api/transfers")).json() if t["id"] == tid)


def _backdate(core, tid, key, minutes):
    core.transfers._watch[tid][key] -= timedelta(minutes=minutes)


async def _stuck_events(client, rid):
    return [e for e in (await client.get("/api/events", params={"release_group_id": rid})).json() if e["kind"] == "download.stuck"]


async def test_plus_de_progression(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, other, t2 = await _album_waiting_on_file2(client, core, engine)
    await _update(core, t2, TransferState.TRANSFERRING, 10)
    await core.transfers.check_stuck()
    assert (await _row(client, t2.id))["stuck"] is False, "il vient de démarrer"

    _backdate(core, t2.id, "progress_at", 11)
    await core.transfers.check_stuck()
    row = await _row(client, t2.id)
    assert row["stuck"] is True and row["stuck_reason"] == "no_progress" and row["stuck_since"]
    await core.transfers.check_stuck()
    assert len(await _stuck_events(client, rid)) == 1, "une seule entrée par épisode"

    # Les octets repartent : plus bloqué
    await _update(core, t2, TransferState.TRANSFERRING, 20)
    assert (await _row(client, t2.id))["stuck"] is False


async def test_file_du_pair_figee(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, other, t2 = await _album_waiting_on_file2(client, core, engine)
    await _update(core, t2, TransferState.QUEUED, queued=True, place=5)
    _backdate(core, t2.id, "queue_since", 31)
    await core.transfers.check_stuck()
    row = await _row(client, t2.id)
    assert row["stuck_reason"] == "queue_frozen"

    # La place baisse : la file avance
    await _update(core, t2, TransferState.QUEUED, queued=True, place=4)
    assert (await _row(client, t2.id))["stuck"] is False
    await core.transfers.check_stuck()
    assert (await _row(client, t2.id))["stuck"] is False


async def test_relances_et_reglage(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, other, t2 = await _album_waiting_on_file2(client, core, engine)
    async with core.db.session() as s:
        (await s.get(Transfer, t2.id)).attempts = 2
        await s.commit()
    await core.transfers.check_stuck()
    assert (await _row(client, t2.id))["stuck_reason"] == "retrying"

    cfg = (await client.get("/api/settings")).json()
    cfg["hunter"]["stuck_after_attempts"] = 0
    assert (await client.put("/api/settings", json=cfg)).status_code == 200
    await core.transfers.check_stuck()
    assert (await _row(client, t2.id))["stuck"] is False


async def test_pause_efface_l_etat_bloque(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, other, t2 = await _album_waiting_on_file2(client, core, engine)
    await _update(core, t2, TransferState.TRANSFERRING, 10)
    _backdate(core, t2.id, "progress_at", 11)
    await core.transfers.check_stuck()
    assert (await client.post(f"/api/transfers/{t2.id}/pause")).status_code == 200
    await core.transfers.check_stuck()
    assert (await _row(client, t2.id))["stuck"] is False


async def test_alternatives_et_changement_de_pair(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, other, t2 = await _album_waiting_on_file2(client, core, engine)
    alts = (await client.get(f"/api/candidates/{good['id']}/alternatives")).json()
    assert alts["missing"] == 1
    assert [a["username"] for a in alts["alternatives"]] == ["otherpeer"]
    assert alts["alternatives"][0]["tracks"] == 1
    before = len(engine.downloads)

    r = await client.post(f"/api/candidates/{good['id']}/switch", json={"to": other["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["files"] == 1
    asked = engine.downloads[before:]
    assert [(u, p.rsplit("\\", 1)[-1]) for u, p, _ in asked] == [("otherpeer", FILE2)], "seule la piste manquante"
    old = next(c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"] if c["id"] == good["id"])
    assert old["reject_reason_t"]["key"] == "reason.switched"

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    events = (await client.get("/api/events", params={"release_group_id": rid})).json()
    assert any(e["kind"] == "download.switched" for e in events)
    assert not any(e["kind"] == "download.abandoned" for e in events)


async def test_changement_avec_nouvelle_recherche(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, other, t2 = await _album_waiting_on_file2(client, core, engine)
    r = await client.post(f"/api/candidates/{good['id']}/switch", json={"search": True})
    assert r.status_code == 200, r.text

    async def searched():
        events = (await client.get("/api/events", params={"release_group_id": rid})).json()
        return any(e["kind"] == "download.switched_search" for e in events)

    await wait_for(searched)
    async with core.db.session() as s:
        assert (await s.get(Transfer, t2.id)).state == TransferState.ABORTED


async def test_changement_refuse(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, other, t2 = await _album_waiting_on_file2(client, core, engine)
    # otherpeer n'est pas une source active
    assert (await client.post(f"/api/candidates/{other['id']}/switch", json={"to": good["id"]})).status_code == 400
    # Cible inconnue
    assert (await client.post(f"/api/candidates/{good['id']}/switch", json={"to": 9999})).status_code == 400
    # Ni cible ni recherche
    assert (await client.post(f"/api/candidates/{good['id']}/switch", json={})).status_code == 422



async def test_retour_a_un_pair_deja_quitte_l_album_est_range(ctx):  # noqa: F811
    """Aller-retour entre deux pairs qui tardent : une fois tout reçu, l'album est rangé.
    Le pair repris garde des fichiers annulés d'avant (livrés depuis par l'autre pair) :
    ils ne doivent pas le faire échouer."""
    client, core, engine, env = ctx
    file3 = f"03 - {TRACKS[2][0]}.flac"
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album("goodpeer"), peer_with_album("otherpeer")]
    # goodpeer n'envoie que la piste 1 ; otherpeer, que la 3
    engine.script[f"goodpeer:{FILE2}"] = ["hold"]
    engine.script[f"goodpeer:{file3}"] = ["hold"]
    engine.script[f"otherpeer:{FILE2}"] = ["hold"]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    good = next(c for c in cands if c["username"] == "goodpeer")
    other = next(c for c in cands if c["username"] == "otherpeer")
    await client.post(f"/api/candidates/{good['id']}/approve")

    def complete(cid, n):
        async def check():
            async with core.db.session() as s:
                ts = list((await s.execute(select(Transfer).where(Transfer.candidate_id == cid))).scalars())
            return sum(t.state == "complete" for t in ts) == n
        return check

    await wait_for(complete(good["id"], 1))
    r = await client.post(f"/api/candidates/{good['id']}/switch", json={"to": other["id"]})
    assert r.status_code == 200 and r.json()["files"] == 2, r.text
    await wait_for(complete(other["id"], 1))

    # Retour à goodpeer pour la piste 2, qu'il envoie cette fois ; sa piste 3 annulée ne compte plus
    r = await client.post(f"/api/candidates/{other['id']}/switch", json={"to": good["id"]})
    assert r.status_code == 200 and r.json()["files"] == 1, r.text

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
