"""« Utiliser pour cet album » : un dossier trouvé à la main devient la source d'un album."""

import os
import time

from fakes import TRACKS, peer_with_album
from sqlalchemy import func, select
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import Candidate, CandidateStatus, Transfer, TransferState
from soulsick.scoring import PeerResult
from soulsick.slsk.engine import LiveSearch

FILE2 = f"02 - {TRACKS[1][0]}.flac"
FILE3 = f"03 - {TRACKS[2][0]}.flac"


async def _album(client, mode="validation"):
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={
        "mbid": "x", "mode": mode, "primary_types": ["album"], "monitor": "all",
    })
    groups = (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
    return next(rg["id"] for rg in groups if rg["title"] == "Nevermind")


async def _found(client, engine, peer: PeerResult) -> dict:
    """Le dossier tel que la page de recherche le reçoit, pour le renvoyer tel quel."""
    ticket = len(engine.searches) + 1
    engine.searches[ticket] = LiveSearch(ticket, "nirvana nevermind", time.time(), [peer])
    folders = (await client.get(f"/api/search/{ticket}")).json()["folders"]
    assert len(folders) == 1
    return folders[0]


async def _candidates(core, rid) -> int:
    async with core.db.session() as s:
        return (await s.execute(select(func.count()).where(Candidate.release_group_id == rid))).scalar_one()


async def test_le_dossier_choisi_devient_la_source_et_l_album_est_range(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _album(client)
    folder = await _found(client, engine, peer_with_album("manualpeer"))

    r = await client.post(f"/api/albums/{rid}/candidates", json=folder)
    assert r.status_code == 200, r.text
    body = r.json()
    cand = body["candidate"]
    assert body["created"] is True and body["dropped"] == 0
    assert cand["username"] == "manualpeer" and cand["status"] == "chosen"
    assert cand["matched_release_id"] and cand["format"] == "flac"
    # Les fichiers sont demandés à ce pair, dans le dossier de l'album, pas dans downloads/manual
    assert sorted(p for _, p, _ in engine.downloads) == sorted(f["filename"] for f in folder["files"])
    assert {(u, d) for u, _, d in engine.downloads} == {("manualpeer", cand["download_dir"])}
    assert cand["download_dir"].startswith(str(env.downloads_dir / f"album-{rid}") + os.sep)

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    assert (env.music_dir / "Nirvana" / "1991 - Nevermind" / "01 - Smells Like Teen Spirit.flac").exists()

    # L'album est complet : un autre dossier ne le remplace pas en douce
    other = await _found(client, engine, peer_with_album("otherpeer"))
    r = await client.post(f"/api/albums/{rid}/candidates", json=other)
    assert r.status_code == 409 and r.json()["detail"]["key"] == "error.album_complete"


async def test_meme_candidat_qu_une_chasse(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _album(client)
    engine.results = [peer_with_album("huntpeer")]
    await core.hunter.hunt(rid)
    hunted = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]

    folder = await _found(client, engine, peer_with_album("manualpeer"))
    manual = (await client.post(f"/api/albums/{rid}/candidates", json=folder)).json()["candidate"]
    for k in ("directory", "files", "format", "score", "score_detail", "matched_release_id",
              "reject_reason", "has_free_slots", "queue_size", "avg_speed"):
        assert manual[k] == hunted[k], k


async def test_un_dossier_rejete_par_la_notation_est_pris_quand_meme(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _album(client)
    peer = peer_with_album("manualpeer")
    peer.files = peer.files[:2]  # il manque une piste : la notation le rejette
    folder = await _found(client, engine, peer)

    r = await client.post(f"/api/albums/{rid}/candidates", json=folder)
    assert r.status_code == 200, r.text
    cand = r.json()["candidate"]
    assert cand["reject_reason_t"]["key"] == "reject.missing_tracks"
    assert cand["status"] == "chosen"
    assert len(engine.downloads) == 2

    # Comme un candidat rejeté approuvé : ce qui arrive est gardé, l'album attend la piste manquante
    async def waiting_for_the_rest():
        a = (await client.get(f"/api/albums/{rid}")).json()
        return a["status"] == "pending_approval" and a["status_reason_t"]["key"] == "reason.incomplete_action"

    await wait_for(waiting_for_the_rest)


async def test_une_source_en_cours_est_remplacee_comme_un_changement_de_pair(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _album(client)
    # slowpeer livre la piste 1, les deux autres restent chez lui
    engine.results = [peer_with_album("slowpeer")]
    engine.script[f"slowpeer:{FILE2}"] = ["hold"]
    engine.script[f"slowpeer:{FILE3}"] = ["hold"]
    await core.hunter.hunt(rid)
    slow = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]
    await client.post(f"/api/candidates/{slow['id']}/approve")

    async def first_done():
        async with core.db.session() as s:
            states = select(Transfer.state).where(Transfer.candidate_id == slow["id"])
            return TransferState.COMPLETE in list((await s.execute(states)).scalars())

    await wait_for(first_done)
    engine.downloads.clear()

    folder = await _found(client, engine, peer_with_album("manualpeer"))
    r = await client.post(f"/api/albums/{rid}/candidates", json=folder)
    assert r.status_code == 200, r.text
    assert r.json()["dropped"] == 1
    async with core.db.session() as s:
        assert (await s.get(Candidate, slow["id"])).status == CandidateStatus.FAILED
    # Ce qui est arrivé reste : seules les pistes manquantes sont demandées au nouveau pair
    asked = sorted(p.rsplit("\\", 1)[-1] for u, p, _ in engine.downloads if u == "manualpeer")
    assert asked == [FILE2, FILE3]

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    kinds = [e["kind"] for e in (await client.get("/api/events", params={"release_group_id": rid})).json()]
    assert "download.switched" in kinds


async def test_un_candidat_existant_est_repris_sans_doublon(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _album(client)
    engine.results = [peer_with_album("goodpeer")]
    await core.hunter.hunt(rid)
    proposed = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]
    before = await _candidates(core, rid)

    folder = await _found(client, engine, peer_with_album("goodpeer"))
    r = await client.post(f"/api/albums/{rid}/candidates", json=folder)
    assert r.status_code == 200, r.text
    assert r.json()["created"] is False and r.json()["candidate"]["id"] == proposed["id"]
    assert await _candidates(core, rid) == before
    n = len(engine.downloads)

    # Un second clic ne redemande rien
    r = await client.post(f"/api/albums/{rid}/candidates", json=folder)
    assert r.status_code == 409 and r.json()["detail"]["key"] == "error.candidate_busy"
    assert len(engine.downloads) == n and await _candidates(core, rid) == before


async def test_entrees_refusees(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _album(client)
    folder = await _found(client, engine, peer_with_album("manualpeer"))

    # Un fichier hors du dossier annoncé, ou un nom qui n'en est pas un
    nevermind = "@@music\\Nirvana\\Nevermind\\"
    for path in ["@@music\\Autre\\01 - x.flac", nevermind + "..", nevermind]:
        bad = folder | {"files": folder["files"] + [folder["files"][0] | {"filename": path}]}
        r = await client.post(f"/api/albums/{rid}/candidates", json=bad)
        assert r.status_code == 400 and r.json()["detail"]["key"] == "error.file_not_in_folder", path

    # Pas un seul fichier audio
    cover = folder | {"files": [{"filename": "@@music\\Nirvana\\Nevermind\\cover.jpg", "size": 10}]}
    r = await client.post(f"/api/albums/{rid}/candidates", json=cover)
    assert r.status_code == 400 and r.json()["detail"]["key"] == "error.no_audio_files"

    for wrong in ({"files": []}, {"username": ""}):
        r = await client.post(f"/api/albums/{rid}/candidates", json=folder | wrong)
        assert r.status_code == 422, wrong
    r = await client.post("/api/albums/9999/candidates", json=folder)
    assert r.status_code == 404 and r.json()["detail"]["key"] == "error.album_not_found"

    # Hors ligne : refusé tout de suite, rien n'est créé (pas de "lancé" suivi d'un retour en attente)
    engine.logged_in = False
    r = await client.post(f"/api/albums/{rid}/candidates", json=folder)
    assert r.status_code == 503 and r.json()["detail"]["key"] == "error.engine_offline"

    assert engine.downloads == [] and await _candidates(core, rid) == 0
