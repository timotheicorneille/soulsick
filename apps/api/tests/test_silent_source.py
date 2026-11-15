"""Une source qui n'envoie plus rien est remplacée toute seule : les pistes qu'elle doit encore
sont demandées à un autre pair qui les a, ce qui est arrivé est gardé."""

from datetime import timedelta

from fakes import TRACKS, peer_with_album
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401
from test_stuck import FILE2, _album_waiting_on_file2, _update

from soulsick.models import Transfer, TransferState

NAMES = [f"{i + 1:02d} - {title}.flac" for i, (title, _) in enumerate(TRACKS)]


async def _age(core, minutes=31):
    """Le temps passe : les demandes, les arrivées et la dernière activité vue reculent d'autant."""
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
        w["progress_at"] -= back
        if w["queue_since"]:
            w["queue_since"] -= back


async def _minute(core):
    """Ce que la boucle de la chasse fait chaque minute pour les téléchargements en cours."""
    await core.transfers.check_stuck()
    await core.hunter.check_silent()


async def _candidate(client, rid, cid):
    return next(c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"] if c["id"] == cid)


async def _imported(client, rid):
    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)


async def _album_queued_at_goodpeer(client, core, engine, peers=("goodpeer", "otherpeer")):
    """goodpeer accepte les 3 pistes dans sa file (place 5) et n'envoie rien."""
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    artist = {"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"}
    aid = (await client.post("/api/artists", json=artist)).json()["id"]
    albums = (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
    rid = next(rg["id"] for rg in albums if rg["title"] == "Nevermind")
    engine.results = [peer_with_album(p) for p in peers]
    for name in NAMES:
        engine.script[f"goodpeer:{name}"] = ["hold"]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    good = next(c for c in cands if c["username"] == "goodpeer")
    await client.post(f"/api/candidates/{good['id']}/approve")
    async with core.db.session() as s:
        rows = list((await s.execute(select(Transfer).where(Transfer.candidate_id == good["id"]))).scalars())
    for t in rows:
        await _update(core, t, TransferState.QUEUED, queued=True, place=5)
    return rid, good, rows


async def test_file_acceptee_puis_muette(ctx):  # noqa: F811
    """Le pair accepte tout dans sa file et n'envoie jamais rien : avant, l'album attendait
    chez lui sans fin, il fallait changer de pair à la main."""
    client, core, engine, env = ctx
    rid, good, rows = await _album_queued_at_goodpeer(client, core, engine)
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen", "il vient d'accepter"

    await _age(core)
    await core.hunter.check_stalled()
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen", "en file : pas un pair absent"
    before = len(engine.downloads)
    await _minute(core)

    old = await _candidate(client, rid, good["id"])
    assert old["status"] == "failed" and old["reject_reason_t"]["key"] == "reason.silent"
    asked = engine.downloads[before:]
    assert sorted(p.rsplit("\\", 1)[-1] for _, p, _ in asked) == NAMES
    assert {u for u, _, _ in asked} == {"otherpeer"}
    await _imported(client, rid)
    events = (await client.get("/api/events", params={"release_group_id": rid})).json()
    assert [e for e in events if e["kind"] == "download.silent"]
    async with core.db.session() as s:
        left = list((await s.execute(select(Transfer).where(Transfer.candidate_id == good["id"]))).scalars())
    assert all(t.state == "aborted" and "cancelled by you" not in t.error for t in left)


async def test_source_livree_en_partie_puis_muette(ctx):  # noqa: F811
    """goodpeer livre les pistes 1 et 3 puis plus rien pour la 2 : seule la 2 est demandée
    ailleurs, les deux reçues restent."""
    client, core, engine, env = ctx
    rid, good, other, t2 = await _album_waiting_on_file2(client, core, engine)
    await _minute(core)
    await _age(core, 29)
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen", "29 min : pas encore"

    await _age(core, 2)
    before = len(engine.downloads)
    await _minute(core)
    asked = engine.downloads[before:]
    assert [(u, p.rsplit("\\", 1)[-1]) for u, p, _ in asked] == [("otherpeer", FILE2)]
    await _imported(client, rid)
    async with core.db.session() as s:
        done = list((await s.execute(select(Transfer).where(Transfer.state == "complete"))).scalars())
    assert sorted(t.username for t in done) == ["goodpeer", "goodpeer", "otherpeer"]


async def test_une_file_qui_avance_n_est_pas_muette(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, rows = await _album_queued_at_goodpeer(client, core, engine)
    await _minute(core)
    await _age(core)
    await _update(core, rows[0], TransferState.QUEUED, queued=True, place=4)  # une place de gagnée
    before = len(engine.downloads)
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen"
    assert engine.downloads[before:] == []


async def test_un_fichier_qui_arrive_et_les_autres_attendent(ctx):  # noqa: F811
    """Un pair envoie un fichier à la fois : les suivants ne bougent pas dans sa file pendant
    ce temps, et ce n'est pas du silence."""
    client, core, engine, env = ctx
    rid, good, rows = await _album_queued_at_goodpeer(client, core, engine)
    await _minute(core)
    await _age(core)
    await _update(core, rows[0], TransferState.TRANSFERRING, 10)
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen"


async def test_en_pause_ou_regle_a_zero_rien_ne_change(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, rows = await _album_queued_at_goodpeer(client, core, engine)
    await _minute(core)
    cfg = (await client.get("/api/settings")).json()
    cfg["hunter"]["silent_source_minutes"] = 0
    assert (await client.put("/api/settings", json=cfg)).status_code == 200
    await _age(core)
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen", "0 = jamais"

    cfg["hunter"]["silent_source_minutes"] = 30
    await client.put("/api/settings", json=cfg)
    assert (await client.post(f"/api/transfers/{rows[0].id}/pause")).status_code == 200
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen", "en pause : c'est toi"


async def test_personne_d_autre_la_source_reste(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, good, rows = await _album_queued_at_goodpeer(client, core, engine, peers=("goodpeer",))
    await _minute(core)
    await _age(core)
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen", "il ne reste qu'à attendre"
    assert (await client.get(f"/api/albums/{rid}")).json()["status"] == "downloading"


async def test_hors_ligne_personne_n_est_muet(ctx):  # noqa: F811
    """Soulseek coupé une heure : au retour, les sources ont de nouveau tout leur temps."""
    client, core, engine, env = ctx
    rid, good, rows = await _album_queued_at_goodpeer(client, core, engine)
    await _minute(core)
    await _age(core, 60)
    engine.logged_in = False
    await _minute(core)
    engine.logged_in = True
    await _minute(core)
    assert (await _candidate(client, rid, good["id"]))["status"] == "chosen"


async def test_un_autre_dossier_du_meme_pair_n_est_pas_choisi(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    artist = {"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"}
    aid = (await client.post("/api/artists", json=artist)).json()["id"]
    albums = (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
    rid = next(rg["id"] for rg in albums if rg["title"] == "Nevermind")
    copy = peer_with_album("goodpeer")
    for f in copy.files:
        f.filename = f.filename.replace("\\Nevermind\\", "\\Nevermind (copy)\\")
    engine.results = [peer_with_album("goodpeer"), copy]
    for name in NAMES:
        engine.script[f"goodpeer:{name}"] = ["hold", "hold"]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    assert len(cands) == 2
    await client.post(f"/api/candidates/{cands[0]['id']}/approve")
    await _minute(core)
    await _age(core)
    before = len(engine.downloads)
    await _minute(core)
    assert engine.downloads[before:] == [], "son autre dossier n'est pas une autre source"
    assert (await _candidate(client, rid, cands[0]["id"]))["status"] == "chosen"
