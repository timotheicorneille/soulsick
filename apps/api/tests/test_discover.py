"""Découvrir : des artistes à suivre, d'après les artistes similaires (ListenBrainz) de ceux suivis."""

import httpx
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import Artist

# Artistes suivis
VALD = "00000000-0000-0000-0000-00000000000a"
LAYLOW = "00000000-0000-0000-0000-00000000000b"
# Artistes similaires
DAMSO = "10000000-0000-0000-0000-000000000001"
ORELSAN = "10000000-0000-0000-0000-000000000002"
PNL = "10000000-0000-0000-0000-000000000003"

SIMILAR = {
    VALD: [(DAMSO, "Damso", 300), (ORELSAN, "Orelsan", 150), (LAYLOW, "Laylow", 120)],
    LAYLOW: [(DAMSO, "Damso", 80), (PNL, "PNL", 100)],
}


def listenbrainz(calls: list[str]) -> httpx.AsyncClient:
    """A fake ListenBrainz: the similar artists of SIMILAR, in its JSON shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        mbid = request.url.params["artist_mbids"]
        calls.append(mbid)
        return httpx.Response(200, json=[
            {"artist_mbid": m, "name": n, "score": s, "comment": "", "type": "Person",
             "gender": None, "reference_mbid": mbid}
            for m, n, s in SIMILAR.get(mbid, [])
        ])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _setup(client, core) -> dict[str, int]:
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    ids = {}
    async with core.db.session() as s:
        for mbid, name in ((VALD, "Vald"), (LAYLOW, "Laylow")):
            a = Artist(mbid=mbid, name=name, sort_name=name)
            s.add(a)
            await s.flush()
            ids[mbid] = a.id
        await s.commit()
    return ids


async def test_recommande_les_artistes_communs_en_premier(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await _setup(client, core)
    calls: list[str] = []
    core.discover.client = listenbrainz(calls)

    r = await client.post("/api/discover/refresh")
    assert r.status_code == 200
    assert sorted(calls) == sorted([VALD, LAYLOW])

    recos = (await client.get("/api/discover")).json()["recommendations"]
    # Damso, similaire aux deux, d'abord ; Laylow, déjà suivi, jamais
    assert [x["name"] for x in recos] == ["Damso", "PNL", "Orelsan"]
    assert {b["name"] for b in recos[0]["because"]} == {"Vald", "Laylow"}
    assert 0 < recos[-1]["score"] < recos[0]["score"] <= 100


async def test_un_artiste_ecarte_ne_revient_plus(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await _setup(client, core)
    core.discover.client = listenbrainz([])
    await client.post("/api/discover/refresh")

    r = await client.post("/api/discover/dismiss", json={"mbid": DAMSO})
    assert r.status_code == 200
    names = [x["name"] for x in (await client.get("/api/discover")).json()["recommendations"]]
    assert "Damso" not in names


async def test_rafraichit_seulement_les_artistes_perimes(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await _setup(client, core)
    calls: list[str] = []
    core.discover.client = listenbrainz(calls)

    assert await core.discover.refresh_stale(limit=10) == 2
    assert await core.discover.refresh_stale(limit=10) == 0, "déjà à jour : rien à redemander"
    assert len(calls) == 2


async def test_similaires_d_un_artiste(ctx):  # noqa: F811
    client, core, engine, env = ctx
    ids = await _setup(client, core)
    core.discover.client = listenbrainz([])
    await client.post("/api/discover/refresh")

    similar = (await client.get(f"/api/artists/{ids[VALD]}/similar")).json()
    assert [x["name"] for x in similar] == ["Damso", "Orelsan", "Laylow"]
    # Laylow est suivi : lien vers sa page plutôt que « Suivre »
    assert similar[2]["followed_id"] == ids[LAYLOW]
    assert similar[0]["followed_id"] is None


async def test_un_artiste_en_double_chez_listenbrainz_ne_compte_qu_une_fois(ctx):  # noqa: F811
    """ListenBrainz renvoie parfois le même artiste deux fois : son meilleur score seulement."""
    client, core, engine, env = ctx
    await _setup(client, core)
    # Additionnés (150 + 160), Orelsan passerait devant PNL
    SIMILAR[VALD].append((ORELSAN, "Orelsan", 160))
    try:
        core.discover.client = listenbrainz([])
        await client.post("/api/discover/refresh")
    finally:
        SIMILAR[VALD].pop()
    recos = (await client.get("/api/discover")).json()["recommendations"]
    assert [x["name"] for x in recos] == ["Damso", "PNL", "Orelsan"]


async def test_suivre_plusieurs_artistes_avec_les_memes_reglages(ctx):  # noqa: F811
    """Sélection multiple : chaque artiste est ajouté avec les mêmes réglages, en arrière-plan."""
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists/bulk", json={
        "mbids": [DAMSO, PNL, DAMSO], "mode": "auto", "primary_types": ["album"],
        "secondary_types": ["live"], "monitor": "all",
    })
    assert r.status_code == 200
    assert r.json()["queued"] == 2, "un artiste coché deux fois n'est ajouté qu'une fois"

    async def both_added():
        artists = (await client.get("/api/artists")).json()
        return len(artists) == 2 and all(a["stats"]["monitored"] for a in artists)

    await wait_for(both_added)
    for a in (await client.get("/api/artists")).json():
        assert (a["mode"], a["primary_types"], a["secondary_types"]) == ("auto", ["album"], ["live"])


async def test_listenbrainz_injoignable_ne_casse_rien(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await _setup(client, core)

    def down(request):
        raise httpx.ConnectError("unreachable")

    core.discover.client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    assert (await client.post("/api/discover/refresh")).status_code == 200
    body = (await client.get("/api/discover")).json()
    assert body["recommendations"] == []
    assert body["pending"] == 2, "les artistes seront redemandés au prochain passage"


async def test_photo_d_artiste_depuis_deezer(ctx):  # noqa: F811
    """Deezer est cherché par nom : l'homonyme le plus écouté, jamais l'image par défaut."""
    import asyncio

    client, core, engine, env = ctx
    await _setup(client, core)
    core.discover.client = listenbrainz([])
    await client.post("/api/discover/refresh")
    await asyncio.gather(*core.discover._prefetch)  # their photos: the fake ListenBrainz has none
    for f in core.mb.covers_dir.glob("artist-*"):
        f.unlink()
    searches: list[str] = []

    def deezer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "img.test":
            return httpx.Response(200, content=b"jpeg:" + request.url.path.encode())
        searches.append(request.url.params["q"])
        return httpx.Response(200, json={"data": {
            "Damso": [
                {"name": "Damso", "nb_fan": 597, "picture_big": "https://img.test/faux.jpg"},
                {"name": "Damsø", "nb_fan": 9_000_000, "picture_big": "https://img.test/autre.jpg"},
                {"name": "DAMSO", "nb_fan": 5_156_654, "picture_big": "https://img.test/vrai.jpg"},
            ],
            "Orelsan": [{"name": "Orelsan", "nb_fan": 10, "picture_big": "https://img.test/images/artist//500x500.jpg"}],
        }.get(request.url.params["q"], [])})

    core.discover.client = httpx.AsyncClient(transport=httpx.MockTransport(deezer))

    r = await client.get(f"/api/artist-photos/{DAMSO}")
    assert r.status_code == 200 and r.content == b"jpeg:/vrai.jpg"
    assert (await client.get(f"/api/artist-photos/{DAMSO}")).content == b"jpeg:/vrai.jpg"
    assert searches == ["Damso"], "la photo est gardée sur le disque"

    assert (await client.get(f"/api/artist-photos/{ORELSAN}")).status_code == 404, "image par défaut de Deezer"
    assert (await client.get(f"/api/artist-photos/{VALD}")).status_code == 404, "artiste suivi, absent de Deezer"
    assert (await client.get(f"/api/artist-photos/{ORELSAN}")).status_code == 404
    assert searches == ["Damso", "Orelsan", "Vald"], "l'absence de photo est retenue aussi"
    assert (await client.get("/api/artist-photos/pas-un-mbid")).status_code == 400


async def test_un_artiste_ajoute_est_demande_tout_de_suite(ctx):  # noqa: F811
    """Sans attendre le prochain passage (30 min) : suivre un artiste réveille Découvrir."""
    import asyncio

    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    pause = asyncio.create_task(core.discover._pause(1800))
    await asyncio.sleep(0)
    await core.catalog.add_artist(DAMSO, mode="validation", quality_profile_id=None,
                                  primary_types=["album"], secondary_types=[])
    await asyncio.wait_for(pause, 1)


async def test_les_photos_sont_prechargees_avec_les_similaires(ctx):  # noqa: F811
    """La page ne les attend pas : elles arrivent avec les artistes similaires."""
    import asyncio

    client, core, engine, env = ctx
    await _setup(client, core)
    lb = listenbrainz([])

    async def both(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.deezer.com":
            q = request.url.params["q"]
            return httpx.Response(200, json={"data": [{"name": q, "nb_fan": 1, "picture_big": f"https://img.test/{q}.jpg"}]})
        if request.url.host == "img.test":
            return httpx.Response(200, content=b"jpeg")
        return await lb._transport.handle_async_request(request)

    core.discover.client = httpx.AsyncClient(transport=httpx.MockTransport(both))
    await client.post("/api/discover/refresh")
    await asyncio.gather(*core.discover._prefetch)
    for mbid in (DAMSO, ORELSAN, PNL):
        assert (core.mb.covers_dir / f"artist-{mbid}.jpg").exists()
