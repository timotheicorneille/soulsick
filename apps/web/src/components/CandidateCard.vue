<script setup lang="ts">
import type { Candidate } from '@/lib/types'
import { ChevronDownIcon, DownloadIcon, FolderTreeIcon, RotateCcwIcon, XIcon } from '@lucide/vue'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Spinner } from '@/components/ui/spinner'
import { bytes, duration, splitPath, speed } from '@/lib/format'
import { cn } from '@/lib/utils'
import FormatTag from './FormatTag.vue'
import ScoreBreakdown from './ScoreBreakdown.vue'
import SwitchPeerMenu from './SwitchPeerMenu.vue'
import { serverText } from '@/lib/serverText'

const props = defineProps<{
  candidate: Candidate
  rank?: number
  busy?: boolean
  canApprove?: boolean
  received?: { done: number, total: number }
  /** One of its downloads stopped moving */
  stuck?: boolean
}>()
const emit = defineEmits<{ approve: [id: number], abandon: [id: number] }>()
const { t } = useI18n()
const open = ref(false)

const c = computed(() => props.candidate)
const audio = computed(() => c.value.files.filter(f => !f.extra))
const extras = computed(() => c.value.files.filter(f => f.extra))
const total = computed(() => audio.value.reduce((s, f) => s + (f.size ?? 0), 0))
const rejected = computed(() => c.value.status === 'rejected')

const statusBadge = computed(() => {
  const variant = ({
    proposed: null,
    rejected: 'quiet',
    chosen: 'signal',
    downloaded: 'signal',
    failed: 'danger',
    imported: 'success',
  } as const)[c.value.status]
  return variant ? { label: t(`candidate.status.${c.value.status}`), variant } : null
})
</script>

<template>
  <Collapsible v-model:open="open" :class="cn('rounded-xl bg-card transition-colors', c.status === 'chosen' && 'border-primary/40', c.status === 'imported' && 'border-success/30')">
    <div class="grid gap-4 p-4 md:grid-cols-[minmax(0,1fr)_260px_auto] md:items-center">
      <div class="flex min-w-0 flex-col gap-1.5">
        <div class="flex min-w-0 items-center gap-2">
          <span v-if="rank != null" class="font-mono text-[11px] text-faint tnum">#{{ rank }}</span>
          <RouterLink
            :to="{ name: 'peer', params: { username: c.username } }"
            class="truncate font-mono text-[13px] font-medium text-foreground hover:text-primary"
          >
            {{ c.username }}
          </RouterLink>
          <FormatTag :format="c.format" />
          <Badge v-if="statusBadge" :variant="statusBadge.variant">
            {{ statusBadge.label }}
          </Badge>
          <span
            v-if="received && received.total"
            :class="cn('font-mono text-[11px] tnum', received.done === received.total ? 'text-success' : 'text-muted-foreground')"
          >{{ t('candidate.received', { n: received.done, total: received.total }) }}</span>
        </div>
        <p class="truncate font-mono text-[11.5px] text-faint" :title="c.directory">
          {{ c.directory }}
        </p>
        <div class="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11.5px] text-muted-foreground">
          <span class="tnum">{{ t('album.trackCount', audio.length) }} · {{ bytes(total) }}</span>
          <span :class="c.has_free_slots ? 'text-success' : 'text-faint'">{{ c.has_free_slots ? t('peer.freeSlot') : t('peer.noFreeSlot') }}</span>
          <span class="tnum">{{ t('peer.queue', { n: c.queue_size }) }}</span>
          <span class="tnum">{{ speed(c.avg_speed) }}</span>
        </div>
        <p v-if="c.reject_reason" :class="cn('text-[12px]', c.status === 'failed' ? 'text-destructive' : 'text-warning')">
          {{ serverText(c.reject_reason, c.reject_reason_t) }}
        </p>
        <p v-if="c.status === 'failed'" class="text-[11.5px] text-muted-foreground">
          {{ t('candidate.resumeHint', { peer: c.username }) }}
        </p>
        <p v-if="c.score_detail.match?.missing?.length" class="text-[11.5px] text-faint">
          {{ t('candidate.missing', { tracks: c.score_detail.match.missing.join(', ') }) }}
        </p>
      </div>

      <ScoreBreakdown :detail="c.score_detail" :score="c.score" :muted="rejected" />

      <div class="flex items-center gap-1.5 md:justify-end">
        <CollapsibleTrigger as-child>
          <Button variant="ghost" size="sm" class="text-muted-foreground">
            {{ t('album.tabs.files') }}
            <ChevronDownIcon data-icon="inline-end" :class="cn('transition-transform', open && 'rotate-180')" />
          </Button>
        </CollapsibleTrigger>
        <SwitchPeerMenu v-if="c.status === 'chosen'" :candidate-id="c.id" :stuck="stuck" size="sm" />
        <Button
          v-if="c.status === 'chosen'"
          variant="ghost"
          size="sm"
          class="text-muted-foreground hover:text-destructive"
          :disabled="busy"
          :title="t('candidate.abandonHint')"
          @click="emit('abandon', c.id)"
        >
          <XIcon data-icon="inline-start" />
          {{ t('candidate.abandon') }}
        </Button>
        <Button
          v-if="canApprove"
          :variant="rejected ? 'outline' : 'default'"
          size="sm"
          :disabled="busy"
          @click="emit('approve', c.id)"
        >
          <Spinner v-if="busy" data-icon="inline-start" />
          <RotateCcwIcon v-else-if="c.status === 'failed'" data-icon="inline-start" />
          <DownloadIcon v-else data-icon="inline-start" />
          {{ c.status === 'failed' ? t('common.resume') : c.status === 'rejected' ? t('candidate.force') : t('candidate.download') }}
        </Button>
      </div>
    </div>

    <CollapsibleContent>
      <div class="border-t px-4 py-3">
        <table class="w-full text-[12px]">
          <thead>
            <tr class="text-left">
              <th class="legend pb-2 font-medium">
                {{ t('candidate.remoteFile') }}
              </th>
              <th class="legend pb-2 text-right font-medium">
                Format
              </th>
              <th class="legend hidden pb-2 text-right font-medium sm:table-cell">
                {{ t('candidate.duration') }}
              </th>
              <th class="legend pb-2 text-right font-medium">
                {{ t('candidate.size') }}
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-border/60">
            <tr v-for="f in [...audio, ...extras]" :key="f.filename" :class="f.extra ? 'text-faint' : ''">
              <td class="max-w-0 truncate py-1.5 pr-3 font-mono" :title="f.filename">
                {{ splitPath(f.filename).name }}
              </td>
              <td class="py-1.5 text-right font-mono whitespace-nowrap text-muted-foreground tnum">
                {{ f.extra ? t('candidate.extra') : [f.bitrate ? `${f.bitrate} kbps` : null, f.bit_depth ? `${f.bit_depth} bit` : null].filter(Boolean).join(' · ') || '—' }}
              </td>
              <td class="hidden py-1.5 text-right font-mono text-muted-foreground tnum sm:table-cell">
                {{ duration(f.duration) }}
              </td>
              <td class="py-1.5 text-right font-mono whitespace-nowrap text-muted-foreground tnum">
                {{ bytes(f.size) }}
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="c.download_dir" class="mt-3 flex items-center gap-2 text-[11.5px] text-faint">
          <FolderTreeIcon class="size-3.5" />
          {{ t('candidate.downloadDir') }} <span class="font-mono text-muted-foreground">{{ c.download_dir }}</span>
        </div>
      </div>
    </CollapsibleContent>
  </Collapsible>
</template>
