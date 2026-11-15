"""Après l'échec d'une source, le candidat suivant pris tout seul est chez un autre pair : un
autre dossier du même pair aurait fait attendre l'album tout autant."""

from dataclasses import replace

from fakes import peer_with_album
from test_flow import ctx, wait_for  # noqa: F401
from test_silent_source import NAMES, _age, _candidate, _imported, _minute


def _two_folders(user="goodpeer"):
    """Le même album, deux dossiers chez le même pair."""
    copy = peer_with_album(user)
    for f in copy.files:
        f.filename = f.filename.replace("\\Nevermind\\", "\\Nevermind (copy)\\")
    return [peer_with_album(user), copy]


async def _album(client, core, engine, results, mode="auto"):
    """La chasse classe d'abord les deux dossiers de goodpeer, otherpeer (file pleine) ensuite."""
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    artist = {"mbid": "x", "mode": mode, "primary_types": ["album"], "monitor": "all"}
    aid = (await client.post("/api/artists", json=artist)).json()["id"]
    albums = (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
    rid = next(rg["id"] for rg in albums if rg["title"] == "Nevermind")
    engine.results = results
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    assert [c["username"] for c in sorted(cands, key=lambda c: -c["score"])][:2] == ["goodpeer", "goodpeer"]
    return rid, cands


def _slow_otherpeer():
    return replace(peer_with_album("otherpeer"), has_free_slots=False, avg_speed=100_000, queue_size=20)


async def test_apres_un_pair_absent_le_suivant_est_chez_un_autre(ctx):  # noqa: F811
    """goodpeer ne répond jamais : après 30 minutes, son autre dossier passait devant
    otherpeer, pour 30 minutes d'attente de plus. check_stalled seul : c'est lui qui agit quand
    check_silent est réglé à 0, ou quand il passe avant lui."""
    client, core, engine, env = ctx
    for name in NAMES:
        engine.script[f"goodpeer:{name}"] = ["hold", "hold"]
    rid, cands = await _album(client, core, engine, _two_folders() + [_slow_otherpeer()])
    first = next(c for c in cands if c["status"] == "chosen")
    copy = next(c for c in cands if c["username"] == "goodpeer" and c["id"] != first["id"])

    await _age(core)
    before = len(engine.downloads)
    await core.hunter.check_stalled()

    assert (await _candidate(client, rid, first["id"]))["reject_reason_t"]["key"] == "reason.stalled"
    assert {u for u, _, _ in engine.downloads[before:]} == {"otherpeer"}
    assert (await _candidate(client, rid, copy["id"]))["status"] == "proposed"
    await _imported(client, rid)


async def test_apres_des_refus_le_suivant_est_chez_un_autre(ctx):  # noqa: F811
    """goodpeer refuse tout et rien n'est arrivé : l'album passe à otherpeer, pas à l'autre
    dossier de goodpeer."""
    client, core, engine, env = ctx
    core.hunter.max_attempts = 1
    for name in NAMES:
        engine.script[f"goodpeer:{name}"] = ["fail", "fail"]
    rid, _ = await _album(client, core, engine, _two_folders() + [_slow_otherpeer()])
    await _imported(client, rid)
    asked = [u for u, _, _ in engine.downloads if u == "goodpeer"]
    assert len(asked) == len(NAMES), "un seul dossier demandé"


async def test_seul_reste_le_meme_pair_c_est_toi_qui_choisis(ctx):  # noqa: F811
    """Plus personne d'autre : rien ne repart tout seul, l'album attend ton choix. Avant,
    check_silent laissait la source en place faute d'autre pair, puis check_stalled prenait
    l'autre dossier dans la même minute. Si tu le choisis toi-même, il part."""
    client, core, engine, env = ctx
    for name in NAMES:
        engine.script[f"goodpeer:{name}"] = ["hold", "complete"]
    rid, cands = await _album(client, core, engine, _two_folders())
    first = next(c for c in cands if c["status"] == "chosen")
    copy = next(c for c in cands if c["id"] != first["id"])

    await _minute(core)
    await _age(core)
    before = len(engine.downloads)
    await _minute(core)  # comme tick : check_stuck, check_silent, puis check_stalled
    await core.hunter.check_stalled()

    assert engine.downloads[before:] == [], "son autre dossier n'est pas une autre source"
    album = (await client.get(f"/api/albums/{rid}")).json()
    assert album["status"] == "pending_approval"
    assert album["status_reason_t"]["key"] == "reason.chosen_failed"
    assert (await _candidate(client, rid, copy["id"]))["status"] == "proposed"

    assert (await client.post(f"/api/candidates/{copy['id']}/approve")).status_code == 200
    assert {u for u, _, _ in engine.downloads[before:]} == {"goodpeer"}
    await _imported(client, rid)


async def test_en_validation_rien_ne_change(ctx):  # noqa: F811
    """Mode validation : après un échec, l'album attend ton choix, comme avant, même s'il ne
    reste qu'un autre dossier du même pair."""
    client, core, engine, env = ctx
    for name in NAMES:
        engine.script[f"goodpeer:{name}"] = ["hold"]
    rid, cands = await _album(client, core, engine, _two_folders(), mode="validation")
    await client.post(f"/api/candidates/{cands[0]['id']}/approve")
    await _age(core)
    await core.hunter.check_stalled()
    assert (await client.get(f"/api/albums/{rid}")).json()["status"] == "pending_approval"
    assert (await _candidate(client, rid, cands[1]["id"]))["status"] == "proposed"
