"""Relances de transferts : une relance ne doit jamais perdre le lien avec l'album."""

import asyncio
import os

from fakes import TRACKS, peer_with_album
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import Transfer
from sqlalchemy import select

FILE2 = f"02 - {TRACKS[1][0]}.flac"


async def _setup(client, core, engine, mode="validation"):
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": mode, "primary_types": ["album"], "monitor": "all"})
    aid = r.json()["id"]
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album()]
    core.hunter.retry_delays = [0.05, 0.05]
    await core.hunter.hunt(rid)
    return rid


async def _rows(core):
    async with core.db.session() as s:
        return list((await s.execute(select(Transfer).order_by(Transfer.id))).scalars())


async def _status(client, rid):
    return (await client.get(f"/api/albums/{rid}")).json()["status"]


async def test_echec_temporaire_relance_automatiquement(ctx):  # noqa: F811
    client, core, engine, env = ctx
    engine.script[FILE2] = ["fail"]  # 1er essai refusé, 2e essai OK
    rid = await _setup(client, core, engine, mode="auto")

    async def imported():
        return await _status(client, rid) == "imported"

    await wait_for(imported)
    rows = await _rows(core)
    assert len(rows) == 3, "une ligne par fichier, jamais de doublon"
    assert all(t.candidate_id is not None for t in rows)
    second = next(t for t in rows if t.remote_path.endswith(FILE2))
    assert second.attempts == 2


async def test_relance_manuelle_reutilise_la_ligne(ctx):  # noqa: F811
    client, core, engine, env = ctx
    engine.script[FILE2] = ["fail", "fail", "fail"]  # épuise les essais automatiques
    rid = await _setup(client, core, engine)
    best = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]
    await client.post(f"/api/candidates/{best['id']}/approve")

    async def gave_up():
        return await _status(client, rid) in ("pending_approval", "wanted")

    await wait_for(gave_up)
    failed = next(t for t in await _rows(core) if t.remote_path.endswith(FILE2))
    assert failed.state == "failed"

    r = await client.post(f"/api/transfers/{failed.id}/retry")  # cette fois le pair accepte
    assert r.status_code == 200

    async def imported():
        return await _status(client, rid) == "imported"

    await wait_for(imported)
    rows = await _rows(core)
    assert len(rows) == 3
    assert next(t for t in rows if t.id == failed.id).state == "complete"


async def test_reprendre_un_candidat_ne_retelecharge_que_le_manquant(ctx):  # noqa: F811
    client, core, engine, env = ctx
    engine.script[FILE2] = ["fail", "fail", "fail"]
    rid = await _setup(client, core, engine)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")

    async def gave_up():
        return await _status(client, rid) in ("pending_approval", "wanted")

    await wait_for(gave_up)
    before = len(engine.downloads)
    r = await client.post(f"/api/candidates/{cid}/approve")  # « Reprendre »
    assert r.status_code == 200

    async def imported():
        return await _status(client, rid) == "imported"

    await wait_for(imported)
    assert len(engine.downloads) - before == 1, "seul le fichier manquant est redemandé"
    assert len(await _rows(core)) == 3


async def _gave_up(client, rid):
    async def gave_up():
        return await _status(client, rid) in ("pending_approval", "wanted")

    await wait_for(gave_up)


def _album_dirs(env, rid):
    path = os.path.join(str(env.downloads_dir), f"album-{rid}")
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


async def test_nouvelle_chasse_reprend_le_dossier_de_la_meme_source(ctx):  # noqa: F811
    """Chasse suivante (toutes les 12 h) sur la même source après un échec : seuls les
    fichiers manquants sont redemandés, dans le même dossier (avant : l'album entier,
    dans un nouveau dossier à chaque fois, jusqu'à remplir le disque)."""
    client, core, engine, env = ctx
    engine.script[FILE2] = ["fail", "fail", "fail"]
    rid = await _setup(client, core, engine, mode="auto")
    await _gave_up(client, rid)
    assert len(_album_dirs(env, rid)) == 1

    # Même source, toujours refusée : rien d'autre n'est retéléchargé, aucun dossier en plus
    engine.script[FILE2] = ["fail", "fail", "fail"]
    before = len(engine.downloads)
    await core.hunter.hunt(rid, trigger="auto")
    await _gave_up(client, rid)
    assert all(p.endswith(FILE2) for _, p, _ in engine.downloads[before:])
    assert len(_album_dirs(env, rid)) == 1

    # Cette fois la source livre : seul le fichier manquant est redemandé, puis l'import
    before = len(engine.downloads)
    await core.hunter.hunt(rid, trigger="auto")

    async def imported():
        return await _status(client, rid) == "imported"

    await wait_for(imported)
    assert len(engine.downloads) - before == 1


async def test_arrivee_tardive_apres_abandon(ctx):  # noqa: F811
    client, core, engine, env = ctx
    engine.script[FILE2] = ["fail", "fail", "fail"]
    rid = await _setup(client, core, engine)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")

    async def gave_up():
        return await _status(client, rid) in ("pending_approval", "wanted")

    await wait_for(gave_up)
    # aioslsk finit par recevoir le fichier tout seul (le pair a relancé de son côté)
    t = next(t for t in await _rows(core) if t.remote_path.endswith(FILE2))
    from fakes import tiny_flac
    from pathlib import Path
    tiny_flac(Path(t.local_path), TRACKS[1][1] // 1000)
    await engine.emit(t.username, t.remote_path, t.local_path, "queued")
    await engine.emit(t.username, t.remote_path, t.local_path, "complete")

    async def imported():
        return await _status(client, rid) == "imported"

    await wait_for(imported)
    assert len(await _rows(core)) == 3
    await asyncio.sleep(0)


async def test_reprise_au_demarrage_reactive_le_candidat(ctx):  # noqa: F811
    """Un fichier relancé au démarrage pour un candidat abandonné : l'album doit redevenir « téléchargement »."""
    client, core, engine, env = ctx
    engine.script[FILE2] = ["fail", "fail", "fail", "hold"]
    rid = await _setup(client, core, engine)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")

    async def gave_up():
        return await _status(client, rid) in ("pending_approval", "wanted")

    await wait_for(gave_up)
    t = next(t for t in await _rows(core) if t.remote_path.endswith(FILE2))
    async with core.db.session() as s:
        row = await s.get(Transfer, t.id)
        row.state = "queued"  # comme après la réparation des orphelins
        await s.commit()

    await core._resume_downloads()
    album = (await client.get(f"/api/albums/{rid}")).json()
    assert album["status"] == "downloading"
    assert next(c for c in album["candidates"] if c["id"] == cid)["status"] == "chosen"


async def test_source_remplacee_pendant_le_delai_plus_rien_ne_lui_est_demande(ctx):  # noqa: F811
    """goodpeer refuse la piste 2, la relance est programmée. Avant qu'elle parte, la source est
    remplacée par otherpeer : la piste ne doit plus être redemandée à goodpeer (avant : elle
    l'était, et restait "en file" chez un pair déjà quitté)."""
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    artist = {"mbid": "x", "mode": "validation", "primary_types": ["album"], "monitor": "all"}
    aid = (await client.post("/api/artists", json=artist)).json()["id"]
    albums = (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
    rid = next(rg["id"] for rg in albums if rg["title"] == "Nevermind")
    engine.results = [peer_with_album("goodpeer"), peer_with_album("otherpeer")]
    engine.script[f"goodpeer:{FILE2}"] = ["fail", "hold"]
    core.hunter.retry_delays = [0.4, 0.4]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    good = next(c for c in cands if c["username"] == "goodpeer")
    other = next(c for c in cands if c["username"] == "otherpeer")
    await client.post(f"/api/candidates/{good['id']}/approve")

    async def retry_scheduled():
        return bool(core.hunter._retrying)

    await wait_for(retry_scheduled)
    r = await client.post(f"/api/candidates/{good['id']}/switch", json={"to": other["id"]})
    assert r.status_code == 200

    async def retry_over():
        return not core.hunter._retrying

    await wait_for(retry_over)
    asked = [u for u, p, _ in engine.downloads if p.endswith(FILE2)]
    assert asked == ["goodpeer", "otherpeer"], "une seule demande à goodpeer : la première"
    second = next(t for t in await _rows(core) if t.username == "goodpeer" and t.remote_path.endswith(FILE2))
    assert second.state == "failed" and second.attempts == 1

    async def imported():
        return await _status(client, rid) == "imported"

    await wait_for(imported)
