"""Une demande restée sans réponse est signalée : un téléchargement lancé à la main depuis la
recherche, ou un fichier d'album que le pair n'a jamais accepté dans sa file."""

from datetime import datetime, timedelta

from fakes import peer_with_album
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401
from test_stuck import FILE2, _row, _update

from soulsick.models import Transfer, TransferState

MANUAL = "@@music\\Some Artist\\Some Album\\01 - Some Song.flac"


async def _age(core, minutes):
    """Le temps passe : les demandes, les arrivées et toutes les horloges suivies reculent d'autant."""
    back = timedelta(minutes=minutes)
    async with core.db.session() as s:
        for t in (await s.execute(select(Transfer))).scalars():
            t.created_at -= back
            if t.requested_at:
                t.requested_at -= back
            if t.completed_at:
                t.completed_at -= back
        await s.commit()
    for w in core.transfers._watch.values():
        for key, value in w.items():
            if isinstance(value, datetime):
                w[key] = value - back


async def _minute(core):
    """Ce que la boucle de la chasse fait chaque minute pour les téléchargements en cours."""
    await core.transfers.check_stuck()
    await core.hunter.check_silent()
    await core.hunter.check_stalled()


async def _stuck_events(client):
    return [e for e in (await client.get("/api/events")).json() if e["kind"] == "download.stuck"]


async def _manual_request(client, core, engine):
    """Un fichier demandé depuis la recherche à un pair qui ne répond pas : "Demande envoyée"."""
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    engine.script["mutepeer:01 - Some Song.flac"] = ["hold"]
    r = await client.post("/api/downloads", json={"username": "mutepeer", "files": [MANUAL]})
    assert r.status_code == 200, r.text
    async with core.db.session() as s:
        t = await s.get(Transfer, r.json()["ids"][0])
    assert t.candidate_id is None
    # Ce que dit aioslsk en ajoutant le transfert : en file chez nous, pas encore chez le pair
    await _update(core, t, TransferState.QUEUED)
    return t


async def test_telechargement_manuel_sans_reponse(ctx):  # noqa: F811
    """Avant, un téléchargement lancé à la main pouvait rester "Demande envoyée" sans fin,
    sans que rien ne le signale."""
    client, core, engine, env = ctx
    t = await _manual_request(client, core, engine)
    await _minute(core)
    await _age(core, 59)
    await _minute(core)
    assert (await _row(client, t.id))["stuck"] is False, "59 min : pas encore"

    await _age(core, 2)
    await _minute(core)
    row = await _row(client, t.id)
    assert row["stuck"] is True and row["stuck_reason"] == "no_answer" and row["stuck_since"]
    assert row["state"] == "queued", "seulement signalé, rien n'est annulé"
    await _minute(core)
    events = await _stuck_events(client)
    assert [e["transfer_id"] for e in events] == [t.id], "une seule entrée par épisode"
    assert events[0]["release_group_id"] is None and events[0]["candidate_id"] is None
    assert (await client.get("/api/status")).json()["stuck_transfers"] == 1

    # Le pair finit par accepter la demande : plus bloqué
    await _update(core, t, TransferState.QUEUED, queued=True, place=3)
    assert (await _row(client, t.id))["stuck"] is False
    await _minute(core)
    assert (await _row(client, t.id))["stuck"] is False

    # Il se déconnecte ensuite (aioslsk retire l'acceptation) : l'attente repart de là
    await _update(core, t, TransferState.QUEUED)
    await _minute(core)
    assert (await _row(client, t.id))["stuck"] is False
    await _age(core, 61)
    await _minute(core)
    assert (await _row(client, t.id))["stuck_reason"] == "no_answer"
    assert len(await _stuck_events(client)) == 2


async def test_reglage_a_zero_jamais(ctx):  # noqa: F811
    client, core, engine, env = ctx
    t = await _manual_request(client, core, engine)
    cfg = (await client.get("/api/settings")).json()
    assert cfg["hunter"]["stuck_no_answer_minutes"] == 60
    cfg["hunter"]["stuck_no_answer_minutes"] = 0
    assert (await client.put("/api/settings", json=cfg)).status_code == 200
    await _minute(core)
    await _age(core, 120)
    await _minute(core)
    assert (await _row(client, t.id))["stuck"] is False, "0 = jamais"

    cfg["hunter"]["stuck_no_answer_minutes"] = 90
    assert (await client.put("/api/settings", json=cfg)).status_code == 200
    await _minute(core)
    assert (await _row(client, t.id))["stuck_reason"] == "no_answer"


async def _album_file2_never_accepted(client, core, engine, peers):
    """goodpeer livre les pistes 1 et 3, la demande de la 2 ne lui parvient jamais."""
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    artist = {"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"}
    aid = (await client.post("/api/artists", json=artist)).json()["id"]
    albums = (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
    rid = next(rg["id"] for rg in albums if rg["title"] == "Nevermind")
    engine.results = [peer_with_album(p) for p in peers]
    engine.script[f"goodpeer:{FILE2}"] = ["hold"]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    good = next(c for c in cands if c["username"] == "goodpeer")
    await client.post(f"/api/candidates/{good['id']}/approve")

    async def two_done():
        async with core.db.session() as s:
            rows = await s.execute(select(Transfer).where(Transfer.candidate_id == good["id"]))
            return sum(t.state == "complete" for t in rows.scalars()) == 2

    await wait_for(two_done)
    async with core.db.session() as s:
        t2 = (await s.execute(select(Transfer).where(Transfer.remote_path.endswith(FILE2)))).scalar_one()
    await _update(core, t2, TransferState.QUEUED)
    return rid, good, t2


async def _candidate(client, rid, cid):
    return next(c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"] if c["id"] == cid)


async def test_fichier_d_album_jamais_accepte(ctx):  # noqa: F811
    """Personne d'autre n'a la piste : la source est gardée, et la demande sans réponse est
    signalée au lieu d'attendre en silence."""
    client, core, engine, env = ctx
    rid, good, t2 = await _album_file2_never_accepted(client, core, engine, peers=("goodpeer",))
    await _minute(core)
    await _age(core, 31)
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen", "personne d'autre : gardée"
    assert (await _row(client, t2.id))["stuck"] is False

    await _age(core, 30)
    await _minute(core)
    row = await _row(client, t2.id)
    assert row["stuck_reason"] == "no_answer"
    events = await _stuck_events(client)
    links = [(e["transfer_id"], e["release_group_id"], e["candidate_id"]) for e in events]
    assert links == [(t2.id, rid, good["id"])], "relié à son album et à sa source"


async def test_source_remplacee_avant_d_etre_signalee(ctx):  # noqa: F811
    """Un autre pair a la piste : au bout de silent_source_minutes (30), la source est remplacée.
    Le signalement, plus tardif (60), ne double pas ce remplacement."""
    client, core, engine, env = ctx
    rid, good, t2 = await _album_file2_never_accepted(client, core, engine, peers=("goodpeer", "otherpeer"))
    await _minute(core)
    await _age(core, 31)
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "failed"
    await _minute(core)
    assert await _stuck_events(client) == []
    assert (await client.get("/api/status")).json()["stuck_transfers"] == 0
