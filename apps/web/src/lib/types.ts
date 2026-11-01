import type { ServerMsg } from './serverText'

export type AlbumStatus =
  | 'ignored'
  | 'wanted'
  | 'searching'
  | 'pending_approval'
  | 'downloading'
  | 'importing'
  | 'needs_review'
  | 'imported'
  | 'failed'

export type ArtistMode = 'auto' | 'validation'

export interface EngineStatus {
  configured: boolean
  connected: boolean
  connecting: boolean
  username: string | null
  error: string | null
  error_t?: ServerMsg | null
  listen_port?: number | null
  /** Port opened by the VPN (gluetun), when Soulsick runs behind it */
  forwarded_port?: number | null
  download_speed: number
  upload_speed: number
  free_upload_slots: number
  upload_slots: number
  shared_folders: number
  shared_files: number
}

export interface HunterState {
  current: { release_group_id: number, label: string, step: string, query?: string } | null
  last_run_at: string | null
  next_run_at: string | null
}

export interface AppStatus {
  engine: EngineStatus
  hunter: HunterState
  library: { files: number, size: number, scanning: boolean }
  artists: number
  albums: Partial<Record<AlbumStatus, number>>
  attention: number
  active_transfers: { download: number, upload: number }
  stuck_transfers: number
  chat_unread: number
  storage: StorageLevel
  library_guard: LibraryGuard
  paths: { music: string, downloads: string, config: string }
}

export interface QualityProfile {
  id: number
  name: string
  formats: string[]
  upgrade_until: string | null
  max_missing_tracks: number
}

export interface FormatDef { key: string, label: string }

export interface ArtistStats {
  total: number
  monitored: number
  imported: number
  wanted: number
  attention: number
  cover_mbid?: string
}

export interface Artist {
  id: number
  mbid: string
  name: string
  sort_name: string
  disambiguation: string
  country: string | null
  type: string | null
  monitored: boolean
  mode: ArtistMode
  quality_profile_id: number | null
  primary_types: string[]
  secondary_types: string[]
  added_at: string
  last_synced_at: string | null
  stats: ArtistStats
}

export interface ReleaseGroup {
  id: number
  mbid: string
  artist_id: number
  title: string
  primary_type: string | null
  secondary_types: string[]
  first_release_date: string | null
  monitored: boolean
  status: AlbumStatus
  status_reason: string | null
  status_reason_t?: ServerMsg | null
  /** Tracks of this album in the library / tracks of its edition (partial album when lower) */
  present_tracks?: number | null
  total_tracks?: number | null
  selected_release_id: number | null
  imported_format: string | null
  imported_path: string | null
  last_searched_at: string | null
  updated_at: string
  artist?: { id: number, name: string, mbid: string, mode: ArtistMode }
}

export interface ArtistDetail extends Artist {
  release_groups: ReleaseGroup[]
}

export interface Track {
  id: number
  mbid: string
  recording_mbid: string
  disc: number
  position: number
  number: string
  title: string
  artist_credit: string
  length_ms: number | null
}

export interface Release {
  id: number
  mbid: string
  title: string
  date: string | null
  country: string | null
  status: string | null
  disambiguation: string
  formats: string
  track_count: number
  disc_count: number
  label: string | null
  barcode: string | null
  tracks: Track[]
}

export interface RemoteFile {
  filename: string
  size: number
  bitrate: number | null
  duration: number | null
  sample_rate: number | null
  bit_depth: number | null
  vbr: boolean
  format: string | null
  extra?: boolean
  name?: string
}

export interface ScoreDetail {
  format?: { points: number, max: number, format: string | null, label: string, rank: number | null, mixed: boolean }
  match?: {
    points: number
    max: number
    release: string
    release_id: number
    tracks_expected: number
    tracks_found: number
    tracks_matched: number
    title_score: number
    duration_score: number | null
    confidence: number
    missing: string[]
  }
  peer?: { points: number, max: number, free_slot: boolean, queue_size: number, avg_speed: number }
}

export interface PlanItem {
  file: string
  name: string
  format: string | null
  duration: number | null
  track_id: number | null
  track_title: string | null
  disc: number | null
  position: number | null
  score: number | null
}

export interface Candidate {
  id: number
  release_group_id: number
  search_run_id: number | null
  username: string
  directory: string
  files: RemoteFile[]
  format: string | null
  score: number
  score_detail: ScoreDetail
  status: 'proposed' | 'rejected' | 'chosen' | 'downloaded' | 'failed' | 'imported'
  reject_reason: string | null
  reject_reason_t?: ServerMsg | null
  matched_release_id: number | null
  has_free_slots: boolean
  queue_size: number
  avg_speed: number
  download_dir: string | null
  import_plan: PlanItem[] | null
  created_at: string
}

export interface SearchRun {
  id: number
  query: string
  kind: string
  started_at: string
  result_count: number
  peer_count: number
}

export interface LibraryFile {
  id: number
  path: string
  size: number
  format: string | null
  bitrate: number | null
  duration: number | null
  tag_artist: string | null
  tag_album: string | null
  tag_title: string | null
  release_group_mbid: string | null
  release_mbid: string | null
  recording_mbid: string | null
  lyrics: FileLyrics | null
}

/** The pass looking for the library's missing lyrics on LRCLIB */
export interface LyricsJob { running: boolean, done: number, total: number, found: number }

/** Lyrics Navidrome plays for a file: the file next to it first, else those embedded */
export interface FileLyrics {
  kind: 'synced' | 'plain'
  source: 'file' | 'embedded'
  ext: string | null
}

export type TransferStateName = 'queued' | 'initializing' | 'transferring' | 'paused' | 'complete' | 'failed' | 'aborted'

export interface Transfer {
  id: number
  direction: 'download' | 'upload'
  username: string
  remote_path: string
  local_path: string | null
  size: number | null
  bytes_transferred: number
  speed: number
  state: TransferStateName
  place_in_queue: number | null
  remotely_queued?: boolean
  error: string | null
  attempts: number
  candidate_id: number | null
  release_group_id: number | null
  created_at: string | null
  started_at: string | null
  completed_at: string | null
  /** Album download that stopped moving (see the stuck_* hunter settings) */
  stuck?: boolean
  stuck_reason?: StuckReason | null
  stuck_since?: string | null
}

export type StuckReason = 'no_progress' | 'queue_frozen' | 'retrying'

/** Another peer that has tracks a source has not delivered yet */
export interface PeerAlternative {
  candidate_id: number
  username: string
  format: string | null
  score: number
  status: string
  has_free_slots: boolean
  queue_size: number
  avg_speed: number
  tracks: number
  track_ids: number[]
}

export interface PeerAlternatives {
  release_group_id: number | null
  missing: number
  alternatives: PeerAlternative[]
}

export interface JournalEvent {
  id: number
  ts: string
  kind: string
  level: 'info' | 'warning' | 'error'
  message: string
  artist_id: number | null
  release_group_id: number | null
  transfer_id: number | null
  candidate_id: number | null
  data: Record<string, unknown>
}

export interface AlbumDetail extends ReleaseGroup {
  releases: Release[]
  candidates: Candidate[]
  search_runs: SearchRun[]
  files: LibraryFile[]
  transfers: Transfer[]
  events: JournalEvent[]
}

export interface MBArtist {
  mbid: string
  name: string
  sort_name: string
  disambiguation: string
  country: string | null
  type: string | null
  score: number
  begin: string | null
  tags: string[]
  in_library: boolean
}

export interface SearchFolder {
  username: string
  directory: string
  files: RemoteFile[]
  formats: string[]
  audio_count: number
  size: number
  has_free_slots: boolean
  avg_speed: number
  queue_size: number
}

export interface SearchSnapshot {
  ticket: number
  query: string
  elapsed: number
  peer_count: number
  file_count: number
  folders: SearchFolder[]
  responses?: SearchResponse[]
}

export interface Share {
  id: number
  path: string
  enabled: boolean
  allowed_extensions: string[]
  exclude_patterns: string[]
  file_count: number
  total_size: number
  excluded_count: number
  last_scan_at: string | null
  exists: boolean
}

export interface SharePreview {
  exists: boolean
  included: number
  included_size?: number
  excluded: number
  excluded_samples: { path: string, reason: string, reason_t?: ServerMsg }[]
  extensions: Record<string, number>
  effective_extensions?: string[]
}

export interface BrowseDirectory { path: string, files: (RemoteFile & { name: string })[] }

export interface AppSettings {
  soulseek: {
    username: string
    password: string
    listen_port: number
    upnp: boolean
    upload_slots: number
    upload_speed_kbps: number
    download_speed_kbps: number
    share_library: boolean
    description: string
  }
  hunter: {
    enabled: boolean
    interval_minutes: number
    max_searches_per_run: number
    search_wait_seconds: number
    proposals: number
    min_match: number
    download_covers: boolean
    stuck_no_progress_minutes: number
    stuck_queue_minutes: number
    stuck_after_attempts: number
  }
  library: {
    naming_template: string
    sync_interval_hours: number
    write_tags: boolean
    embed_cover: boolean
    fetch_lyrics: boolean
  }
  navidrome: { enabled: boolean, url: string, username: string, password: string }
}

export interface CoverageSource {
  candidate_id: number
  username: string
  transfer_id: number
  state: TransferStateName
  format: string | null
  filename: string
  bytes: number
  size: number | null
}

export interface CoverageTrack {
  track_id: number
  disc: number
  position: number
  title: string
  length_ms: number | null
  state: 'received' | 'downloading' | 'paused' | 'missing' | 'in_library'
  sources: CoverageSource[]
  /** Format of the shelved file; `upgrade` when it is below the rest of the album */
  library_format?: string | null
  upgrade?: boolean
  /** Other peers having this track, to take it from them */
  holders?: CoverageHolder[]
}

export interface CoverageHolder {
  candidate_id: number
  username: string
  status: string
  format: string | null
  filename: string
  has_free_slots: boolean
  queue_size: number
  avg_speed: number
}

export interface CoverageFiller {
  candidate_id: number
  username: string
  directory: string
  format: string | null
  score: number
  status: string
  has_free_slots: boolean
  queue_size: number
  avg_speed: number
  tracks: { track_id: number, title: string, filename: string, format: string | null, from_format?: string | null }[]
}

export interface Coverage {
  release_id: number | null
  release_title?: string
  tracks: CoverageTrack[]
  summary: { received: number, downloading: number, paused?: number, missing: number, in_library: number, total: number, upgrade?: number }
  received_formats?: string[]
  fillers: CoverageFiller[]
  /** Peers having better versions of the tracks to upgrade */
  upgraders?: CoverageFiller[]
}

export interface LogLine {
  id: number
  ts: string
  level: 'debug' | 'info' | 'warning' | 'error' | 'critical'
  source: string
  logger: string
  message: string
}

export interface SearchResponse {
  at: number
  username: string
  files: number
  folders: number
  locked: number
  free_slot: boolean
  speed: number
  queue: number
}

export interface Activity {
  hunter: HunterState
  searches: { ticket: number, query: string, age: number, responses: number, files: number }[]
  library_scanning: boolean
  retrying: number
  engine: EngineStatus
}

export interface ExistingProposal {
  release_group_mbid: string
  release_mbid: string
  title: string
  artist_name: string
  artist_mbid: string
  date: string | null
  release_title: string
  release_info: string
  track_count: number
  confidence: number
  matched: number
  mapping: { file: string, name: string, track_mbid: string | null, track: string | null }[]
  unmatched_files: string[]
  missing_tracks: string[]
  destination: string | null
  safe: boolean
  partial?: boolean
  file_confidence?: number
}

export interface ExistingGroup {
  dir: string
  files: number
  size: number
  format: string | null
  guess: { artist: string, album: string }
  result: { proposals: ExistingProposal[], query?: { artist: string, album: string }, error?: string } | null
}

export type ChatKind = 'private' | 'room'

export interface ChatMessage {
  id: number
  ts: string
  kind: ChatKind
  target: string
  username: string
  text: string
  outgoing: boolean
  read: boolean
}

export interface ChatConversation {
  kind: ChatKind
  target: string
  last: ChatMessage | null
  unread: number
}

export interface ChatOverview {
  conversations: ChatConversation[]
  rooms: string[]
  unread_private: number
  me: string | null
}

export interface ChatRoomInfo { name: string, users: number, private: boolean }

export type StorageLevel = 'ok' | 'low' | 'critical'

/** Library folder check: imports wait while it is missing, not mounted or suddenly empty. */
export interface LibraryGuard {
  ok: boolean
  path: string
  problem: string | null
  problem_t: ServerMsg | null
  warnings: string[]
  warnings_t: ServerMsg[]
}

export interface StorageFolder { key: 'music' | 'downloads' | 'config' | 'share', path: string, size: number, missing?: boolean }

export interface StorageDisk {
  id: number
  mount: string | null
  total: number
  used: number
  free: number
  level: StorageLevel
  soulsick: number
  folders: StorageFolder[]
}

/** Album only partly in the library (e.g. a single track shelved by hand) */
export function isPartial(a: { present_tracks?: number | null, total_tracks?: number | null }): boolean {
  return !!a.total_tracks && (a.present_tracks ?? 0) < a.total_tracks
}

/** What we exchanged with a peer (GET /peers/:username). */
export interface PeerTotals { files: number, complete: number, failed: number, bytes: number, first: string | null, last: string | null }
export interface PeerAlbum {
  id: number
  mbid: string
  title: string
  artist: string
  artist_id: number
  year: string | null
  candidate_status: 'chosen' | 'downloaded' | 'failed' | 'imported'
  album_status: AlbumStatus
  format: string | null
  date: string | null
}
export interface PeerHistory {
  username: string
  downloads: PeerTotals | null
  uploads: PeerTotals | null
  measured_speed: number | null
  albums: PeerAlbum[]
  proposals: number
  messages: { count: number, last: string | null }
  recent: Transfer[]
}

/** What the network says about a peer (GET /peers/:username/profile); every part may be missing. */
export interface PeerProfile {
  username: string
  status: 'online' | 'away' | 'offline' | null
  privileged: boolean | null
  reachable: boolean
  country: string | null
  avg_speed?: number
  uploads?: number
  shared_files?: number
  shared_folders?: number
  description?: string | null
  picture?: string | null
  has_free_slots?: boolean
  upload_slots?: number
  queue_size?: number
  upload_permissions?: string
  interests?: string[]
  hated_interests?: string[]
}

// Discover: artists similar to the followed ones (ListenBrainz)
export interface Recommendation {
  mbid: string
  name: string
  comment: string
  type: string | null
  /** 1–100, relative to the best recommendation */
  score: number
  /** Followed artists it is similar to, the closest first */
  because: { id: number, name: string }[]
}

export interface DiscoverResponse {
  recommendations: Recommendation[]
  /** Followed artists whose similar artists were not fetched yet */
  pending: number
  followed: number
}

export interface SimilarArtist {
  mbid: string
  name: string
  comment: string
  type: string | null
  /** Set when it is already followed */
  followed_id: number | null
}
