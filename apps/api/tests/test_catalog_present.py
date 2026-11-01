"""Une bibliothèque existante (tags MusicBrainz, ex. beets) apparaît sans suivre ses artistes (issue #9)."""

from fakes import ARTIST_MBID, RG_MBID, TRACKS, tiny_flac
from mutagen.flac import FLAC
from test_flow import ctx  # noqa: F401


def _beets_album(env) -> None:
    """Un album rangé par beets : ses propres chemins, tagué MusicBrainz, inconnu du catalogue."""
    for i, (title, length) in enumerate(TRACKS):
        p = env.music_dir / "beets" / "Nirvana" / "Nevermind" / f"{i + 1:02d} {title}.flac"
        tiny_flac(p, length // 1000)
        f = FLAC(p)
        f["TITLE"], f["ARTIST"], f["ALBUM"] = title, "Nirvana", "Nevermind"
        f["MUSICBRAINZ_RELEASEGROUPID"], f["MUSICBRAINZ_ALBUMARTISTID"] = RG_MBID, ARTIST_MBID
        f.save()


async def test_les_albums_presents_apparaissent_sans_suivre(ctx):  # noqa: F811
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    discographies: list[str] = []

    async def no_discography(mbid):
        discographies.append(mbid)
        return []

    core.mb.artist_release_groups = no_discography
    _beets_album(env)
    await core.library.scan()
    await core.catalog.from_library()

    artists = (await client.get("/api/artists")).json()
    assert [(a["name"], a["monitored"]) for a in artists] == [("Nirvana", False)], "présent, pas suivi"
    shelved = (await client.get("/api/library/albums")).json()
    assert [a["title"] for a in shelved] == ["Nevermind"]
    assert shelved[0]["status"] == "imported"
    assert str(env.music_dir / "beets") in shelved[0]["imported_path"], "les fichiers restent où ils sont"
    assert discographies == [], "pas de discographie complète tant qu'on ne suit pas"
    assert engine.downloads == [], "rien n'est téléchargé"

    # Un scan suivant ne recrée rien
    await core.library.scan()
    await core.catalog.from_library()
    assert len((await client.get("/api/artists")).json()) == 1
    assert len((await client.get("/api/library/albums")).json()) == 1
