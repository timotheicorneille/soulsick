<script setup lang="ts">
import type { Coverage, CoverageSource, FileLyrics, Release } from '@/lib/types'
import { isPartial } from '@/lib/types'
import { ArrowLeftIcon, ExternalLinkIcon, FolderInputIcon, RadarIcon, XIcon } from '@lucide/vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { toast } from 'vue-sonner'
import CandidateCard from '@/components/CandidateCard.vue'
import SwitchPeerMenu from '@/components/SwitchPeerMenu.vue'
import RedownloadButton from '@/components/RedownloadButton.vue'
import CoverArt from '@/components/CoverArt.vue'
import CoverBackdrop from '@/components/CoverBackdrop.vue'
import DownloadPanel from '@/components/DownloadPanel.vue'
import EventFeed from '@/components/EventFeed.vue'
import FormatTag from '@/components/FormatTag.vue'
import ImportReview from '@/components/ImportReview.vue'
import LyricsTag from '@/components/LyricsTag.vue'
import SignalChain from '@/components/SignalChain.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TrackCoverage from '@/components/TrackCoverage.vue'
import TransferRow from '@/components/TransferRow.vue'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useCoverAccent } from '@/composables/coverAccent'
import { useAlbum } from '@/composables/queries'
import { useTransferActions } from '@/composables/transferActions'
import { api } from '@/lib/api'
import { ago, bytes, ms, year } from '@/lib/format'
import { albumStatus, FORMAT_LABELS, releaseTypeLabel } from '@/lib/status'
import { cn } from '@/lib/utils'
import { useLive } from '@/stores/live'
import { serverText } from '@/lib/serverText'

const { t } = useI18n()
const route = useRoute()
const qc = useQueryClient()
const live = useLive()
const id = computed(() => Number(route.params.id))
const { data: album, isPending } = useAlbum(id)
const { data: coverage } = useQuery({
  queryKey: ['album', id, 'coverage'],
  queryFn: () => api.get<Coverage>(`/albums/${id.value}/coverage`),
})
const accent = useCoverAccent(computed(() => album.value?.mbid))
const tx = useTransferActions()

const tab = ref('')
const viewRelease = ref<string>('')
const showRejected = ref(false)

watch(album, (a) => {
  if (!a)
    return
  if (!viewRelease.value)
    viewRelease.value = String(a.selected_release_id ?? a.releases[0]?.id ?? '')
  // Open where the action is: candidates while hunting, tracks once something arrived
  if (!tab.value)
    tab.value = a.candidates.length && !['imported', 'downloading', 'importing'].includes(a.status) ? 'candidates' : 'tracks'
}, { immediate: true })

const release = computed<Release | undefined>(() =>
  album.value?.releases.find(r => String(r.id) === viewRelease.value),
)
const proposed = computed(() => (album.value?.candidates ?? []).filter(c => c.status !== 'rejected'))
const rejected = computed(() => (album.value?.candidates ?? []).filter(c => c.status === 'rejected'))
const reviewCandidate = computed(() =>
  album.value?.status === 'needs_review'
    ? album.value.candidates.find(c => c.status === 'downloaded')
    : undefined,
)
const present = computed(() => new Set((album.value?.files ?? []).map(f => f.recording_mbid).filter(Boolean)))
/** Lyrics of each shelved track (issue #1: which tracks have their .lrc) */
const lyricsByRecording = computed(() => new Map<string, FileLyrics | null>(
  (album.value?.files ?? []).filter(f => f.recording_mbid).map(f => [f.recording_mbid!, f.lyrics]),
))
const fetchLyrics = useMutation({
  mutationFn: () => api.post<{ asked: number, found: number, error: string | null }>(`/albums/${album.value!.id}/lyrics`),
  onSuccess: (r) => {
    if (r.error)
      toast.warning(t('lyrics.unavailable'), { description: r.error })
    else if (!r.asked)
      toast.info(t('lyrics.nothingToFetch'))
    else
      toast.success(t('lyrics.found', { n: r.found, total: r.asked }, r.found))
    qc.invalidateQueries({ queryKey: ['album'] })
  },
  onError: e => toast.error(t('lyrics.unavailable'), { description: e.message }),
})
const lyricsStats = computed(() => {
  const all = [...lyricsByRecording.value.values()]
  return { total: all.length, with: all.filter(Boolean).length, synced: all.filter(l => l?.kind === 'synced').length }
})
const hasSources = computed(() => !!coverage.value?.tracks.some(x => x.sources.length))
/** Per-track coverage, only meaningful for the edition the hunt targets */
const coverageByTrack = computed(() =>
  coverage.value && String(coverage.value.release_id) === viewRelease.value
    ? new Map(coverage.value.tracks.map(x => [x.track_id, x]))
    : null,
)
/** Audio files received / requested, per candidate */
const received = computed(() => {
  const out: Record<number, { done: number, total: number }> = {}
  for (const tr of album.value?.transfers ?? []) {
    if (!tr.candidate_id || !/\.(flac|mp3|m4a|aac|ogg|opus|wav|aiff?|alac|ape|wv|wma)$/i.test(tr.remote_path))
      continue
    const r = (out[tr.candidate_id] ??= { done: 0, total: 0 })
    r.total++
    if (tr.state === 'complete')
      r.done++
  }
  return out
})
/** Sources with a download that stopped moving */
const stuckSources = computed(() => new Set((album.value?.transfers ?? [])
  .map(tr => ({ ...tr, ...live.transfers.get(tr.id) }))
  .filter(tr => tr.stuck && tr.candidate_id && ['queued', 'initializing', 'transferring'].includes(tr.state!))
  .map(tr => tr.candidate_id!)))
const canApprove = computed(() => !!album.value && !['downloading', 'importing', 'searching'].includes(album.value.status))
const typeLabel = computed(() => {
  const a = album.value
  if (!a)
    return ''
  return [a.primary_type ?? 'other', ...a.secondary_types].map(releaseTypeLabel).filter(Boolean).join(' · ')
})

function progress(src: CoverageSource) {
  const l = live.transfers.get(src.transfer_id)
  const b = l?.bytes_transferred ?? src.bytes
  const size = l?.size ?? src.size
  return size ? Math.round((b / size) * 100) : 0
}

function refresh() {
  qc.invalidateQueries({ queryKey: ['album'] })
}

const hunt = useMutation({
  mutationFn: () => api.post(`/albums/${id.value}/hunt`),
  onSuccess: () => toast.info(t('album.searchStarted')),
})
const approving = ref<number | null>(null)
const approve = useMutation({
  mutationFn: (cid: number) => {
    approving.value = cid
    return api.post(`/candidates/${cid}/approve`)
  },
  onSuccess: (d) => {
    const r = d as { resumed?: boolean, missing?: number }
    toast.success(r.resumed ? t('album.resumed', r.missing ?? 0) : t('album.downloadStarted'), { description: t('album.followTransfers') })
    refresh()
  },
  onError: e => toast.error(e.message),
  onSettled: () => (approving.value = null),
})
const monitor = useMutation({
  mutationFn: (monitored: boolean) => api.patch(`/albums/${id.value}`, { monitored }),
  onSuccess: refresh,
})
const selectRelease = useMutation({
  mutationFn: (rid: number) => api.patch(`/albums/${id.value}`, { selected_release_id: rid }),
  onSuccess: () => {
    toast.success(t('album.editionSelected'))
    refresh()
  },
})
const cancelling = ref(false)
async function cancelDownload(cid?: number) {
  cancelling.value = true
  try {
    const r = await api.post<{ cancelled: number }>(cid ? `/candidates/${cid}/abandon` : `/albums/${id.value}/cancel`)
    toast.success(t('album.cancelled', r.cancelled), { description: t('album.cancelledHint') })
    refresh()
  }
  catch (e) {
    toast.error((e as Error).message)
  }
  finally {
    cancelling.value = false
  }
}
const activeSources = computed(() => (album.value?.candidates ?? []).filter(c => c.status === 'chosen'))
const downloading = computed(() => activeSources.value.length > 0)
/** Everything received or already shelved, but the album isn't shelved yet: import can run now */
const importable = computed(() => {
  const s = coverage.value?.summary
  return !!s && !!album.value && s.received > 0 && s.received + s.in_library >= s.total
    && !['imported', 'importing'].includes(album.value.status) && !downloading.value
})
const importNow = useMutation({
  mutationFn: () => api.post(`/albums/${id.value}/import`),
  onSuccess: () => {
    toast.info(t('album.importStarted'))
    refresh()
  },
  onError: e => toast.error(e.message),
})
const dismiss = useMutation({
  mutationFn: () => api.post(`/albums/${id.value}/dismiss`),
  onSuccess: refresh,
})
</script>

<template>
  <div :class="cn('relative isolate flex flex-col gap-8', accent.active.value && 'cover-accent')" :style="accent.style.value">
    <CoverBackdrop :mbid="album?.mbid" :tinted="accent.active.value" />

    <RouterLink
      v-if="album?.artist"
      :to="{ name: 'artist', params: { id: album.artist.id } }"
      class="inline-flex w-fit items-center gap-1.5 text-[12px] text-muted-foreground hover:text-foreground"
    >
      <ArrowLeftIcon class="size-3.5" />
      {{ album.artist.name }}
    </RouterLink>

    <div v-if="isPending" class="flex gap-8">
      <Skeleton class="size-52" />
      <div class="flex flex-1 flex-col gap-3">
        <Skeleton class="h-12 w-1/2" />
        <Skeleton class="h-4 w-1/3" />
      </div>
    </div>

    <template v-else-if="album">
      <header class="grid gap-7 md:grid-cols-[208px_minmax(0,1fr)]">
        <CoverArt
          :mbid="album.mbid"
          :size="500"
          :class="cn('w-full max-w-[208px] ring-1 ring-white/10', accent.active.value
            ? 'shadow-[0_28px_70px_-18px_hsl(var(--cover-h)_var(--cover-s)_18%/0.9)]'
            : 'shadow-[0_28px_70px_-22px_rgb(0_0_0/0.8)]')"
          :alt="album.title"
        />
        <div class="flex min-w-0 flex-col justify-end gap-4">
          <div class="flex flex-col gap-2">
            <p class="legend">
              {{ typeLabel }} · {{ year(album.first_release_date) }}
            </p>
            <h1 class="font-serif text-[44px] leading-[0.95] tracking-tight text-balance md:text-[54px]">
              {{ album.title }}
            </h1>
            <p class="text-[14px] text-muted-foreground">
              {{ album.artist?.name }}
              <span class="text-faint">· {{ t(`artists.mode.${album.artist?.mode ?? 'validation'}`) }}</span>
            </p>
          </div>

          <div class="flex flex-col gap-3 rounded-xl bg-card/55 p-4 ring-1 ring-white/[0.03] backdrop-blur-xl lg:flex-row lg:items-center lg:justify-between">
            <div class="flex flex-col gap-2.5">
              <SignalChain :status="album.status" size="lg" labels />
              <p class="flex flex-wrap items-center gap-2 text-[12.5px]">
                <StatusBadge :status="album.status" />
                <span class="text-muted-foreground">{{ album.status_reason ? serverText(album.status_reason, album.status_reason_t) : albumStatus(album.status).hint }}</span>
              </p>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <label class="mr-2 flex items-center gap-2 text-[12.5px] text-muted-foreground">
                <Switch :model-value="album.monitored" @update:model-value="(v: boolean) => monitor.mutate(v)" />
                {{ t('album.followed') }}
              </label>
              <Button v-if="importable" size="sm" :disabled="importNow.isPending.value" @click="importNow.mutate()">
                <FolderInputIcon data-icon="inline-start" />
                {{ t('album.importNow') }}
              </Button>
              <RedownloadButton
                v-if="album.imported_format || downloading"
                :album-id="album.id"
                :format="album.imported_format ? (FORMAT_LABELS[album.imported_format] ?? album.imported_format) : null"
                :downloading="downloading"
                @done="refresh"
              />
              <!-- Switching peer and cancelling live in the download panel; here only when it isn't shown -->
              <template v-if="downloading && !(coverage && hasSources)">
                <SwitchPeerMenu
                  v-for="c in activeSources"
                  :key="c.id"
                  :candidate-id="c.id"
                  :stuck="stuckSources.has(c.id)"
                  :peer="activeSources.length > 1 ? c.username : undefined"
                  size="sm"
                />
                <Button
                  variant="ghost"
                  size="sm"
                  class="text-muted-foreground hover:text-destructive"
                  :disabled="cancelling"
                  @click="cancelDownload()"
                >
                  <XIcon data-icon="inline-start" />
                  {{ t('album.cancelDownload') }}
                </Button>
              </template>
              <Button
                v-if="album.status === 'pending_approval'"
                variant="ghost"
                size="sm"
                class="text-muted-foreground"
                @click="dismiss.mutate()"
              >
                <XIcon data-icon="inline-start" />
                {{ t('album.rejectAll') }}
              </Button>
              <Button
                size="sm"
                variant="outline"
                :disabled="hunt.isPending.value || album.status === 'searching' || album.status === 'downloading'"
                @click="hunt.mutate()"
              >
                <RadarIcon data-icon="inline-start" />
                {{ album.status === 'imported' ? t('album.searchBetter') : t('album.searchNow') }}
              </Button>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-x-5 gap-y-1 text-[12px] text-faint">
            <span v-if="album.imported_format" class="flex items-center gap-1.5">{{ t('album.presentAs') }} <FormatTag :format="album.imported_format" /></span>
            <span v-if="lyricsStats.total" class="flex items-center gap-1.5">
              {{ t('lyrics.count', { n: lyricsStats.with, total: lyricsStats.total }) }}<template v-if="lyricsStats.synced">
                · {{ t('lyrics.countSynced', lyricsStats.synced) }}</template>
              <button
                v-if="lyricsStats.with < lyricsStats.total"
                type="button"
                class="inline-flex items-center gap-1 text-primary hover:underline disabled:opacity-60"
                :title="t('lyrics.fetchHint')"
                :disabled="fetchLyrics.isPending.value"
                @click="fetchLyrics.mutate()"
              >
                <Spinner v-if="fetchLyrics.isPending.value" class="size-3" />{{ t('lyrics.fetch') }}
              </button>
            </span>
            <span v-if="isPartial(album)" class="text-warning">{{ t('album.partial', { n: album.present_tracks, total: album.total_tracks }) }}</span>
            <span v-if="album.imported_path" class="min-w-0 truncate font-mono" :title="album.imported_path">{{ album.imported_path }}</span>
            <span>{{ t('album.lastSearch', { when: ago(album.last_searched_at) }) }}</span>
            <a :href="`https://musicbrainz.org/release-group/${album.mbid}`" target="_blank" rel="noreferrer" class="inline-flex items-center gap-1 hover:text-primary">
              MusicBrainz <ExternalLinkIcon class="size-3" />
            </a>
          </div>
        </div>
      </header>

      <ImportReview v-if="reviewCandidate" :candidate-id="reviewCandidate.id" :releases="album.releases" />

      <!-- The whole download in one place, until the album is shelved -->
      <DownloadPanel
        v-if="coverage && hasSources && album.status !== 'imported'"
        :album-id="album.id"
        :coverage="coverage"
        :transfers="album.transfers"
        :active="activeSources"
        :stuck="stuckSources"
        :cancelling="cancelling"
        @cancel="cancelDownload()"
      />
      <!-- A shelved album with tracks below the rest: who has better ones -->
      <TrackCoverage
        v-else-if="coverage?.summary.upgrade"
        :album-id="album.id"
        :coverage="coverage"
        @details="tab = 'tracks'; viewRelease = String(coverage.release_id)"
      />

      <Tabs v-model="tab" class="gap-5">
        <TabsList>
          <TabsTrigger value="tracks">
            {{ t('album.tabs.tracks') }}
          </TabsTrigger>
          <TabsTrigger value="candidates">
            {{ t('album.tabs.candidates') }} <span class="ml-1 font-mono text-[11px] text-faint tnum">{{ album.candidates.length }}</span>
          </TabsTrigger>
          <TabsTrigger value="files">
            {{ t('album.tabs.files') }} <span class="ml-1 font-mono text-[11px] text-faint tnum">{{ album.files.length }}</span>
          </TabsTrigger>
          <TabsTrigger value="transfers">
            {{ t('album.tabs.transfers') }} <span class="ml-1 font-mono text-[11px] text-faint tnum">{{ album.transfers.length }}</span>
          </TabsTrigger>
          <TabsTrigger value="history">
            {{ t('album.tabs.history') }}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="tracks" class="flex flex-col gap-4">
          <div class="flex flex-col gap-3 sm:flex-row sm:items-center">
            <Select v-model="viewRelease">
              <SelectTrigger class="w-full sm:max-w-xl">
                <SelectValue :placeholder="t('album.edition')" />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem v-for="r in album.releases" :key="r.id" :value="String(r.id)">
                    {{ r.title }}{{ r.disambiguation ? ` (${r.disambiguation})` : '' }} · {{ r.date ?? '?' }} · {{ r.country ?? '?' }} · {{ r.formats || '?' }} · {{ t('album.trackCount', r.track_count) }}
                  </SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
            <Button
              v-if="release && String(album.selected_release_id) !== viewRelease"
              variant="outline"
              size="sm"
              @click="selectRelease.mutate(release.id)"
            >
              {{ t('album.useEdition') }}
            </Button>
            <span v-else-if="release" class="text-[12px] text-success">{{ t('album.editionInUse') }}</span>
          </div>
          <p v-if="release" class="text-[12px] text-faint">
            {{ release.label ?? t('album.unknownLabel') }}<template v-if="release.barcode">
              · {{ t('album.barcode') }} <span class="font-mono">{{ release.barcode }}</span>
            </template> · {{ t('album.knownEditions', album.releases.length) }}
          </p>
          <div v-if="release" class="overflow-hidden rounded-xl bg-card">
            <div
              v-for="tr in release.tracks"
              :key="tr.id"
              class="grid grid-cols-[3rem_minmax(0,1fr)_auto_auto_auto] items-center gap-3 border-b px-4 py-2 text-[13px] last:border-b-0"
            >
              <span class="font-mono text-[11.5px] text-faint tnum">{{ release.disc_count > 1 ? `${tr.disc}-` : '' }}{{ String(tr.position).padStart(2, '0') }}</span>
              <span class="truncate">{{ tr.title }}<span v-if="tr.artist_credit && tr.artist_credit !== album.artist?.name" class="text-faint"> · {{ tr.artist_credit }}</span></span>
              <span class="font-mono text-[11.5px] text-muted-foreground tnum">{{ ms(tr.length_ms) }}</span>
              <!-- Its lyrics, once shelved -->
              <span class="flex w-14 justify-end">
                <LyricsTag v-if="lyricsByRecording.has(tr.recording_mbid)" :lyrics="lyricsByRecording.get(tr.recording_mbid)" />
              </span>
              <!-- Where this track comes from -->
              <span class="flex min-w-24 items-center justify-end gap-1.5">
                <template v-if="coverageByTrack?.get(tr.id)?.sources.length">
                  <span
                    v-for="src in coverageByTrack.get(tr.id)!.sources.slice(0, 2)"
                    :key="src.transfer_id"
                    :class="cn('inline-flex items-center gap-1.5 rounded-[4px] border px-1.5 py-px font-mono text-[11px]',
                               src.state === 'complete' ? 'border-success/25 text-success'
                               : ['failed', 'aborted'].includes(src.state) ? 'border-border text-faint line-through'
                                 : 'border-primary/30 text-primary')"
                  >
                    {{ src.username }}
                    <span v-if="src.format" class="opacity-70">{{ FORMAT_LABELS[src.format] ?? src.format }}</span>
                    <span v-if="src.state === 'transferring'" class="tnum">{{ progress(src) }} %</span>
                    <span v-else-if="src.state === 'queued'">{{ t('transferState.queued') }}</span>
                    <span v-else-if="src.state === 'paused'">{{ t('transferState.paused') }}</span>
                  </span>
                </template>
                <span
                  v-else-if="coverageByTrack?.get(tr.id)?.upgrade"
                  class="inline-flex items-center gap-1.5 text-[11px] text-warning"
                  :title="t('coverage.upgradeHint')"
                >{{ FORMAT_LABELS[coverageByTrack.get(tr.id)!.library_format ?? ''] }} · {{ t('coverage.toUpgrade') }}</span>
                <span
                  v-else-if="present.has(tr.recording_mbid) || coverageByTrack?.get(tr.id)?.state === 'in_library'"
                  class="text-[11px] text-success"
                >{{ t('coverage.state.in_library') }}</span>
                <span v-else :class="cn('text-[11px]', coverageByTrack && album.monitored ? 'text-warning' : 'text-faint')">{{ t('coverage.state.missing') }}</span>
              </span>
            </div>
          </div>
          <p v-else class="text-[12.5px] text-faint">
            {{ t('album.noTracklist') }}
          </p>
        </TabsContent>

        <TabsContent value="candidates" class="flex flex-col gap-4">
          <div v-if="album.search_runs.length" class="flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-muted-foreground">
            <span v-for="r in album.search_runs.slice(0, 4)" :key="r.id">
              <span class="font-mono text-foreground/80">{{ t('common.quoted', { text: r.query }) }}</span>
              <span class="text-faint"> → {{ t('album.searchRun', { files: r.result_count, peers: r.peer_count, when: ago(r.started_at) }) }}</span>
            </span>
          </div>
          <Empty v-if="!album.candidates.length" class="rounded-xl bg-card/50 py-12">
            <EmptyHeader>
              <EmptyTitle class="text-[14px]">
                {{ t('album.noCandidates') }}
              </EmptyTitle>
              <EmptyDescription class="text-[12.5px]">
                {{ t('album.noCandidatesHint') }}
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
          <CandidateCard
            v-for="(c, i) in proposed"
            :key="c.id"
            :candidate="c"
            :rank="i + 1"
            :received="received[c.id]"
            :stuck="stuckSources.has(c.id)"
            :can-approve="canApprove && ['proposed', 'failed'].includes(c.status)"
            :busy="approving === c.id || cancelling"
            @approve="approve.mutate"
            @abandon="cancelDownload"
          />
          <template v-if="rejected.length">
            <button
              type="button"
              class="w-fit text-[12px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
              @click="showRejected = !showRejected"
            >
              {{ showRejected ? t('album.hideRejected', rejected.length) : t('album.showRejected', rejected.length) }}
            </button>
            <template v-if="showRejected">
              <CandidateCard
                v-for="c in rejected"
                :key="c.id"
                :candidate="c"
                :can-approve="canApprove"
                :busy="approving === c.id"
                @approve="approve.mutate"
              />
            </template>
          </template>
        </TabsContent>

        <TabsContent value="files">
          <div v-if="album.files.length" class="overflow-hidden rounded-xl bg-card">
            <div
              v-for="f in album.files"
              :key="f.id"
              class="flex items-center gap-3 border-b px-4 py-2 last:border-b-0"
            >
              <FormatTag :format="f.format" />
              <span class="min-w-0 flex-1 truncate font-mono text-[12px]" :title="f.path">{{ f.path }}</span>
              <span class="font-mono text-[11.5px] text-faint tnum">{{ bytes(f.size) }}</span>
            </div>
          </div>
          <p v-else class="rounded-xl bg-card/50 px-4 py-6 text-[12.5px] text-faint">
            {{ t('album.noFiles') }}
          </p>
        </TabsContent>

        <TabsContent value="transfers">
          <div v-if="album.transfers.length" class="flex flex-col divide-y rounded-xl bg-card px-4">
            <TransferRow
              v-for="tr in album.transfers"
              :key="tr.id"
              :transfer="tr"
              :busy="tx.busy.value.has(tr.id)"
              @cancel="(tid) => tx.run([tid], 'cancel')"
              @retry="(tid) => tx.run([tid], 'retry')"
              @pause="(tid) => tx.run([tid], 'pause')"
              @resume="(tid) => tx.run([tid], 'resume')"
            />
          </div>
          <p v-else class="rounded-xl bg-card/50 px-4 py-6 text-[12.5px] text-faint">
            {{ t('album.noTransfers') }}
          </p>
        </TabsContent>

        <TabsContent value="history">
          <EventFeed v-if="album.events.length" :events="album.events" :links="false" />
          <p v-else class="text-[12.5px] text-faint">
            {{ t('common.nothingYet') }}
          </p>
        </TabsContent>
      </Tabs>
    </template>
  </div>
</template>
