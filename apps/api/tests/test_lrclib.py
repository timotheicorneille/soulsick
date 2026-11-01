"""Paroles depuis LRCLIB : seulement pour les pistes qui n'en ont pas, jamais rien d'écrasé."""

import asyncio
import os

import httpx
from fakes import TRACKS, tiny_flac
from mutagen.flac import FLAC
from sqlalchemy import select
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.models import LibraryFile
from soulsick.services import lyrics_fetch

SYNCED = "[00:01.00] Load up on guns\n"
RG = "1b022e01-4da6-387b-8658-8678046e4cef"


def lrclib(asked: list[str], answers: dict[str, object], search: dict[str, list] | None = None) -> httpx.AsyncClient:
    """A fake LRCLIB: the exact lookup answers by title (a JSON answer, or an HTTP status);
    the search answers from `search` (nothing by default)."""

    def handler(request: httpx.Request) -> httpx.Response:
        title = request.url.params["track_name"]
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=(search or {}).get(title, []))
        asked.append(title)
        a = answers.get(title, 404)
        if isinstance(a, int):
            return httpx.Response(a, json={"message": "nope"})
        return httpx.Response(200, json={"instrumental": False, "syncedLyrics": None, "plainLyrics": None} | a)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _shelve(core, env, titles: list[str]) -> dict[str, str]:
    """Tracks of an album already in the library, tagged like Soulsick does."""
    paths = {}
    for i, title in enumerate(titles):
        p = env.music_dir / "Nirvana" / "1991 - Nevermind" / f"{i + 1:02d} - {title}.flac"
        tiny_flac(p, 200)
        f = FLAC(p)
        f["TITLE"], f["ARTIST"], f["ALBUM"], f["MUSICBRAINZ_RELEASEGROUPID"] = title, "Nirvana", "Nevermind", RG
        f.save()
        paths[title] = str(p)
    await core.library.scan()
    return paths


async def test_ecrit_les_paroles_manquantes_sans_rien_ecraser(ctx):  # noqa: F811
    client, core, engine, env = ctx
    titles = ["Synced", "Plain", "Instru", "Unknown", "Has Lrc"]
    paths = await _shelve(core, env, titles)
    with open(paths["Has Lrc"].replace(".flac", ".lrc"), "w") as fh:
        fh.write("recalé à la main")
    await core.library.scan()
    asked: list[str] = []
    core.lyrics.client = lrclib(asked, {
        "Synced": {"syncedLyrics": SYNCED, "plainLyrics": "Load up on guns"},
        "Plain": {"plainLyrics": "Load up on guns"},
        "Instru": {"instrumental": True},
    })

    r = await core.lyrics.fetch_album(RG)
    assert sorted(asked) == ["Instru", "Plain", "Synced", "Unknown"], "jamais pour une piste qui a ses paroles"
    assert r == {"asked": 4, "found": 2, "error": None}
    assert open(paths["Synced"].replace(".flac", ".lrc")).read() == SYNCED
    assert open(paths["Plain"].replace(".flac", ".txt")).read() == "Load up on guns\n"
    assert open(paths["Has Lrc"].replace(".flac", ".lrc")).read() == "recalé à la main"

    async with core.db.session() as s:
        rows = {f.tag_title: f for f in (await s.execute(select(LibraryFile))).scalars()}
    assert (rows["Synced"].lyrics_file, rows["Plain"].lyrics_file) == ("lrc", "txt")
    assert (rows["Instru"].lyrics_missing, rows["Unknown"].lyrics_missing) == ("instrumental", "not_found")

    # Asked again only after a while, or when you ask for it
    asked.clear()
    assert (await core.lyrics.fetch_album(RG))["asked"] == 0
    assert (await core.lyrics.fetch_album(RG, by_user=True))["asked"] == 2
    assert sorted(asked) == ["Instru", "Unknown"]


async def test_bouton_de_l_album(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={
        "mbid": "5b11f4ce-a62d-471e-81fc-a69a8278c7da", "mode": "validation", "primary_types": ["album"],
    })
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    await _shelve(core, env, [TRACKS[0][0]])
    core.lyrics.client = lrclib([], {TRACKS[0][0]: {"syncedLyrics": SYNCED}})

    r = await client.post(f"/api/albums/{rid}/lyrics")
    assert r.json() == {"asked": 1, "found": 1, "error": None}
    files = (await client.get(f"/api/albums/{rid}")).json()["files"]
    assert files[0]["lyrics"] == {"kind": "synced", "source": "file", "ext": "lrc"}


async def test_lrclib_sature_la_passe_s_arrete_et_reprendra(ctx, monkeypatch):  # noqa: F811
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    monkeypatch.setattr(lyrics_fetch, "BACKOFF", (0, 0))
    monkeypatch.setattr(lyrics_fetch, "PAUSE", 0)
    await _shelve(core, env, ["A", "B", "C"])
    asked: list[str] = []
    core.lyrics.client = lrclib(asked, {"A": {"plainLyrics": "a"}, "B": 503, "C": {"plainLyrics": "c"}})

    assert core.lyrics.start_library()
    assert not core.lyrics.start_library(), "une seule passe à la fois"

    await asyncio.gather(*core.lyrics._tasks)
    assert asked == ["A", "B", "B", "B"], "réessaie, puis abandonne la passe"
    assert core.lyrics.job == {"running": False, "done": 1, "total": 3, "found": 1}
    async with core.db.session() as s:
        b = (await s.execute(select(LibraryFile).where(LibraryFile.tag_title == "B"))).scalar_one()
    assert b.lyrics_checked_at is None, "pas marquée : redemandée au prochain passage"
    kinds = [e["kind"] for e in (await client.get("/api/events")).json()]
    assert "lyrics.unavailable" in kinds


async def test_apres_un_import_les_paroles_manquantes_sont_cherchees(ctx):  # noqa: F811
    """Le rangement de l'existant (comme l'import d'une chasse) lance la recherche en arrière-plan."""
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    asked: list[str] = []
    core.lyrics.client = lrclib(asked, {t: {"syncedLyrics": SYNCED} for t, _ in TRACKS})
    raw = env.music_dir / "slskd" / "Nirvana - Nevermind (1991) [FLAC]"
    for i, (title, length) in enumerate(TRACKS):
        tiny_flac(raw / f"{i + 1:02d}. {title}.flac", length // 1000)
    (raw / "01. Smells Like Teen Spirit.lrc").write_text("du pair")
    await core.library.scan()
    g = next(x for x in (await client.get("/api/library/existing")).json()["groups"] if "Nevermind" in x["dir"])
    best = (await client.post("/api/library/existing/identify", json={"dir": g["dir"]})).json()["proposals"][0]
    await client.post("/api/library/existing/apply", json={
        "dir": g["dir"], "release_group_mbid": best["release_group_mbid"],
        "release_mbid": best["release_mbid"], "follow": False,
    })

    await asyncio.gather(*core.lyrics._tasks)
    assert sorted(asked) == ["Come as You Are", "In Bloom"], "pas celle arrivée avec son .lrc"
    dest = env.music_dir / "Nirvana" / "1991 - Nevermind"
    assert (dest / "02 - In Bloom.lrc").read_text() == SYNCED
    assert (dest / "01 - Smells Like Teen Spirit.lrc").read_text() == "du pair"


def _hit(artist, title, duration, album="Autre album", synced=None, plain=None, instrumental=False):
    return {"artistName": artist, "trackName": title, "albumName": album, "duration": duration,
            "syncedLyrics": synced, "plainLyrics": plain, "instrumental": instrumental}


async def test_lrclib_range_le_morceau_ailleurs_la_recherche_le_trouve(ctx):  # noqa: F811
    """La recherche exacte rate (autre album chez LRCLIB) : la recherche par artiste et titre
    trouve, et on garde le même morceau, synchronisé de préférence."""
    client, core, engine, env = ctx
    paths = await _shelve(core, env, ["Basique (Radio Edit)"])  # 200 s, Nirvana
    core.lyrics.client = lrclib([], {}, search={"Basique": [
        _hit("Someone Else", "Basique", 200, synced=SYNCED),          # autre artiste
        _hit("Nirvana", "Basique", 230, synced=SYNCED),               # autre version (durée)
        _hit("Nirvana", "Basique", 199, plain="texte seul"),
        _hit("Nirvana feat. X", "Basique", 201.5, synced=SYNCED, album="Orelsan Songs"),
    ]})

    r = await core.lyrics.fetch_album(RG)
    assert r == {"asked": 1, "found": 1, "error": None}
    assert open(paths["Basique (Radio Edit)"].replace(".flac", ".lrc")).read() == SYNCED


async def test_jamais_les_paroles_d_une_autre_version(ctx):  # noqa: F811
    client, core, engine, env = ctx
    paths = await _shelve(core, env, ["In Bloom"])
    core.lyrics.client = lrclib([], {}, search={"In Bloom": [_hit("Nirvana", "In Bloom", 260, synced=SYNCED)]})

    assert (await core.lyrics.fetch_album(RG))["found"] == 0
    assert not os.path.exists(paths["In Bloom"].replace(".flac", ".lrc"))


async def test_le_bouton_reessaie_si_lrclib_est_sature(ctx, monkeypatch):  # noqa: F811
    client, core, engine, env = ctx
    monkeypatch.setattr(lyrics_fetch, "QUICK", (0, 0))
    await _shelve(core, env, ["Synced"])
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"message": "The server is busy"})
        return httpx.Response(200, json={"instrumental": False, "syncedLyrics": SYNCED, "plainLyrics": None})

    core.lyrics.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert (await core.lyrics.fetch_album(RG, by_user=True)) == {"asked": 1, "found": 1, "error": None}
