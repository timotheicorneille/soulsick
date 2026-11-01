from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(d: datetime | None) -> str | None:
    """ISO 8601 avec fuseau : SQLite renvoie des dates naïves, stockées en UTC."""
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.isoformat()


class AlbumStatus(StrEnum):
    IGNORED = "ignored"
    WANTED = "wanted"
    SEARCHING = "searching"
    PENDING_APPROVAL = "pending_approval"
    DOWNLOADING = "downloading"
    IMPORTING = "importing"
    NEEDS_REVIEW = "needs_review"
    IMPORTED = "imported"
    FAILED = "failed"


class ArtistMode(StrEnum):
    AUTO = "auto"
    VALIDATION = "validation"


class CandidateStatus(StrEnum):
    PROPOSED = "proposed"
    REJECTED = "rejected"
    CHOSEN = "chosen"
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    IMPORTED = "imported"


class TransferState(StrEnum):
    QUEUED = "queued"
    INITIALIZING = "initializing"
    TRANSFERRING = "transferring"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    ABORTED = "aborted"


class Setting(Base):
    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class QualityProfile(Base):
    __tablename__ = "quality_profile"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    # Formats acceptés, du préféré au moins bon. Tout format absent est refusé.
    formats: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Tant que le format importé est moins bon que celui-ci, on continue à chercher mieux.
    upgrade_until: Mapped[str | None] = mapped_column(String, nullable=True)
    max_missing_tracks: Mapped[int] = mapped_column(Integer, default=0)


class Artist(Base):
    __tablename__ = "artist"
    id: Mapped[int] = mapped_column(primary_key=True)
    mbid: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    sort_name: Mapped[str] = mapped_column(String, default="")
    disambiguation: Mapped[str] = mapped_column(String, default="")
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str | None] = mapped_column(String, nullable=True)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String, default=ArtistMode.VALIDATION)
    quality_profile_id: Mapped[int | None] = mapped_column(ForeignKey("quality_profile.id"), nullable=True)
    # Types primaires suivis (album, ep, single) et secondaires acceptés (live, compilation…)
    primary_types: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["album", "ep"])
    secondary_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last time its similar artists were asked to ListenBrainz (Discover)
    similar_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    quality_profile: Mapped[QualityProfile | None] = relationship(lazy="selectin")
    release_groups: Mapped[list["ReleaseGroup"]] = relationship(
        back_populates="artist", cascade="all, delete-orphan", lazy="raise", passive_deletes=True
    )


class SimilarArtist(Base):
    """An artist similar to a followed one, as ListenBrainz sees it."""

    __tablename__ = "similar_artist"
    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artist.id", ondelete="CASCADE"), index=True)
    mbid: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    comment: Mapped[str] = mapped_column(String, default="")
    type: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[int] = mapped_column(Integer)


class DismissedArtist(Base):
    """A recommendation you are not interested in: never proposed again."""

    __tablename__ = "dismissed_artist"
    mbid: Mapped[str] = mapped_column(String, primary_key=True)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReleaseGroup(Base):
    __tablename__ = "release_group"
    id: Mapped[int] = mapped_column(primary_key=True)
    mbid: Mapped[str] = mapped_column(String, unique=True, index=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artist.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String)
    primary_type: Mapped[str | None] = mapped_column(String, nullable=True)
    secondary_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    first_release_date: Mapped[str | None] = mapped_column(String, nullable=True)
    monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default=AlbumStatus.IGNORED, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_reason_t: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Tracks of this album found in the library / tracks of its edition (None = unknown)
    present_tracks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tracks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_release_id: Mapped[int | None] = mapped_column(
        ForeignKey("release.id", use_alter=True, ondelete="SET NULL"), nullable=True
    )
    releases_fetched: Mapped[bool] = mapped_column(Boolean, default=False)
    imported_format: Mapped[str | None] = mapped_column(String, nullable=True)
    imported_path: Mapped[str | None] = mapped_column(String, nullable=True)
    last_searched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    artist: Mapped[Artist] = relationship(back_populates="release_groups", lazy="selectin")
    releases: Mapped[list["Release"]] = relationship(
        back_populates="release_group",
        cascade="all, delete-orphan",
        foreign_keys="Release.release_group_id",
        lazy="raise",
        passive_deletes=True,
    )


class ArtistReleaseGroup(Base):
    """Une sortie peut appartenir à plusieurs artistes (featuring, split, collaboration).

    `ReleaseGroup.artist_id` reste l'artiste « principal » (nom utilisé pour chercher et ranger) ;
    cette table dit dans quelles discographies la sortie apparaît.
    """

    __tablename__ = "artist_release_group"
    artist_id: Mapped[int] = mapped_column(ForeignKey("artist.id", ondelete="CASCADE"), primary_key=True)
    release_group_id: Mapped[int] = mapped_column(
        ForeignKey("release_group.id", ondelete="CASCADE"), primary_key=True, index=True
    )


def of_artist(artist_id: int):
    """Filtre : sorties présentes dans la discographie de l'artiste (principal ou associé)."""
    from sqlalchemy import select

    return ReleaseGroup.id.in_(
        select(ArtistReleaseGroup.release_group_id).where(ArtistReleaseGroup.artist_id == artist_id)
    )


class Release(Base):
    __tablename__ = "release"
    id: Mapped[int] = mapped_column(primary_key=True)
    mbid: Mapped[str] = mapped_column(String, unique=True, index=True)
    release_group_id: Mapped[int] = mapped_column(ForeignKey("release_group.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String)
    date: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    disambiguation: Mapped[str] = mapped_column(String, default="")
    formats: Mapped[str] = mapped_column(String, default="")
    track_count: Mapped[int] = mapped_column(Integer, default=0)
    disc_count: Mapped[int] = mapped_column(Integer, default=1)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    barcode: Mapped[str | None] = mapped_column(String, nullable=True)

    release_group: Mapped[ReleaseGroup] = relationship(
        back_populates="releases", foreign_keys=[release_group_id], lazy="raise"
    )
    tracks: Mapped[list["Track"]] = relationship(
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="(Track.disc, Track.position)",
        lazy="selectin",
    )


class Track(Base):
    __tablename__ = "track"
    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("release.id", ondelete="CASCADE"), index=True)
    mbid: Mapped[str] = mapped_column(String, index=True)
    recording_mbid: Mapped[str] = mapped_column(String, index=True)
    disc: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer)
    number: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String)
    artist_credit: Mapped[str] = mapped_column(String, default="")
    length_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    release: Mapped[Release] = relationship(back_populates="tracks", lazy="raise")


class LibraryFile(Base):
    __tablename__ = "library_file"
    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    mtime: Mapped[float] = mapped_column(Float, default=0)
    format: Mapped[str | None] = mapped_column(String, nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    tag_artist: Mapped[str | None] = mapped_column(String, nullable=True)
    tag_album: Mapped[str | None] = mapped_column(String, nullable=True)
    tag_title: Mapped[str | None] = mapped_column(String, nullable=True)
    release_group_mbid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    release_mbid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    recording_mbid: Mapped[str | None] = mapped_column(String, nullable=True)
    # Paroles : fichier à côté (son extension : lrc, elrc, txt), et intégrées (« synced », « plain »,
    # "" si aucune, NULL si pas encore lu)
    lyrics_file: Mapped[str | None] = mapped_column(String, nullable=True)
    lyrics_embedded: Mapped[str | None] = mapped_column(String, nullable=True)
    # Last search on LRCLIB, and what it said when nothing was written (« not_found », « instrumental »)
    lyrics_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lyrics_missing: Mapped[str | None] = mapped_column(String, nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SearchRun(Base):
    __tablename__ = "search_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    release_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("release_group.id", ondelete="CASCADE"), nullable=True, index=True
    )
    query: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="auto")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    peer_count: Mapped[int] = mapped_column(Integer, default=0)


class Candidate(Base):
    __tablename__ = "candidate"
    id: Mapped[int] = mapped_column(primary_key=True)
    release_group_id: Mapped[int] = mapped_column(ForeignKey("release_group.id", ondelete="CASCADE"), index=True)
    search_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_run.id", ondelete="SET NULL"), nullable=True
    )
    username: Mapped[str] = mapped_column(String)
    directory: Mapped[str] = mapped_column(String)
    files: Mapped[list[dict]] = mapped_column(JSON, default=list)
    format: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    score_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default=CandidateStatus.PROPOSED, index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reject_reason_t: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    matched_release_id: Mapped[int | None] = mapped_column(
        ForeignKey("release.id", ondelete="SET NULL"), nullable=True
    )
    has_free_slots: Mapped[bool] = mapped_column(Boolean, default=False)
    queue_size: Mapped[int] = mapped_column(Integer, default=0)
    avg_speed: Mapped[int] = mapped_column(Integer, default=0)
    download_dir: Mapped[str | None] = mapped_column(String, nullable=True)
    # Plan d'import proposé (fichier → piste), modifiable en revue manuelle
    import_plan: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transfer(Base):
    __tablename__ = "transfer"
    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String, index=True)  # download | upload
    username: Mapped[str] = mapped_column(String)
    remote_path: Mapped[str] = mapped_column(String)
    local_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # Manual download later identified and shelved in the library: where the file went
    shelved_path: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes_transferred: Mapped[int] = mapped_column(Integer, default=0)
    speed: Mapped[float] = mapped_column(Float, default=0)
    state: Mapped[str] = mapped_column(String, default=TransferState.QUEUED, index=True)
    place_in_queue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remotely_queued: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nombre de tentatives (1 = premier essai) ; une relance réutilise toujours la même ligne
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate.id", ondelete="SET NULL"), nullable=True, index=True
    )
    release_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("release_group.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Share(Base):
    __tablename__ = "share"
    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Vide = toutes les extensions audio par défaut
    allowed_extensions: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclude_patterns: Mapped[list[str]] = mapped_column(JSON, default=list)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    total_size: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "event"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    level: Mapped[str] = mapped_column(String, default="info")
    message: Mapped[str] = mapped_column(Text)
    artist_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    release_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    transfer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ChatMessage(Base):
    """A Soulseek chat message: private (target = the other user) or in a room (target = room name)."""

    __tablename__ = "chat_message"
    __table_args__ = (Index("ix_chat_conversation", "kind", "target", "id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String)  # private | room
    target: Mapped[str] = mapped_column(String)
    username: Mapped[str] = mapped_column(String)  # author
    text: Mapped[str] = mapped_column(Text)
    outgoing: Mapped[bool] = mapped_column(Boolean, default=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
