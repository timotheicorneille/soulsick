<script setup lang="ts">
import type { Coverage, CoverageFiller, CoverageHolder, CoverageSource, CoverageTrack, Transfer } from '@/lib/types'
import { ArrowDownToLineIcon, XIcon } from '@lucide/vue'
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { toast } from 'vue-sonner'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Spinner } from '@/components/ui/spinner'
import { api } from '@/lib/api'
import { speed } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useLive } from '@/stores/live'
import FormatTag from './FormatTag.vue'
import SwitchPeerMenu from './SwitchPeerMenu.vue'

/**
 * The whole download of an album in one place: why it is waiting, where each track comes from,
 * and what you can do about it (take a track from another peer, switch peer, cancel).
 */
const props = defineProps<{
  albumId: number
  coverage: Coverage
  transfers: Transfer[]
  /** Sources downloading now, with the ones that stopped moving */
  active: { id: number, username: string }[]
  stuck: Set<number>
  cancelling: boolean
}>()
const emit = defineEmits<{ cancel: [] }>()
const { t } = useI18n()
const qc = useQueryClient()
const live = useLive()

const ACTIVE = ['queued', 'initializing', 'transferring', 'paused']
const s = computed(() => props.coverage.summary)
const done = computed(() => s.value.received + s.value.in_library)

/** The transfer row of a source, with what came live since the page loaded */
function liveOf(src: CoverageSource) {
  const row = props.transfers.find(x => x.id === src.transfer_id)
  return { ...row, ...live.transfers.get(src.transfer_id) } as Partial<Transfer>
}

/** The source that says best where the track is: received, then in progress, then the last attempt */
function lead(tr: CoverageTrack): CoverageSource | undefined {
  return tr.sources[0]
}

function line(tr: CoverageTrack): { text: string, tone: string } {
  const src = lead(tr)
  if (tr.state === 'in_library')
    return { text: t('download.track.inLibrary'), tone: 'text-success/80' }
  if (tr.state === 'received')
    return { text: t('download.track.received', { peer: src?.username }), tone: 'text-success' }
  if (src && ACTIVE.includes(src.state)) {
    const l = liveOf(src)
    if (l.state === 'transferring') {
      const pct = l.size ? Math.round(((l.bytes_transferred ?? 0) / l.size) * 100) : 0
      return { text: t('download.track.transferring', { pct, peer: src.username }), tone: 'text-primary' }
    }
    if (l.state === 'paused')
      return { text: t('download.track.paused', { peer: src.username }), tone: 'text-muted-foreground' }
    return l.place_in_queue
      ? { text: t('download.track.queuedAt', { pos: l.place_in_queue, peer: src.username }), tone: 'text-muted-foreground' }
      : { text: t('download.track.queued', { peer: src.username }), tone: 'text-muted-foreground' }
  }
  if (src?.state === 'failed')
    return { text: t('download.track.refused', { peer: src.username }), tone: 'text-destructive' }
  if (src?.state === 'aborted')
    return { text: t('download.track.cancelled', { peer: src.username }), tone: 'text-faint' }
  return { text: t('download.track.missing'), tone: 'text-warning' }
}

const inFlight = computed(() => props.coverage.tracks.filter(tr => tr.sources.some(x => ACTIVE.includes(x.state))))
const missing = computed(() => props.coverage.tracks.filter(tr => tr.state === 'missing' && !tr.sources.some(x => ACTIVE.includes(x.state))))

/** Why the album is waiting, in plain words */
const why = computed(() => {
  if (inFlight.value.length) {
    const byPeer = new Map<string, number>()
    for (const tr of inFlight.value) {
      const src = tr.sources.find(x => ACTIVE.includes(x.state))!
      byPeer.set(src.username, (byPeer.get(src.username) ?? 0) + 1)
    }
    return { text: [...byPeer].map(([peer, n]) => t('download.waitingOn', { n, peer }, n)).join(' · '), tone: 'text-muted-foreground' }
  }
  if (missing.value.length) {
    const titles = missing.value.slice(0, 3).map(x => x.title).join(', ') + (missing.value.length > 3 ? '…' : '')
    const someone = missing.value.some(x => x.holders?.length)
    return { text: t(someone ? 'download.missingTake' : 'download.missingNobody', { titles }, missing.value.length), tone: 'text-warning' }
  }
  return { text: t('download.allThere'), tone: 'text-success' }
})

const taking = ref<string | null>(null)
const take = useMutation({
  mutationFn: ({ tr, h }: { tr: CoverageTrack, h: CoverageHolder }) => {
    taking.value = `${tr.track_id}:${h.candidate_id}`
    return api.post(`/albums/${props.albumId}/tracks/${tr.track_id}/take`, { candidate_id: h.candidate_id })
  },
  onSuccess: (_, { tr, h }) => {
    toast.success(t('download.taken', { title: tr.title, peer: h.username }))
    qc.invalidateQueries({ queryKey: ['album'] })
  },
  onError: e => toast.error(e.message),
  onSettled: () => (taking.value = null),
})

const filling = ref<number | null>(null)
const fill = useMutation({
  mutationFn: (f: CoverageFiller) => {
    filling.value = f.candidate_id
    return api.post<{ files: number }>(`/albums/${props.albumId}/complete`, {
      candidate_id: f.candidate_id,
      track_ids: f.tracks.map(x => x.track_id),
    })
  },
  onSuccess: (d, f) => {
    toast.success(t('coverage.requested', { n: d.files, peer: f.username }, d.files))
    qc.invalidateQueries({ queryKey: ['album'] })
  },
  onError: e => toast.error(e.message),
  onSettled: () => (filling.value = null),
})
</script>

<template>
  <section class="flex flex-col gap-4 rounded-xl bg-card/70 p-5 ring-1 ring-white/[0.03] backdrop-blur-xl">
    <!-- Summary: where it stands and why it waits -->
    <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div class="flex min-w-0 flex-col gap-1">
        <span class="legend">{{ t('download.title') }}</span>
        <div class="flex items-baseline gap-2">
          <span class="font-mono text-[28px] leading-none font-medium tnum">{{ done }}<span class="text-faint">/{{ s.total }}</span></span>
          <span :class="cn('text-[12.5px]', why.tone)">{{ why.text }}</span>
        </div>
        <span class="text-[11.5px] text-faint">{{ t('coverage.edition', { title: coverage.release_title }) }}</span>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <SwitchPeerMenu
          v-for="c in active"
          :key="c.id"
          :candidate-id="c.id"
          :stuck="stuck.has(c.id)"
          :peer="active.length > 1 ? c.username : undefined"
          size="sm"
        />
        <Button
          v-if="active.length"
          variant="ghost"
          size="sm"
          class="text-muted-foreground hover:text-destructive"
          :disabled="cancelling"
          @click="emit('cancel')"
        >
          <XIcon data-icon="inline-start" />
          {{ t('album.cancelDownload') }}
        </Button>
      </div>
    </div>

    <!-- One line per track: its state, where it comes from, and taking it elsewhere -->
    <div class="overflow-hidden rounded-lg border">
      <div
        v-for="tr in coverage.tracks"
        :key="tr.track_id"
        class="grid grid-cols-[2.5rem_minmax(0,1fr)_auto] items-center gap-3 border-b px-3 py-1.5 text-[13px] last:border-b-0"
      >
        <span class="font-mono text-[11.5px] text-faint tnum">{{ String(tr.position).padStart(2, '0') }}</span>
        <span class="flex min-w-0 flex-col">
          <span class="truncate">{{ tr.title }}</span>
          <span :class="cn('truncate text-[11.5px]', line(tr).tone)">{{ line(tr).text }}</span>
        </span>
        <Popover v-if="!['received', 'in_library'].includes(tr.state) && tr.holders?.length">
          <PopoverTrigger as-child>
            <Button size="xs" variant="outline">
              <ArrowDownToLineIcon data-icon="inline-start" />
              {{ t('download.takeFrom') }}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" class="w-80 p-1.5">
            <p class="px-2 py-1.5 text-[11.5px] text-muted-foreground">
              {{ t('download.takeHint', { title: tr.title }) }}
            </p>
            <button
              v-for="h in tr.holders"
              :key="h.candidate_id"
              type="button"
              class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] hover:bg-accent disabled:opacity-60"
              :disabled="take.isPending.value"
              @click="take.mutate({ tr, h })"
            >
              <Spinner v-if="taking === `${tr.track_id}:${h.candidate_id}`" class="size-3.5" />
              <span class="min-w-0 flex-1 truncate font-mono">{{ h.username }}</span>
              <FormatTag :format="h.format" />
              <span :class="cn('text-[11px]', h.has_free_slots ? 'text-success' : 'text-faint')">
                {{ h.has_free_slots ? t('peer.freeSlot') : t('peer.queue', { n: h.queue_size }) }}
              </span>
              <span class="font-mono text-[11px] text-faint tnum">{{ speed(h.avg_speed) }}</span>
            </button>
          </PopoverContent>
        </Popover>
        <span v-else />
      </div>
    </div>

    <!-- Several tracks missing: take them all from one peer -->
    <div v-if="missing.length > 1 && coverage.fillers.length" class="flex flex-wrap items-center gap-2">
      <span class="text-[12px] text-muted-foreground">{{ t('download.fillAll') }}</span>
      <Button
        v-for="f in coverage.fillers.slice(0, 3)"
        :key="f.candidate_id"
        size="xs"
        variant="outline"
        :disabled="fill.isPending.value"
        @click="fill.mutate(f)"
      >
        <Spinner v-if="filling === f.candidate_id" data-icon="inline-start" />
        {{ t('download.fillFrom', { peer: f.username, n: f.tracks.length }) }}
        <FormatTag :format="f.format" class="ml-1" />
      </Button>
    </div>
  </section>
</template>
