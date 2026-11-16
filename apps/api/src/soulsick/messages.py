"""User-facing server messages.

Every sentence the server shows to a person (journal entries, album/candidate
reasons, API errors) has a key. The English text is rendered here; the web UI
renders other languages from the same key + params (apps/web/src/i18n/server.*.ts).

Templates use `{name}` placeholders. "singular | plural" picks a form from
`params["n"]`. A param can itself be a `Msg` (nested sentence).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EN: dict[str, str] = {
    # --- small words used inside other sentences
    "word.manual": "manual",
    "word.automatic": "automatic",
    "word.automatically": "automatically",
    "word.by_you": "by you",
    "word.none": "none",
    "word.default": "default",
    "word.enabled": "enabled",
    "word.disabled": "disabled",
    "word.unknown_format": "unknown format",
    "word.unknown_reason": "unknown reason",
    "word.ask_me": "ask me",
    "join": "{a}; {b}",
    "raw": "{text}",

    # --- album reasons (status_reason)
    "reason.no_edition": "No edition known on MusicBrainz",
    "reason.soulseek_unavailable": "Soulseek unavailable: {error}",
    "reason.no_valid": "No valid candidate among {n} folder | No valid candidate among {n} folders",
    "reason.awaiting": "{n} proposal awaiting approval | {n} proposals awaiting approval",
    "reason.from_peer": "From {peer}",
    "reason.complement_from": "Completing from {peer}",
    "reason.resume_from": "Resuming from {peer}",
    "reason.chosen_failed": "The chosen candidate failed, pick another one",
    "reason.last_failed": "Last attempt failed: {reason}",
    "reason.refused_after": "{n} file refused after {max} attempts: {reasons} | {n} files refused after {max} attempts: {reasons}",
    "reason.cancelled_by_you": "{n} file cancelled by you | {n} files cancelled by you",
    "reason.stalled": "no transfer started within {minutes} minutes (peer away or queue full)",
    "reason.dismissed": "Proposals rejected",
    "reason.restart_interrupted": "Search interrupted by a restart",
    "reason.import_interrupted": "Import interrupted by a restart: re-examined",
    "reason.files_missing": "Files missing from the library",
    "reason.upgrading": "Present as {format}, looking for better quality",
    "reason.imported_upgrading": "Imported as {format}, looking for better quality",
    "reason.import_error": "Import error: {error}",
    "reason.waiting_files": "Files received from {peer}, waiting for {names}",
    "reason.incomplete": "{received}/{total} tracks received — missing: {missing}",
    "reason.incomplete_action": "{reason}. Complete from another peer.",
    "reason.uncertain": "Uncertain mapping: {problems}",
    "reason.partial": "Partial: {n}/{total} tracks in the library",
    "reason.low_confidence": "confidence {score} % < {min} %",
    "reason.unmatched_files": "{n} file without a track | {n} files without a track",
    "reason.abandoned": "Source dropped by you",
    "reason.taken_elsewhere": "Taken from {peer} instead",
    "reason.switched": "Replaced by {peer}",
    "reason.switched_search": "Source {peer} dropped, searching for another peer",
    "reason.abandoned_pick": "Source dropped by you: pick another candidate",
    "reason.retry_by_you": "retry requested by you",
    "reason.library_unavailable": "Waiting for the library folder: {problem}",
    "reason.restart_retried": "file retried on restart | files retried on restart",

    # --- candidate rejections (reject_reason)
    "reject.not_better": "Not better than the present version ({format})",
    "reject.unknown_format": "Unrecognised format on some files",
    "reject.format_refused": "Format {format} not allowed by the profile",
    "reject.no_tracklist": "No MusicBrainz tracklist available",
    "reject.missing_tracks": "{n} track missing out of {total} (allowed: {allowed}) | {n} tracks missing out of {total} (allowed: {allowed})",
    "reject.titles": "Titles too different from the tracklist ({score} % < {min} %)",

    # --- journal
    "app.started": "Soulsick started",
    "transfer.repaired": "{n} retried transfer reattached to its album | {n} retried transfers reattached to their album",
    "transfer.repaired_requeued": "{n} file of an album already imported was being downloaded again (aioslsk bug, fixed): closed | {n} files of albums already imported were being downloaded again (aioslsk bug, fixed): closed",
    "download.resumed": "{n} download resumed after restart | {n} downloads resumed after restart",
    "hunt.error": "{label}: couldn’t fetch the MusicBrainz tracklist ({error})",
    "hunt.started": "Searching “{label}” ({trigger}) — profile “{profile}”, queries: {queries}",
    "hunt.skipped": "{label}: search impossible, {error}",
    "search.done": "Query “{query}”: {files} files from {peers} peers",
    "hunt.no_candidate": "{label}: {reason}. Retrying in {hours} h.",
    "hunt.scored": "{label}: {n} valid candidate out of {total} folders. Best: {peer} · {dir} — {score}/100 ({format}, {matched}/{expected} tracks) | {label}: {n} valid candidates out of {total} folders. Best: {peer} · {dir} — {score}/100 ({format}, {matched}/{expected} tracks)",
    "hunt.awaiting_approval": "{label}: {n} proposal waiting for your approval (artist in ask-me mode) | {label}: {n} proposals waiting for your approval (artist in ask-me mode)",
    "download.started": "{label}: downloading {n} file from {peer} ({dir}) to {dest} — chosen {by} | {label}: downloading {n} files from {peer} ({dir}) to {dest} — chosen {by}",
    "download.error": "{label}: download impossible, {error}",
    "download.retry_scheduled": "{n} file refused by {peer} ({names}) — retry {attempt}/{max} in {delay} | {n} files refused by {peer} ({names}) — retry {attempt}/{max} in {delay}",
    "download.complete": "All files from {peer} arrived ({n} tracks) — importing",
    "download.complete_late": "All files from {peer} arrived after all ({n} tracks), despite giving up — importing",
    "download.complement": "{label}: completing {n} track from {peer} ({titles}) — chosen by you | {label}: completing {n} tracks from {peer} ({titles}) — chosen by you",
    "download.complement_mixed": "{label}: completing {n} track from {peer} ({titles}) — chosen by you. Warning: mixed formats ({received} + {format}) | {label}: completing {n} tracks from {peer} ({titles}) — chosen by you. Warning: mixed formats ({received} + {format})",
    "download.reactivated": "Candidate {peer} reactivated: {why}",
    "download.resumed_candidate": "Resuming download from {peer}: {n} file requested again, {done} already received kept | Resuming download from {peer}: {n} files requested again, {done} already received kept",
    "download.failed": "{label}: candidate dropped — {reason}",
    "download.abandoned": "{label}: source {peer} dropped by you ({n} unfinished file cancelled, what arrived is kept) | {label}: source {peer} dropped by you ({n} unfinished files cancelled, what arrived is kept)",
    "download.stuck": "{name} from {peer} looks stuck: {why}",
    "download.relayed": "Refused track(s) asked to {peer} instead ({n})",
    "download.switched": "{label}: switching from {old} to {new} ({n} track not received yet) | {label}: switching from {old} to {new} ({n} tracks not received yet)",
    "download.switched_search": "{label}: source {peer} dropped, searching for another peer",
    "stuck.no_progress": "no data received for {minutes} min",
    "stuck.queue_frozen": "queue position not moving for {minutes} min",
    "stuck.retrying": "attempt {n}",
    "hunt.fallback": "{label}: switching to the next candidate, {next}",
    "hunt.dismissed": "“{title}”: proposals rejected, will retry later",
    "album.release_selected": "“{title}”: edition set to {release} ({date}, {country}, {formats}, {n} tracks)",
    "album.monitored": "“{title}” added to wanted albums",
    "album.unmonitored": "“{title}” is no longer followed",
    "album.bulk.all": "{name}: {n} release now followed | {name}: {n} releases now followed",
    "album.bulk.none": "{name}: {n} release no longer followed | {name}: {n} releases no longer followed",
    "album.bulk.future": "{name}: {n} release followed only if upcoming | {name}: {n} releases followed only if upcoming",
    "album.bulk.section.all": "{name}: {n} release ({section}) now followed | {name}: {n} releases ({section}) now followed",
    "album.bulk.section.none": "{name}: {n} release ({section}) no longer followed | {name}: {n} releases ({section}) no longer followed",
    "album.bulk.section.future": "{name}: {n} release ({section}) followed only if upcoming | {name}: {n} releases ({section}) followed only if upcoming",
    "album.bulk.types": "{name}: {n} release of a type no longer followed is no longer followed | {name}: {n} releases of a type no longer followed are no longer followed",
    "artist.added": "Artist added: {name} ({mode} mode)",
    "artist.removed": "Artist removed: {name} (files in the library are kept)",
    "artist.removed_shared": "Artist removed: {name} (files in the library are kept; {kept} shared release stays with the other artist) | Artist removed: {name} (files in the library are kept; {kept} shared releases stay with the other artist)",
    "artist.updated": "{name}: {changes}",
    "artist.change.mode": "mode {value}",
    "artist.change.monitored_on": "following resumed",
    "artist.change.monitored_off": "following paused",
    "artist.change.profile": "profile “{value}”",
    "artist.change.primary_types": "release types: {value}",
    "artist.change.secondary_types": "secondary types: {value}",
    "musicbrainz.synced": "{name}’s discography synced: {total} releases on MusicBrainz{details}",
    "musicbrainz.detail.added": ", {n} new followed: {titles} | , {n} new followed: {titles}",
    "musicbrainz.detail.shared": ", {n} shared with another followed artist ({titles}) | , {n} shared with another followed artist ({titles})",
    "musicbrainz.sync_failed": "MusicBrainz sync failed for {name}: {error}",
    "library.cataloged": "Already in your library: {albums} album(s) of {artists} new artist(s) added, not followed",
    "library.scanned": "Library scanned: {n} audio files ({changed} new/changed, {removed} gone)",
    "lyrics.fetched": "Lyrics found on LRCLIB for {n} track(s) out of {total} without",
    "lyrics.library_done": "Lyrics: {n} found on LRCLIB out of {total} tracks asked",
    "lyrics.unavailable": "LRCLIB doesn’t answer ({error}): stopped after {done} of {total} tracks, the rest next time",
    "library.scan_skipped": "Library scan skipped: {problem}",
    "library.guard.missing": "the library folder {path} doesn’t exist",
    "library.guard.not_mounted": "{path} isn’t mounted from your computer (check the volumes in docker-compose.yml): music would stay inside the container",
    "library.guard.anonymous": "{path} is an anonymous Docker volume, not your music folder (check the volumes in docker-compose.yml)",
    "library.guard.readonly": "the library folder {path} isn’t writable",
    "library.guard.emptied": "{path} is empty while {n} file was known (disk unplugged or not mounted?) | {path} is empty while {n} files were known (disk unplugged or not mounted?)",
    "library.guard.folder_not_mounted": "The {name} folder ({path}) isn’t mounted from your computer: its content is lost when the container is recreated",
    "library.guard.blocked": "Library unavailable, imports on hold: {problem}",
    "library.guard.back": "Library folder {path} available again: imports resume",
    "library.guard.confirmed_empty": "Library confirmed empty by you: {n} file forgotten | Library confirmed empty by you: {n} files forgotten",
    "library.identify_started": "Identifying {n} folder of existing music | Identifying {n} folders of existing music",
    "library.identify_done": "Identification done: {n} folder, {safe} safe match | Identification done: {n} folders, {safe} safe matches",
    "import.existing": "{artist} – {title}: {n} existing file tagged and shelved in {dir}{details} | {artist} – {title}: {n} existing files tagged and shelved in {dir}{details}",
    "import.existing_extras": ", {n} extra moved | , {n} extras moved",
    "import.manual": "{artist} – {title}: {n} manually downloaded track tagged and shelved in {dir}{details} | {artist} – {title}: {n} manually downloaded tracks tagged and shelved in {dir}{details}",
    "import.existing_skipped": "; {n} file without a track left in place | ; {n} files without a track left in place",
    "import.error": "“{title}”: error during import ({error})",
    "import.incomplete": "{label}: {reason}. Nothing was shelved; you can complete the missing tracks from another peer.",
    "import.needs_review": "{label}: {reason}. Nothing was moved, check the import plan.",
    "import.done": "{artist} – {title} imported: {n} files shelved in {dir}{details}",
    "import.detail.tags": " (MusicBrainz tags written)",
    "import.detail.tags_cover": " (MusicBrainz tags written, cover embedded)",
    "import.detail.sources": "; sources: {sources}",
    "import.detail.replaced": "; {n} old file replaced | ; {n} old files replaced",
    "import.detail.dropped": "; {n} unused file removed from downloads | ; {n} unused files removed from downloads",
    "import.detail.by_you": "; confirmed by you",
    "search.manual": "Manual search “{query}”",
    "transfer.cancelled": "Transfer cancelled by you: {path} ({peer})",
    "transfer.cancelled_many": "{n} transfer cancelled by you | {n} transfers cancelled by you",
    "transfer.pause_many": "{n} transfer paused by you | {n} transfers paused by you",
    "transfer.resume_many": "{n} transfer resumed by you | {n} transfers resumed by you",
    "transfer.retry_many": "Retry requested by you for {n} transfer | Retry requested by you for {n} transfers",
    "transfer.paused": "Paused by you: “{name}” ({peer})",
    "transfer.resumed": "Resumed by you: “{name}” ({peer})",
    "transfer.retried": "Retry requested by you: “{name}” from {peer}",
    "browse": "Browsed {peer}’s shares: {n} folders",
    "share.added": "Shared folder added: {path}",
    "share.updated": "Rules of share {path} changed: {state}, extensions {extensions}, exclusions {excludes}",
    "share.removed": "Folder removed from shares: {path}",
    "shares.applied": "Shares updated: {n} folder exposed to the network | Shares updated: {n} folders exposed to the network",
    "shares.applied_files": "Shares updated: {n} folder exposed to the network, {files} files in the configured folders | Shares updated: {n} folders exposed to the network, {files} files in the configured folders",
    "settings.updated": "Settings changed: {sections}",
    "settings.unchanged": "Settings saved: no change",
    "settings.themes": "Custom themes: {changes}",
    "profile.created": "Quality profile “{name}” created: {formats}",
    "profile.updated": "Quality profile “{name}” changed: {formats}",
    "auth.setup": "Admin account “{name}” created",
    "auth.failed": "Sign-in refused for “{name}”",
    "navidrome.scan": "Navidrome scan triggered",
    "profile.picture_updated": "Profile picture changed",
    "profile.picture_removed": "Profile picture removed",
    "navidrome.scan_failed": "Navidrome scan impossible: {error}",
    "upload.requested": "{peer} requests “{name}”",
    "upload.started": "Sending “{name}” to {peer}",
    "upload.complete": "“{name}” sent to {peer}",
    "upload.failed": "Sending “{name}” to {peer} interrupted ({error})",
    "download.file_complete": "Received “{name}” from {peer} → {path}",
    "download.file_failed": "“{name}” failed at {peer}: {error}",
    "download.manual": "Manual download of {n} file from {peer} to {dest} | Manual download of {n} files from {peer} to {dest}",
    "slsk.connected": "Connected to the Soulseek network as {user}",
    "slsk.connect_retry": "{error} — retrying in {delay} s",
    "chat.joined": "Joined room “{room}”",
    "chat.left": "Left room “{room}”",
    "error.nothing_received": "Nothing received for this album yet",
    "error.empty_message": "Empty message",
    "error.rooms_timeout": "The server didn’t send the room list in time",
    "error.room_join_timeout": "Couldn’t join “{room}” (no answer from the server)",
    "network.port_forwarded": "VPN forwarded port is now {port} (was {previous}): Soulseek reconnects on it",
    "network.port_waiting": "Waiting for the VPN forwarded port (file {path} is empty or missing), Soulseek stays offline until then: is port forwarding on in gluetun?",
    "slsk.disconnected": "Disconnected from the Soulseek server (reconnecting automatically)",

    # --- engine state / errors shown in the UI
    "engine.not_configured": "Soulseek credentials not configured",
    "engine.auth_refused": "Authentication refused by the server: {reason}",
    "engine.invalid_credentials": "invalid credentials",
    "engine.connect_failed": "Connection failed: {error}",
    "engine.offline": "Not connected to the Soulseek network",
    "transfer.restart_interrupted": "Interrupted by a restart",
    "transfer.source_removed": "Download source removed from Soulsick",
    "transfer.source_failed": "Source dropped: the album is looked for again",

    # --- share rules
    "share.hidden_file": "hidden file",
    "share.excluded_pattern": "excluded by pattern “{pattern}”",
    "share.blocked_ext": "extension .{ext} is never shared (sensitive file)",
    "share.ext_not_allowed": "extension .{ext} not allowed",
    "share.root_forbidden": "The system root can’t be shared",
    "share.protected": "This folder contains or overlaps {path} (Soulsick configuration/downloads), it can’t be shared",

    # --- API errors
    "error.not_found": "Not found",
    "error.auth_required": "Sign-in required",
    "error.too_many_attempts": "Too many attempts, try again in a few minutes",
    "error.admin_exists": "The admin account already exists",
    "error.invalid_credentials": "Invalid credentials",
    "error.invalid_edition": "Invalid edition",
    "error.candidate_busy": "This candidate is already in progress or used",
    "error.search_expired": "Search expired",
    "error.peer_no_answer": "{peer} didn’t answer (offline or unreachable)",
    "error.peer_offline": "{peer} is offline",
    "error.peer_unknown": "No Soulseek user named {peer}",
    "error.peer_unreachable": "Couldn’t connect to {peer}: neither side could reach the other. Their listening port (or yours) is probably not open; ask them to check theirs, or try again later.",
    "error.folder_missing": "Folder not found in the container: {path}",
    "error.already_shared": "This folder is already shared",
    "error.access_denied": "Access denied",
    "error.engine_offline": "Not connected to the Soulseek network",
    "error.profile_in_use": "Profile used by {n} artist | Profile used by {n} artists",
    "error.theme_too_many_keys": "{path}: too many keys",
    "error.theme_bad_key": "{path}: invalid key",
    "error.theme_bad_value": "{path}: value refused",
    "error.theme_bad_type": "{path}: type refused",
    "error.theme_limit": "50 themes maximum",
    "error.theme_bad_id": "Invalid or duplicate theme id: {id}",
    "error.theme_no_name": "Theme {id}: name missing",
    "error.artist_not_found": "Artist not found",
    "error.album_not_found": "Album not found",
    "error.candidate_not_found": "Candidate not found",
    "error.transfer_not_found": "Transfer not found",
    "error.share_not_found": "Share not found",
    "error.profile_not_found": "Profile not found",
    "error.search_not_found": "Search not found or expired",
    "error.track_already_received": "This track is already received",
    "error.no_tracks_for_peer": "This peer has none of the requested tracks",
    "error.source_not_active": "This source is not downloading",
    "album.redownload": "{label}: downloading again from {source} ({n} source dropped) — the shelved files are replaced once it's imported | {label}: downloading again from {source} ({n} sources dropped) — the shelved files are replaced once it's imported",
    "reason.redownload": "Replaced: downloading again from {source}",
    "error.picture_invalid": "Not an image (JPEG, PNG, GIF or WebP expected)",
    "error.picture_too_big": "Image too big (1 MB at most)",
    "error.destination_taken": "Destination already taken: {path}",
    "error.file_outside_downloads": "File outside the download folders: {path}",
    "error.track_mapped_twice": "A track is mapped to several files",
    "error.folder_outside_library": "Folder outside the library",
    "error.no_unrecognised_files": "No unrecognised file in this folder",
    "error.unknown_match": "Unknown match: run the identification again",
    "error.edition_not_found": "Edition not found on MusicBrainz",
    "error.destination_refused": "Destination path refused: {path}",
    "error.navidrome_not_configured": "Navidrome URL not configured",
    "error.library_unavailable": "Library unavailable: {problem}",
    "error.library_not_empty_case": "This only applies when the library folder is empty",
    "error.musicbrainz_status": "MusicBrainz answered {status}",
}


@dataclass(frozen=True)
class Msg:
    """A translatable sentence: key + params. `str(msg)` is the English text."""

    key: str
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return render(self.key, self.params)

    def to_json(self) -> dict:
        return {"key": self.key, "params": {k: _json(v) for k, v in self.params.items()}}

    def __str__(self) -> str:
        return self.text


def M(key: str, **params: Any) -> Msg:
    return Msg(key, params)


def _json(v: Any) -> Any:
    if isinstance(v, Msg):
        return v.to_json()
    if isinstance(v, (list, tuple)):
        return [_json(x) for x in v]
    return v


def _value(v: Any) -> Any:
    if isinstance(v, Msg):
        return v.text
    if isinstance(v, list):
        # A list of sentence fragments is concatenated
        return "".join(str(_value(x)) for x in v)
    if isinstance(v, dict) and "key" in v:
        return render(v["key"], v.get("params") or {})
    return v


class _Safe(dict):
    def __missing__(self, k: str) -> str:
        return "{" + k + "}"


def render(key: str, params: dict[str, Any] | None = None) -> str:
    params = params or {}
    tpl = EN.get(key, key)
    if " | " in tpl:
        forms = tpl.split(" | ")
        tpl = forms[0] if params.get("n") == 1 else forms[-1]
    return tpl.format_map(_Safe({k: _value(v) for k, v in params.items()}))


def set_reason(obj: Any, attr: str, msg: Msg | None) -> None:
    """Stores a reason on a row: English text in `attr`, translatable form in `attr_t`."""
    setattr(obj, attr, msg.text if msg else None)
    setattr(obj, f"{attr}_t", msg.to_json() if msg else None)


class UserError(ValueError):
    """An error meant for the person using the app (turned into a translatable API error)."""

    def __init__(self, key: str, **params: Any) -> None:
        self.msg = Msg(key, params)
        super().__init__(self.msg.text)
