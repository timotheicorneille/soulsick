"""Panachage : compléter un album avec des pistes venant d'un autre pair."""

from fakes import TRACKS, peer_with_album
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.audiotags import read_meta
from soulsick.models import Candidate, CandidateStatus, LibraryFile
from sqlalchemy import select

FILE2 = f"02 - {TRACKS[1][0]}.flac"


async def _album_with_one_missing(client, core, engine):
    """am3 donne 2 pistes sur 3 (la 2 est refusée jusqu'au bout), otherpeer a l'album complet.
    otherpeer (et flaky) ont été écartés à la notation : le relais automatique ne va pas chez
    eux, compléter depuis eux reste ton choix."""
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"})
    aid = r.json()["id"]
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album("am3"), peer_with_album("otherpeer")]
    if getattr(engine, "results_extra", False):
        engine.results.append(peer_with_album("flaky"))
    engine.script[f"am3:{FILE2}"] = ["fail", "fail", "fail"]
    core.hunter.retry_delays = [0.02, 0.02]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    am3 = next(c for c in cands if c["username"] == "am3")
    other = next(c for c in cands if c["username"] == "otherpeer")
    async with core.db.session() as s:
        for c in (await s.execute(select(Candidate).where(Candidate.username != "am3"))).scalars():
            c.status = CandidateStatus.REJECTED
        await s.commit()
    await client.post(f"/api/candidates/{am3['id']}/approve")

    async def gave_up():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "pending_approval"

    await wait_for(gave_up)
    return rid, am3, other


async def test_couverture_montre_ce_qui_manque_et_qui_peut_completer(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, am3, other = await _album_with_one_missing(client, core, engine)
    cov = (await client.get(f"/api/albums/{rid}/coverage")).json()
    assert cov["summary"]["received"] == 2 and cov["summary"]["missing"] == 1
    missing = next(t for t in cov["tracks"] if t["state"] == "missing")
    assert missing["title"] == "In Bloom"
    received = next(t for t in cov["tracks"] if t["state"] == "received")
    assert received["sources"][0]["username"] == "am3"
    filler = next(f for f in cov["fillers"] if f["username"] == "otherpeer")
    assert [t["title"] for t in filler["tracks"]] == ["In Bloom"]


async def test_completer_une_piste_depuis_un_autre_pair(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid, am3, other = await _album_with_one_missing(client, core, engine)
    cov = (await client.get(f"/api/albums/{rid}/coverage")).json()
    missing_id = next(t["track_id"] for t in cov["tracks"] if t["state"] == "missing")
    before = len(engine.downloads)

    r = await client.post(f"/api/albums/{rid}/complete", json={"candidate_id": other["id"], "track_ids": [missing_id]})
    assert r.status_code == 200, r.text
    assert r.json()["files"] == 1

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    assert len(engine.downloads) - before == 1, "une seule piste demandée à otherpeer"
    async with core.db.session() as s:
        files = list((await s.execute(select(LibraryFile))).scalars())
    assert len(files) == 3
    titles = sorted(read_meta(f.path).title for f in files)
    assert titles == sorted(t for t, _ in TRACKS)
    events = (await client.get("/api/events", params={"release_group_id": rid})).json()
    done = next(e for e in events if e["kind"] == "import.done")
    assert done["data"]["sources"] == {"am3": 2, "otherpeer": 1}
    # La réception est nettoyée
    assert not any((env.downloads_dir / f"album-{rid}").iterdir())


async def test_album_complet_ailleurs_sans_doublons(ctx):  # noqa: F811
    """Tu abandonnes am3 et prends tout l'album chez otherpeer : pas de doublons, pas de revue."""
    client, core, engine, env = ctx
    rid, am3, other = await _album_with_one_missing(client, core, engine)
    await client.post(f"/api/candidates/{other['id']}/approve")

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    async with core.db.session() as s:
        files = list((await s.execute(select(LibraryFile))).scalars())
    assert len(files) == 3


async def test_piste_en_bibliotheque_malgre_des_essais_annules(ctx):  # noqa: F811
    """Cas réel : ALED annulé chez deux pairs puis reçu d'un troisième → la piste n'est pas « manquante »."""
    client, core, engine, env = ctx
    engine.results_extra = True
    rid, am3, other = await _album_with_one_missing(client, core, engine)
    cov = (await client.get(f"/api/albums/{rid}/coverage")).json()
    missing_id = next(t["track_id"] for t in cov["tracks"] if t["state"] == "missing")
    # 1er complément chez « flaky » : échoue jusqu'au bout (comme underpaid0954 / prizewhisper)
    flaky = next(f for f in cov["fillers"] if f["username"] == "flaky")
    engine.script[f"flaky:{FILE2}"] = ["fail", "fail", "fail"]
    await client.post(f"/api/albums/{rid}/complete", json={"candidate_id": flaky["candidate_id"], "track_ids": [missing_id]})

    async def flaky_failed():
        cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
        return next(c for c in cands if c["username"] == "flaky")["status"] == "failed"

    await wait_for(flaky_failed)
    # 2e complément chez otherpeer : réussi
    await client.post(f"/api/albums/{rid}/complete", json={"candidate_id": other["id"], "track_ids": [missing_id]})

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    cov = (await client.get(f"/api/albums/{rid}/coverage")).json()
    assert cov["summary"]["missing"] == 0, cov["tracks"]
    assert all(t["state"] == "in_library" for t in cov["tracks"])
    # am3 garde sa tentative ratée sur la piste 2, mais elle ne doit plus polluer l'affichage
    t2 = next(t for t in cov["tracks"] if t["title"] == "In Bloom")
    assert t2["sources"] == []
