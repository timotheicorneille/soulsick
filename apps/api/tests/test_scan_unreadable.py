"""A library that can't be read for a moment (too many open files, a share that hiccups) is not
a library that was emptied: the scan keeps what it knew about what it could not read."""

import errno
import os

from fakes import ARTIST_MBID, RG_MBID, TRACKS, tiny_flac
from mutagen import MutagenError
from mutagen.flac import FLAC
from sqlalchemy import func, select
from test_flow import ctx  # noqa: F401

from soulsick import audiotags
from soulsick.models import LibraryFile


def _library(env):
    """An album tagged with MusicBrainz, and a loose file next to it."""
    album = env.music_dir / "Nirvana" / "Nevermind"
    for i, (title, length) in enumerate(TRACKS):
        p = album / f"{i + 1:02d} {title}.flac"
        tiny_flac(p, length // 1000)
        f = FLAC(p)
        f["TITLE"], f["ARTIST"], f["ALBUM"] = title, "Nirvana", "Nevermind"
        f["MUSICBRAINZ_RELEASEGROUPID"], f["MUSICBRAINZ_ALBUMARTISTID"] = RG_MBID, ARTIST_MBID
        f.save()
    tiny_flac(env.music_dir / "loose" / "demo.flac", 3)
    return album


async def _shelved(client, core):
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    await core.library.scan()
    await core.catalog.from_library()
    albums = (await client.get("/api/library/albums")).json()
    assert [(a["title"], a["status"]) for a in albums] == [("Nevermind", "imported")]


async def _tagged(core) -> int:
    async with core.db.session() as s:
        return (await s.execute(
            select(func.count()).select_from(LibraryFile).where(LibraryFile.release_group_mbid == RG_MBID)
        )).scalar_one()


async def test_a_folder_that_cannot_be_listed_is_not_forgotten(ctx, monkeypatch):  # noqa: F811
    client, core, engine, env = ctx
    album = _library(env)
    await _shelved(client, core)

    real = os.scandir

    def scandir(path="."):
        if os.fspath(path) == str(album):
            raise OSError(errno.EMFILE, "Too many open files", os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    stats = await core.library.scan()
    assert stats["removed"] == 0, "nothing is forgotten because a folder could not be listed"
    assert await _tagged(core) == len(TRACKS)
    assert [a["status"] for a in (await client.get("/api/library/albums")).json()] == ["imported"]
    events = (await client.get("/api/events", params={"kind": "library.scan_partial"})).json()
    assert events and events[0]["level"] == "warning", "and the incomplete scan is said"

    # Listed again: a file really deleted is forgotten as before
    monkeypatch.setattr(os, "scandir", real)
    os.remove(next(album.glob("01 *.flac")))
    assert (await core.library.scan())["removed"] == 1


async def test_a_file_that_cannot_be_opened_keeps_its_tags(ctx, monkeypatch):  # noqa: F811
    client, core, engine, env = ctx
    album = _library(env)
    await _shelved(client, core)

    # Every file looks changed, and none can be opened
    for p in album.glob("*.flac"):
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 60))
    real = audiotags.mutagen.File

    def unreadable(path, *a, **kw):
        raise MutagenError(OSError(errno.EMFILE, "Too many open files", str(path)))

    monkeypatch.setattr(audiotags.mutagen, "File", unreadable)
    await core.library.scan()
    assert await _tagged(core) == len(TRACKS), "an unreadable file is not a file without tags"
    assert [a["status"] for a in (await client.get("/api/library/albums")).json()] == ["imported"]

    # Readable again: the files are read at the next scan, nothing was marked as done
    monkeypatch.setattr(audiotags.mutagen, "File", real)
    stats = await core.library.scan()
    assert stats["new_or_changed"] == len(TRACKS)
    assert await _tagged(core) == len(TRACKS)
