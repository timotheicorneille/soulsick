import type { AlbumStatus, Transfer, TransferStateName } from './types'
import { t, te } from '@/i18n'
import { ago } from './format'

/** The chain: the four stages an album goes through, from wanted to shelved. */
export const CHAIN_STAGES = ['wanted', 'found', 'received', 'shelved'] as const

export type StageTone = 'off' | 'lit' | 'active' | 'attention' | 'done' | 'error'
export type BadgeTone = 'signal' | 'success' | 'warning' | 'danger' | 'quiet'

export interface AlbumStatusInfo {
  label: string
  badge: BadgeTone
  /** Stage reached (-1 = outside the chain) */
  stage: number
  tone: StageTone
  hint: string
}

const ALBUM_META: Record<AlbumStatus, { badge: BadgeTone, stage: number, tone: StageTone }> = {
  ignored: { badge: 'quiet', stage: -1, tone: 'off' },
  wanted: { badge: 'quiet', stage: 0, tone: 'lit' },
  searching: { badge: 'signal', stage: 0, tone: 'active' },
  pending_approval: { badge: 'warning', stage: 1, tone: 'attention' },
  downloading: { badge: 'signal', stage: 2, tone: 'active' },
  importing: { badge: 'signal', stage: 3, tone: 'active' },
  needs_review: { badge: 'warning', stage: 3, tone: 'attention' },
  imported: { badge: 'success', stage: 3, tone: 'done' },
  failed: { badge: 'danger', stage: 0, tone: 'error' },
}

export function albumStatus(s: AlbumStatus): AlbumStatusInfo {
  return { ...ALBUM_META[s], label: t(`albumStatus.${s}.label`), hint: t(`albumStatus.${s}.hint`) }
}

const TRANSFER_BADGE: Record<TransferStateName, BadgeTone> = {
  queued: 'quiet',
  initializing: 'signal',
  transferring: 'signal',
  paused: 'quiet',
  complete: 'success',
  failed: 'danger',
  aborted: 'quiet',
}

export function transferState(s: TransferStateName): { label: string, badge: BadgeTone } {
  return { label: t(`transferState.${s}`), badge: TRANSFER_BADGE[s] }
}

/** What the wait of a download depends on: a transfer row, merged with its live progress */
export type Waiting = Pick<Transfer, 'direction' | 'state' | 'username'>
  & Partial<Pick<Transfer, 'remotely_queued' | 'place_in_queue' | 'attempts' | 'stuck' | 'stuck_reason' | 'stuck_since' | 'candidate_id'>>

/** What a download is waiting for, when its state alone says too little */
export type DownloadWait =
  | { kind: 'stuck' }
  | { kind: 'requested' }
  | { kind: 'inTheirQueue', place: number | null }
  | { kind: 'connecting', seconds: number | null }

/** Album download that stopped moving (see the stuck_* hunter settings) */
export function isStuck(tr: Waiting): boolean {
  return !!tr.stuck && tr.direction === 'download' && ['queued', 'initializing', 'transferring'].includes(tr.state)
}

/**
 * "Queued" alone means nothing: did the peer answer, where are we in their queue, is their file
 * connection on its way? Stuck comes first, it is what needs you. `connecting` is how long the
 * download has been connecting, when the caller keeps that clock.
 */
export function downloadWait(tr: Waiting, connecting: number | null = null): DownloadWait | null {
  if (tr.direction !== 'download')
    return null
  if (isStuck(tr))
    return { kind: 'stuck' }
  if (tr.state === 'queued')
    return tr.remotely_queued ? { kind: 'inTheirQueue', place: tr.place_in_queue ?? null } : { kind: 'requested' }
  if (tr.state === 'initializing')
    return { kind: 'connecting', seconds: connecting }
  return null
}

/** Badge of a transfer: what it waits for, else its state */
export function transferStatus(tr: Waiting, connecting: number | null = null): { label: string, badge: BadgeTone } {
  const w = downloadWait(tr, connecting)
  if (w?.kind === 'stuck')
    return { label: t('transfer.stuck'), badge: 'warning' }
  if (w?.kind === 'requested')
    return { label: t('transfer.requested'), badge: 'quiet' }
  if (w?.kind === 'inTheirQueue')
    return { label: w.place ? t('transfer.inTheirQueueAt', { n: w.place }) : t('transfer.inTheirQueue'), badge: 'signal' }
  if (w?.kind === 'connecting' && w.seconds !== null && w.seconds >= 3)
    return { label: t('transfer.connectingFor', { s: w.seconds }), badge: w.seconds >= 30 ? 'warning' : 'signal' }
  return transferState(tr.state)
}

/** Why a download waits, in a sentence (tooltip); null when it doesn't.
 * Changing peer is only offered for an album source: a manual download has no other candidate. */
export function downloadWaitHint(tr: Waiting, w: DownloadWait | null): string | null {
  if (w?.kind === 'stuck')
    return `${t(`transfer.stuckWhy.${tr.stuck_reason}`, { since: ago(tr.stuck_since), n: tr.attempts ?? 1, place: tr.place_in_queue ?? '?' })}${tr.candidate_id ? ` ${t('transfer.stuckAction')}` : ''}`
  if (w?.kind === 'requested')
    return t('transfer.requestedHint', { peer: tr.username })
  if (w?.kind === 'inTheirQueue')
    return t('transfer.acceptedHint', { peer: tr.username })
  if (w?.kind === 'connecting')
    return t('transfer.connectingHint', { peer: tr.username })
  return null
}

export const FORMAT_LABELS: Record<string, string> = {
  'flac-24': 'FLAC 24',
  'flac': 'FLAC',
  'alac': 'ALAC',
  'wav': 'WAV',
  'mp3-320': 'MP3 320',
  'mp3-v0': 'MP3 V0',
  'aac-256': 'AAC 256',
  'mp3-256': 'MP3 256',
  'opus': 'Opus',
  'ogg': 'Vorbis',
  'mp3-192': 'MP3 192',
  'aac': 'AAC',
  'mp3-low': 'MP3 <192',
}

export const LOSSLESS = new Set(['flac-24', 'flac', 'alac', 'wav'])

export const RELEASE_TYPES = ['album', 'ep', 'single', 'broadcast', 'other'] as const
export const SECONDARY_TYPES = ['compilation', 'live', 'remix', 'soundtrack', 'demo', 'mixtape/street', 'dj-mix', 'interview', 'spokenword'] as const

/** Label of a MusicBrainz primary or secondary release type. */
export function releaseTypeLabel(key: string): string {
  const k = `releaseType.${key}`
  return te(k) ? t(k) : key
}

/** Journal event families, for filters and labels. */
export const EVENT_FAMILIES = ['hunt', 'search', 'download', 'import', 'upload', 'library', 'artist', 'album', 'musicbrainz', 'share', 'transfer', 'chat', 'slsk', 'settings', 'auth'] as const

export function eventFamilyLabel(family: string): string {
  const k = `eventFamily.${family === 'shares' ? 'share' : family}`
  return te(k) ? t(k) : family
}
