from .. import lyrics
from ..models import iso_utc, Artist, Candidate, LibraryFile, QualityProfile, Release, ReleaseGroup, Share


def iso(d) -> str | None:
    return iso_utc(d)


def profile_dict(p: QualityProfile) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "formats": p.formats,
        "upgrade_until": p.upgrade_until,
        "max_missing_tracks": p.max_missing_tracks,
    }


def artist_dict(a: Artist, stats: dict | None = None) -> dict:
    return {
        "id": a.id,
        "mbid": a.mbid,
        "name": a.name,
        "sort_name": a.sort_name,
        "disambiguation": a.disambiguation,
        "country": a.country,
        "type": a.type,
        "monitored": a.monitored,
        "mode": a.mode,
        "quality_profile_id": a.quality_profile_id,
        "primary_types": a.primary_types,
        "secondary_types": a.secondary_types,
        "added_at": iso(a.added_at),
        "last_synced_at": iso(a.last_synced_at),
        "stats": stats or {},
    }


def rg_dict(rg: ReleaseGroup, artist: Artist | None = None) -> dict:
    d = {
        "id": rg.id,
        "mbid": rg.mbid,
        "artist_id": rg.artist_id,
        "title": rg.title,
        "primary_type": rg.primary_type,
        "secondary_types": rg.secondary_types,
        "first_release_date": rg.first_release_date,
        "monitored": rg.monitored,
        "status": rg.status,
        "status_reason": rg.status_reason,
        "status_reason_t": rg.status_reason_t,
        "present_tracks": rg.present_tracks,
        "total_tracks": rg.total_tracks,
        "selected_release_id": rg.selected_release_id,
        "imported_format": rg.imported_format,
        "imported_path": rg.imported_path,
        "last_searched_at": iso(rg.last_searched_at),
        "updated_at": iso(rg.updated_at),
    }
    if artist is not None:
        d["artist"] = {"id": artist.id, "name": artist.name, "mbid": artist.mbid, "mode": artist.mode}
    return d


def release_dict(r: Release, with_tracks: bool = True) -> dict:
    d = {
        "id": r.id,
        "mbid": r.mbid,
        "title": r.title,
        "date": r.date,
        "country": r.country,
        "status": r.status,
        "disambiguation": r.disambiguation,
        "formats": r.formats,
        "track_count": r.track_count,
        "disc_count": r.disc_count,
        "label": r.label,
        "barcode": r.barcode,
    }
    if with_tracks:
        d["tracks"] = [
            {
                "id": t.id,
                "mbid": t.mbid,
                "recording_mbid": t.recording_mbid,
                "disc": t.disc,
                "position": t.position,
                "number": t.number,
                "title": t.title,
                "artist_credit": t.artist_credit,
                "length_ms": t.length_ms,
            }
            for t in r.tracks
        ]
    return d


def candidate_dict(c: Candidate) -> dict:
    return {
        "id": c.id,
        "release_group_id": c.release_group_id,
        "search_run_id": c.search_run_id,
        "username": c.username,
        "directory": c.directory,
        "files": c.files,
        "format": c.format,
        "score": c.score,
        "score_detail": c.score_detail,
        "status": c.status,
        "reject_reason": c.reject_reason,
        "reject_reason_t": c.reject_reason_t,
        "matched_release_id": c.matched_release_id,
        "has_free_slots": c.has_free_slots,
        "queue_size": c.queue_size,
        "avg_speed": c.avg_speed,
        "download_dir": c.download_dir,
        "import_plan": c.import_plan,
        "created_at": iso(c.created_at),
    }


def share_dict(s: Share) -> dict:
    return {
        "id": s.id,
        "path": s.path,
        "enabled": s.enabled,
        "allowed_extensions": s.allowed_extensions,
        "exclude_patterns": s.exclude_patterns,
        "file_count": s.file_count,
        "total_size": s.total_size,
        "excluded_count": s.excluded_count,
        "last_scan_at": iso(s.last_scan_at),
    }


def library_file_dict(f: LibraryFile) -> dict:
    return {
        "id": f.id,
        "path": f.path,
        "size": f.size,
        "format": f.format,
        "bitrate": f.bitrate,
        "duration": f.duration,
        "tag_artist": f.tag_artist,
        "tag_album": f.tag_album,
        "tag_title": f.tag_title,
        "release_group_mbid": f.release_group_mbid,
        "release_mbid": f.release_mbid,
        "recording_mbid": f.recording_mbid,
        "lyrics": file_lyrics(f),
    }


def file_lyrics(f: LibraryFile) -> dict | None:
    """The lyrics Navidrome plays for this file: the file next to it first, as its default
    LyricsPriority (.lrc, .txt, then embedded)."""
    if f.lyrics_file:
        return {"kind": lyrics.kind_of_file(f.lyrics_file), "source": "file", "ext": f.lyrics_file}
    if f.lyrics_embedded:
        return {"kind": f.lyrics_embedded, "source": "embedded", "ext": None}
    return None
