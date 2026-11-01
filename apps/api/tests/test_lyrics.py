"""Paroles : les .lrc suivent leur piste, les paroles intégrées survivent au tagage, et on voit
par piste ce qu'il y a (issue #1)."""

from fakes import TRACKS, peer_with_album, tiny_flac
from mutagen.flac import FLAC
from mutagen.id3 import ID3, SYLT, USLT
from test_flow import ctx, wait_for  # noqa: F401

from soulsick import lyrics
from soulsick.audiotags import TagSet, read_meta, write_tags
from soulsick.scoring import RemoteFile

SYNCED = "[00:01.00] Load up on guns\n[00:03.50] Bring your friends\n"


def _tags() -> TagSet:
    return TagSet(
        title="In Bloom", artist="Nirvana", album="Nevermind", albumartist="Nirvana", date="1991",
        tracknumber=2, tracktotal=3, discnumber=1, disctotal=1, musicbrainz_trackid="r",
        musicbrainz_releasetrackid="t", musicbrainz_albumid="a", musicbrainz_releasegroupid="g",
        musicbrainz_artistid="ar", musicbrainz_albumartistid="ar",
    )


def test_les_paroles_suivent_la_piste_et_prennent_son_nom(tmp_path):
    src = tmp_path / "in" / "02. in bloom.flac"
    tiny_flac(src, 3)
    (tmp_path / "in" / "02. in bloom.lrc").write_text(SYNCED)
    (tmp_path / "in" / "02. in bloom.TXT").write_text("Load up on guns")
    (tmp_path / "in" / "02. in bloom (live).lrc").write_text("une autre piste")
    dest = tmp_path / "out" / "02 - In Bloom.flac"

    moved = lyrics.follow(str(src), str(dest))
    assert sorted(moved) == sorted([str(dest.with_suffix(".lrc")), str(dest.with_suffix(".TXT"))])
    assert dest.with_suffix(".lrc").read_text() == SYNCED
    assert (tmp_path / "in" / "02. in bloom (live).lrc").exists(), "le .lrc d'une autre piste reste"


def test_des_paroles_deja_la_ne_sont_pas_ecrasees(tmp_path):
    """Une piste remplacée sous le même nom : son .lrc (peut-être recalé à la main) l'emporte."""
    src, dest = tmp_path / "new" / "01.flac", tmp_path / "lib" / "01 - Song.flac"
    tiny_flac(src, 3)
    (tmp_path / "new" / "01.lrc").write_text("nouveau")
    dest.parent.mkdir()
    dest.with_suffix(".lrc").write_text("recalé à la main")

    assert lyrics.follow(str(src), str(dest)) == []
    assert dest.with_suffix(".lrc").read_text() == "recalé à la main"


def test_une_piste_remplacee_sous_un_autre_nom_garde_ses_paroles(tmp_path):
    old, new = tmp_path / "01 - Song (Remastered).mp3", tmp_path / "01 - Song.flac"
    old.write_bytes(b"mp3")
    old.with_suffix(".lrc").write_text("recalé à la main")
    tiny_flac(new, 3)
    new.with_suffix(".lrc").write_text("arrivé avec la nouvelle version")

    lyrics.drop_replaced(str(old), str(new))
    assert not old.exists()
    assert new.with_suffix(".lrc").read_text() == "recalé à la main"
    assert not old.with_suffix(".lrc").exists()


def test_write_tags_garde_les_paroles_flac(tmp_path):
    path = tmp_path / "a.flac"
    tiny_flac(path, 3)
    f = FLAC(path)
    f["LYRICS"] = SYNCED
    f["UNSYNCEDLYRICS"] = "Load up on guns"
    f["COMMENT"] = "rippé par X"
    f.save()
    assert read_meta(path).lyrics == "synced"

    write_tags(path, _tags())
    f = FLAC(path)
    assert f["LYRICS"] == [SYNCED] and f["UNSYNCEDLYRICS"] == ["Load up on guns"]
    assert "COMMENT" not in f, "le reste est bien remplacé"
    assert f["TITLE"] == ["In Bloom"]


def test_write_tags_garde_les_paroles_mp3(tmp_path):
    path = tmp_path / "a.mp3"
    path.write_bytes(b"\xff\xfb" + b"\x00" * 400)
    id3 = ID3()
    id3.add(USLT(encoding=3, lang="eng", desc="", text="Load up on guns"))
    id3.add(SYLT(encoding=3, lang="eng", format=2, type=1, desc="", text=[("Load up on guns", 1000)]))
    id3.save(path)

    write_tags(path, _tags())
    id3 = ID3(path)
    assert str(id3.getall("USLT")[0]) == "Load up on guns"
    assert id3.getall("SYLT")
    assert str(id3["TIT2"]) == "In Bloom"


def test_paroles_integrees_texte_simple(tmp_path):
    path = tmp_path / "a.flac"
    tiny_flac(path, 3)
    f = FLAC(path)
    f["LYRICS"] = "Load up on guns, bring your friends"
    f.save()
    assert read_meta(path).lyrics == "plain"
    tiny_flac(tmp_path / "b.flac", 3)
    assert read_meta(tmp_path / "b.flac").lyrics is None


async def test_rangement_de_l_existant_avec_paroles(ctx):  # noqa: F811
    """Le cas de l'issue : un album sans tags MusicBrainz, avec ses .lrc, reconnu puis rangé."""
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    raw = env.music_dir / "slskd" / "Nirvana - Nevermind (1991) [FLAC]"
    for i, (title, length) in enumerate(TRACKS):
        tiny_flac(raw / f"{i + 1:02d}. {title}.flac", length // 1000)
    (raw / "01. Smells Like Teen Spirit.lrc").write_text(SYNCED)
    (raw / "02. In Bloom.txt").write_text("Load up on guns")
    f = FLAC(raw / "03. Come as You Are.flac")
    f["LYRICS"] = "Come as you are"
    f.save()
    await core.library.scan()

    g = next(x for x in (await client.get("/api/library/existing")).json()["groups"] if "Nevermind" in x["dir"])
    best = (await client.post("/api/library/existing/identify", json={"dir": g["dir"]})).json()["proposals"][0]
    r = await client.post("/api/library/existing/apply", json={
        "dir": g["dir"], "release_group_mbid": best["release_group_mbid"],
        "release_mbid": best["release_mbid"], "follow": False,
    })
    assert r.status_code == 200, r.text
    dest = env.music_dir / "Nirvana" / "1991 - Nevermind"
    assert (dest / "01 - Smells Like Teen Spirit.lrc").read_text() == SYNCED
    assert (dest / "02 - In Bloom.txt").read_text() == "Load up on guns", "le .txt prend le nom de sa piste"
    assert not raw.exists(), "plus rien ne retient l'ancien dossier"
    assert read_meta(dest / "03 - Come as You Are.flac").lyrics == "plain", "survivent au tagage"

    album = (await client.get(f"/api/albums/{r.json()['release_group_id']}")).json()
    by_title = {f["tag_title"]: f["lyrics"] for f in album["files"]}
    assert by_title == {
        "Smells Like Teen Spirit": {"kind": "synced", "source": "file", "ext": "lrc"},
        "In Bloom": {"kind": "plain", "source": "file", "ext": "txt"},
        "Come as You Are": {"kind": "plain", "source": "embedded", "ext": None},
    }

    # Un .lrc ajouté à la main se voit au scan suivant, sans toucher à la piste
    (dest / "03 - Come as You Are.lrc").write_text(SYNCED)
    await core.library.scan()
    album = (await client.get(f"/api/albums/{r.json()['release_group_id']}")).json()
    come = next(f for f in album["files"] if f["tag_title"] == "Come as You Are")
    assert come["lyrics"] == {"kind": "synced", "source": "file", "ext": "lrc"}


async def test_la_chasse_telecharge_les_paroles_en_premier(ctx):  # noqa: F811
    """Même sans les pochettes, et avant l'audio : l'album est importé dès que l'audio est là."""
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    settings = await core.settings.get()
    settings.hunter.download_covers = False
    await core.settings.save(settings)
    r = await client.post("/api/artists", json={
        "mbid": "5b11f4ce-a62d-471e-81fc-a69a8278c7da", "mode": "validation",
        "primary_types": ["album"], "monitor": "all",
    })
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    peer = peer_with_album()
    peer.files += [
        RemoteFile("@@music\\Nirvana\\Nevermind\\01 - Smells Like Teen Spirit.lrc", size=10),
        RemoteFile("@@music\\Nirvana\\Nevermind\\folder.jpg", size=10),
    ]
    engine.results = [peer]
    await core.hunter.hunt(rid)
    cand = (await client.get(f"/api/albums/{rid}")).json()["candidates"][0]
    await client.post(f"/api/candidates/{cand['id']}/approve")

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)
    asked = [p.rsplit("\\", 1)[-1] for _, p, _ in engine.downloads]
    assert asked[0] == "01 - Smells Like Teen Spirit.lrc", asked
    assert "folder.jpg" not in asked, "les pochettes du pair n'étaient pas demandées"
    lrc = env.music_dir / "Nirvana" / "1991 - Nevermind" / "01 - Smells Like Teen Spirit.lrc"
    assert lrc.read_text().startswith("[00:01.00]")
