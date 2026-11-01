"""Lecture et écriture des tags audio (mutagen), compatibles Picard / Navidrome."""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TXXX, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

from .quality import FileInfo, detect_format, extension_of

log = logging.getLogger(__name__)

# Paroles intégrées : jamais effacées par write_tags (elles ont pu être recalées à la main)
VORBIS_LYRICS = {"LYRICS", "UNSYNCEDLYRICS", "SYNCEDLYRICS"}
MP4_LYRICS = "\xa9lyr"
_LRC_TIME = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")


@dataclass
class AudioMeta:
    duration: float | None = None
    bitrate: int | None = None
    format: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    albumartist: str | None = None
    track: int | None = None
    disc: int | None = None
    release_group_mbid: str | None = None
    release_mbid: str | None = None
    recording_mbid: str | None = None
    lyrics: str | None = None  # paroles intégrées : « synced », « plain » ou rien


def _lyrics_kind(texts: list[str]) -> str | None:
    texts = [x for x in texts if x and x.strip()]
    if not texts:
        return None
    return "synced" if any(_LRC_TIME.search(x) for x in texts) else "plain"


def embedded_lyrics(raw) -> str | None:
    """Paroles intégrées au fichier (déjà ouvert par mutagen) : « synced », « plain » ou None."""
    tags = getattr(raw, "tags", None)
    if tags is None:
        return None
    if isinstance(tags, ID3):
        if tags.getall("SYLT"):
            return "synced"
        return _lyrics_kind([str(f) for f in tags.getall("USLT")])
    if isinstance(raw, MP4):
        return _lyrics_kind([str(v) for v in tags.get(MP4_LYRICS, [])])
    try:
        return _lyrics_kind([v for k in tags.keys() if k.upper() in VORBIS_LYRICS for v in tags[k]])
    except Exception:
        return None


def _first(tags, key: str) -> str | None:
    try:
        v = tags.get(key)
    except (KeyError, ValueError):
        return None
    if not v:
        return None
    return str(v[0]) if isinstance(v, list) else str(v)


def _num(v: str | None) -> int | None:
    if not v:
        return None
    try:
        return int(str(v).split("/")[0])
    except ValueError:
        return None


def read_meta(path: str | Path) -> AudioMeta:
    meta = AudioMeta()
    try:
        raw = mutagen.File(path)
    except Exception as e:
        log.debug("reading %s: %s", path, e)
        return meta
    if raw is None:
        return meta
    meta.lyrics = embedded_lyrics(raw)
    info = getattr(raw, "info", None)
    ext = extension_of(str(path))
    if info is not None:
        meta.duration = getattr(info, "length", None)
        br = getattr(info, "bitrate", None)
        meta.bitrate = round(br / 1000) if br else None
        codec = getattr(info, "codec", "") or ""
        if ext == "m4a" and "alac" in codec.lower():
            ext = "alac"
        meta.format = detect_format(
            FileInfo(
                ext,
                bitrate=meta.bitrate,
                bit_depth=getattr(info, "bits_per_sample", None),
                sample_rate=getattr(info, "sample_rate", None),
                vbr=getattr(info, "bitrate_mode", 0) not in (0, 1),
            )
        )
    try:
        easy = mutagen.File(path, easy=True)
    except Exception:
        easy = None
    tags = getattr(easy, "tags", None) if easy is not None else None
    if tags is not None:
        meta.title = _first(tags, "title")
        meta.artist = _first(tags, "artist")
        meta.album = _first(tags, "album")
        meta.albumartist = _first(tags, "albumartist")
        meta.track = _num(_first(tags, "tracknumber"))
        meta.disc = _num(_first(tags, "discnumber"))
        meta.release_mbid = _first(tags, "musicbrainz_albumid")
        meta.release_group_mbid = _first(tags, "musicbrainz_releasegroupid")
        meta.recording_mbid = _first(tags, "musicbrainz_trackid")
    return meta


@dataclass
class TagSet:
    title: str
    artist: str
    album: str
    albumartist: str
    date: str
    tracknumber: int
    tracktotal: int
    discnumber: int
    disctotal: int
    musicbrainz_trackid: str  # recording
    musicbrainz_releasetrackid: str
    musicbrainz_albumid: str
    musicbrainz_releasegroupid: str
    musicbrainz_artistid: str
    musicbrainz_albumartistid: str
    releasetype: str = ""
    label: str = ""
    barcode: str = ""
    extra: dict = field(default_factory=dict)


# Clés « Picard » en Vorbis comments (FLAC/Ogg/Opus)
def _vorbis_map(t: TagSet) -> dict[str, str]:
    return {
        "TITLE": t.title,
        "ARTIST": t.artist,
        "ALBUM": t.album,
        "ALBUMARTIST": t.albumartist,
        "DATE": t.date,
        "ORIGINALDATE": t.date,
        "TRACKNUMBER": str(t.tracknumber),
        "TRACKTOTAL": str(t.tracktotal),
        "TOTALTRACKS": str(t.tracktotal),
        "DISCNUMBER": str(t.discnumber),
        "DISCTOTAL": str(t.disctotal),
        "TOTALDISCS": str(t.disctotal),
        "RELEASETYPE": t.releasetype,
        "LABEL": t.label,
        "BARCODE": t.barcode,
        "MUSICBRAINZ_TRACKID": t.musicbrainz_trackid,
        "MUSICBRAINZ_RELEASETRACKID": t.musicbrainz_releasetrackid,
        "MUSICBRAINZ_ALBUMID": t.musicbrainz_albumid,
        "MUSICBRAINZ_RELEASEGROUPID": t.musicbrainz_releasegroupid,
        "MUSICBRAINZ_ARTISTID": t.musicbrainz_artistid,
        "MUSICBRAINZ_ALBUMARTISTID": t.musicbrainz_albumartistid,
    }


def _vorbis_lyrics(tags) -> list[tuple[str, list[str]]]:
    return [(k, list(tags[k])) for k in tags.keys() if k.upper() in VORBIS_LYRICS] if tags else []


def write_tags(path: str | Path, t: TagSet, cover: bytes | None = None) -> None:
    """Remplace les tags du fichier par ceux issus de MusicBrainz (+ pochette embarquée).
    Les paroles intégrées sont gardées."""
    path = str(path)
    ext = extension_of(path)
    vmap = {k: v for k, v in _vorbis_map(t).items() if v}

    if ext == "flac":
        f = FLAC(path)
        kept = _vorbis_lyrics(f.tags)
        f.delete()
        for k, v in [*vmap.items(), *kept]:
            f[k] = v
        if cover:
            f.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = cover
            f.add_picture(pic)
        f.save()
        return

    if ext in {"ogg", "opus"}:
        f = mutagen.File(path)
        if f is None:
            return
        if f.tags is None:
            f.add_tags()
        kept = _vorbis_lyrics(f.tags)
        f.tags.clear()
        for k, v in [*vmap.items(), *kept]:
            f.tags[k] = v
        f.save()
        return

    if ext == "mp3":
        from mutagen.easyid3 import EasyID3

        try:
            old = ID3(path)
            kept = old.getall("USLT") + old.getall("SYLT")
            old.delete()
        except ID3NoHeaderError:
            kept = []
        e = EasyID3()
        e["title"] = t.title
        e["artist"] = t.artist
        e["album"] = t.album
        e["albumartist"] = t.albumartist
        if t.date:
            e["date"] = t.date
            e["originaldate"] = t.date
        e["tracknumber"] = f"{t.tracknumber}/{t.tracktotal}"
        e["discnumber"] = f"{t.discnumber}/{t.disctotal}"
        e["musicbrainz_trackid"] = t.musicbrainz_trackid
        e["musicbrainz_releasetrackid"] = t.musicbrainz_releasetrackid
        e["musicbrainz_albumid"] = t.musicbrainz_albumid
        e["musicbrainz_releasegroupid"] = t.musicbrainz_releasegroupid
        e["musicbrainz_artistid"] = t.musicbrainz_artistid
        e["musicbrainz_albumartistid"] = t.musicbrainz_albumartistid
        if t.barcode:
            e["barcode"] = t.barcode
        e.save(path, v2_version=4)
        id3 = ID3(path)
        if t.releasetype:
            id3.add(TXXX(encoding=3, desc="MusicBrainz Album Type", text=[t.releasetype]))
        if cover:
            id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        for frame in kept:
            id3.add(frame)
        id3.save(v2_version=4)
        return

    if ext in {"m4a", "alac", "aac", "mp4"}:
        f = MP4(path)
        kept = (f.tags or {}).get(MP4_LYRICS)
        f.delete()
        if kept:
            f[MP4_LYRICS] = kept
        f["\xa9nam"] = t.title
        f["\xa9ART"] = t.artist
        f["\xa9alb"] = t.album
        f["aART"] = t.albumartist
        if t.date:
            f["\xa9day"] = t.date
        f["trkn"] = [(t.tracknumber, t.tracktotal)]
        f["disk"] = [(t.discnumber, t.disctotal)]
        free = {
            "MusicBrainz Track Id": t.musicbrainz_trackid,
            "MusicBrainz Release Track Id": t.musicbrainz_releasetrackid,
            "MusicBrainz Album Id": t.musicbrainz_albumid,
            "MusicBrainz Release Group Id": t.musicbrainz_releasegroupid,
            "MusicBrainz Artist Id": t.musicbrainz_artistid,
            "MusicBrainz Album Artist Id": t.musicbrainz_albumartistid,
        }
        for k, v in free.items():
            if v:
                f[f"----:com.apple.iTunes:{k}"] = [v.encode()]
        if cover:
            f["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
        return

    log.info("tag writing not supported for .%s: %s", ext, path)
