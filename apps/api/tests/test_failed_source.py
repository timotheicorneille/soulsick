"""Une source qui échoue n'en laisse pas de téléchargements « en file » pour toujours."""

from fakes import TRACKS
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401
from test_retry import _setup

from soulsick.messages import M
from soulsick.models import Artist, Candidate, CandidateStatus, ReleaseGroup, Transfer, TransferState

FILE1 = f"01 - {TRACKS[0][0]}.flac"


async def _rows(core, candidate_id):
    async with core.db.session() as s:
        rows = await s.execute(select(Transfer).where(Transfer.candidate_id == candidate_id))
        return list(rows.scalars())


async def _active(client):
    return sum((await client.get("/api/status")).json()["active_transfers"].values())


async def test_les_fichiers_encore_en_file_d_une_source_en_echec_sont_abandonnes(ctx):  # noqa: F811
    """Cas réel : deux albums en échec gardaient 17 morceaux « en file » ; la page Transferts
    n'en montrait aucun, le compteur du menu en annonçait 17."""
    client, core, engine, env = ctx
    engine.script[FILE1] = ["hold"]  # jamais répondu
    rid = await _setup(client, core, engine)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")
    assert await _active(client) > 0

    # La source lâche (moteur indisponible, disque plein…)
    await core.hunter._fail_candidate(cid, M("raw", text="no space left on device"))

    held = next(t for t in await _rows(core, cid) if t.remote_path.endswith(FILE1))
    assert held.state == TransferState.ABORTED
    assert await _active(client) == 0


async def test_au_demarrage_les_fantomes_deja_la_sont_nettoyes(ctx):  # noqa: F811
    """Ceux laissés par les versions d'avant : réparés au démarrage."""
    client, core, engine, env = ctx
    engine.script[FILE1] = ["hold"]
    rid = await _setup(client, core, engine)
    cid = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]["id"]
    await client.post(f"/api/candidates/{cid}/approve")

    async def others_arrived():  # seul le fichier retenu reste en file
        return sum(t.state == TransferState.COMPLETE for t in await _rows(core, cid)) == 2

    await wait_for(others_arrived)
    async with core.db.session() as s:  # l'état laissé par l'ancienne version
        (await s.get(Candidate, cid)).status = CandidateStatus.FAILED
        await s.commit()
    assert await _active(client) > 0

    await core._recover()
    assert await _active(client) == 0
    states = [t.state for t in await _rows(core, cid)]
    assert all(st in (TransferState.ABORTED, TransferState.COMPLETE) for st in states), states


async def test_restes_d_une_source_retiree(ctx):  # noqa: F811
    """Qobuz a été retiré : ses téléchargements encore en cours sont annulés et ses albums encore
    proposés écartés au démarrage, au lieu d'être relancés sur Soulseek, où ils n'existent pas."""
    client, core, engine, env = ctx
    async with core.db.session() as s:
        a = Artist(mbid="a-x", name="X", sort_name="X")
        s.add(a)
        await s.flush()
        rg = ReleaseGroup(mbid="rg-x", artist_id=a.id, title="X")
        s.add(rg)
        await s.flush()
        c = Candidate(release_group_id=rg.id, username="Qobuz", directory="qobuz:album-1", status=CandidateStatus.PROPOSED)
        s.add(c)
        s.add(Transfer(direction="download", username="Qobuz", remote_path="qobuz:album-1\\01 - X.flac",
                       state=TransferState.QUEUED, release_group_id=rg.id))
        await s.commit()

    raw = await core.settings.get_raw(core.settings.KEY) or {}
    await core.settings.set_raw(core.settings.KEY, raw | {"qobuz": {"token": "secret-du-compte"}})

    await core._drop_retired_source()
    assert "qobuz" not in (await core.settings.get_raw(core.settings.KEY)), "le jeton du compte est effacé"
    async with core.db.session() as s:
        assert (await s.execute(select(Transfer))).scalar_one().state == TransferState.ABORTED
        assert (await s.execute(select(Candidate))).scalar_one().status == CandidateStatus.REJECTED
